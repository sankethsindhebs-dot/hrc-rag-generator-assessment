"""Application factory.

    uvicorn --factory app.main:create_app

Configuration comes from environment variables (see app/config.py). ``create_app`` also accepts an
embedder and generator so tests can supply deterministic doubles.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.errors import SECURITY_HEADERS, ApiError, describe, envelope
from app.api.middleware import BodyLimitMiddleware, SecurityHeadersMiddleware
from app.api.routes import MAX_FILES_PER_UPLOAD, Services, router
from app.config import Settings
from app.rag.answering import AnswerService
from app.rag.collections import CollectionManager
from app.rag.embeddings import Embedder, LazyOnnxEmbedder
from app.rag.errors import GenerationUnavailableError, ModelUnavailableError, RagError
from app.rag.generation import Generator, build_generator

log = logging.getLogger("app.api")
STATIC_DIR = Path(__file__).parent / "static"
_UPLOAD_OVERHEAD = 1024 * 1024  # multipart framing, field names, a little slack


def create_app(
    settings: Settings | None = None,
    *,
    embedder: Embedder | None = None,
    generator: Generator | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    embedder = embedder or LazyOnnxEmbedder(settings.model_cache_dir, settings.model_url, settings.model_sha256)
    generator = generator or build_generator(settings)
    manager = CollectionManager(settings, embedder)
    services = Services(settings, embedder, generator, manager, AnswerService(manager, generator, settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        warm = getattr(embedder, "warm", None)
        if warm is not None:  # load the model in the background; /api/health reports progress
            def _warm():
                try:
                    warm()
                except ModelUnavailableError:
                    pass  # already logged; the next request retries
            threading.Thread(target=_warm, name="embedding-warmup", daemon=True).start()
        yield

    app = FastAPI(title="RAG Generator", version="0.1.0", lifespan=lifespan)
    app.state.services = services
    app.include_router(router)
    register_error_handlers(app)
    # Added first = inner. The size cap sits inside the security headers so even its 413s get them.
    app.add_middleware(BodyLimitMiddleware, upload_limit=settings.max_upload_bytes * MAX_FILES_PER_UPLOAD + _UPLOAD_OVERHEAD)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RagError)
    async def rag_error(request: Request, exc: RagError):
        error = describe(exc)
        if error.status >= 500 or error.status == 503:
            log.error("%s %s -> %s: %s", request.method, request.url.path, error.code, exc)
        extra = {}
        if isinstance(exc, GenerationUnavailableError):  # retrieval worked: hand the evidence back
            from app.api.schemas import evidence_out
            extra["evidence"] = [evidence_out(e).model_dump() for e in exc.evidence]
        return JSONResponse(envelope(error, **extra), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(envelope(ApiError(422, "invalid_request", "The request was malformed."), details=details),
                            status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        codes = {404: "not_found", 405: "method_not_allowed", 413: "request_too_large"}
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        error = ApiError(exc.status_code, codes.get(exc.status_code, "http_error"), message)
        return JSONResponse(envelope(error), status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        # This handler runs outside the middleware stack, so attach the security headers here.
        return JSONResponse(envelope(describe(exc)), status_code=500, headers=SECURITY_HEADERS)
