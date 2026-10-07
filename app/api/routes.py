"""HTTP routes. Deliberately thin: parse input, call the domain layer, serialise the result.

Routes are plain ``def`` so FastAPI runs them in its thread pool; embedding and generation are
blocking work that must not stall the event loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.api import schemas
from app.api.errors import describe
from app.config import Settings
from app.rag.answering import MAX_QUESTION_CHARS, AnswerService
from app.rag.collections import CollectionManager
from app.rag.embeddings import Embedder
from app.rag.errors import DocumentLoadError, DocumentTooLargeError, InvalidInputError
from app.rag.generation import Generator

MAX_FILES_PER_UPLOAD = 5
_MAX_FILENAME = 255


@dataclass
class Services:
    settings: Settings
    embedder: Embedder
    generator: Generator
    manager: CollectionManager
    answers: AnswerService


def get_services(request: Request) -> Services:
    return request.app.state.services


router = APIRouter(prefix="/api")


@router.get("/health")
def health(services: Services = Depends(get_services)):
    s = services
    embedding_ready = bool(getattr(s.embedder, "loaded", True))  # embedders without lazy loading are always ready
    generation_ready = bool(s.generator.available)
    body = {
        "status": "ok" if embedding_ready else "starting",
        "ready": embedding_ready,
        "embedding": {
            "model": s.embedder.name,
            "ready": embedding_ready,
            "detail": None if embedding_ready else "The embedding model is not loaded yet (it downloads on first use).",
        },
        "generation": {
            "configured": generation_ready,
            "model": s.settings.anthropic_model if generation_ready else None,
            "detail": None if generation_ready else
            "ANTHROPIC_API_KEY is not set: documents can be uploaded and searched, but answers are unavailable.",
        },
        "limits": {
            "max_upload_bytes": s.settings.max_upload_bytes,
            "max_files_per_upload": MAX_FILES_PER_UPLOAD,
            "max_question_chars": MAX_QUESTION_CHARS,
            "min_score": s.settings.min_score,
            "top_k": s.settings.top_k,
        },
    }
    return JSONResponse(body, status_code=200 if embedding_ready else 503)


@router.post("/collections", status_code=201, response_model=schemas.CollectionOut)
def create_collection(body: schemas.CreateCollectionIn, s: Services = Depends(get_services)):
    return schemas.collection_out(s.manager.create_collection(body.name))


@router.get("/collections")
def list_collections(s: Services = Depends(get_services)):
    return {"collections": [schemas.collection_out(c) for c in s.manager.list_collections()]}


@router.delete("/collections/{collection_id}", status_code=204)
def delete_collection(collection_id: str, s: Services = Depends(get_services)):
    s.manager.delete_collection(collection_id)
    return Response(status_code=204)


@router.get("/collections/{collection_id}/documents")
def list_documents(collection_id: str, s: Services = Depends(get_services)):
    return {"documents": [schemas.document_out(d) for d in s.manager.list_documents(collection_id)]}


@router.post("/collections/{collection_id}/documents", response_model=schemas.UploadOut)
def upload_documents(
    collection_id: str,
    response: Response,
    files: list[UploadFile] = File(...),
    s: Services = Depends(get_services),
):
    """Index one or more uploaded files. Per-file problems (unsupported, empty, too large,
    unreadable) are reported in ``rejected``; the status is 201 if anything was indexed,
    otherwise the status of the first rejection. Systemic failures raise instead."""
    s.manager.get_collection(collection_id)  # 404 before reading or embedding anything
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise InvalidInputError(f"at most {MAX_FILES_PER_UPLOAD} files per upload request")

    limit = s.settings.max_upload_bytes
    indexed: list[schemas.DocumentOut] = []
    rejected: list[schemas.RejectedOut] = []
    first_rejection_status = 422
    for upload in files:
        name = clean_filename(upload.filename)
        try:
            data = upload.file.read(limit + 1)  # never pull more than limit+1 bytes into memory
            if len(data) > limit:
                raise DocumentTooLargeError(f"{name!r} is larger than the {limit}-byte limit")
            indexed.append(schemas.document_out(s.manager.add_document(collection_id, name, data)))
        except DocumentLoadError as exc:
            problem = describe(exc)
            if not rejected:
                first_rejection_status = problem.status
            rejected.append(schemas.RejectedOut(filename=name, code=problem.code, message=problem.message))
    response.status_code = 201 if indexed else first_rejection_status
    return schemas.UploadOut(indexed=indexed, rejected=rejected)


@router.post("/collections/{collection_id}/ask", response_model=schemas.AskOut)
def ask(collection_id: str, body: schemas.AskIn, s: Services = Depends(get_services)):
    return schemas.ask_out(s.answers.ask(collection_id, body.question, body.top_k, body.min_score))


def clean_filename(raw: str | None) -> str:
    """Display name only (it is never used as a file-system path): drop any directory part and
    control characters, bound the length."""
    name = PurePosixPath((raw or "").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:_MAX_FILENAME] or "untitled"

