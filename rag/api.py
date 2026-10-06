"""FastAPI app: upload documents at runtime, ask grounded questions about them."""
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Settings
from .embeddings import Embedder, EmbeddingError, FastEmbedEmbedder
from .grounding import answer_question
from .llm import LLM, ClaudeLLM, ConfigError, LLMError
from .loaders import DocumentError
from .pipeline import build_collection
from .store import Collection

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_QUESTION_CHARS = 1000


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


class _LazyClaude:
    """Builds the Claude client on first use, so the server starts (and uploads work) without a key."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._llm: ClaudeLLM | None = None

    def generate(self, system: str, user: str, schema: dict) -> str:
        if self._llm is None:
            self._llm = ClaudeLLM(self._settings.llm_model)
        return self._llm.generate(system, user, schema)


def create_app(embedder: Embedder | None = None, llm: LLM | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    embedder = embedder or FastEmbedEmbedder(settings.embed_model)
    llm = llm or _LazyClaude(settings)
    collections: dict[str, Collection] = {}  # in memory only: lost on restart, by design

    app = FastAPI(title="RAG Generator")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok", "collections": len(collections)}

    @app.post("/collections", status_code=201)
    def create_collection(files: list[UploadFile] = File(...)):
        limit = settings.max_file_mb * 1024 * 1024
        payload: list[tuple[str, bytes]] = []
        for f in files:
            data = f.file.read(limit + 1)  # never buffer more than limit + 1 bytes of one file
            payload.append((f.filename or "upload", data))
        try:
            collection = build_collection(payload, embedder, settings)
        except DocumentError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except EmbeddingError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        collections[collection.id] = collection
        return {
            "collection_id": collection.id,
            "documents": collection.doc_names,
            "n_chunks": len(collection.chunks),
        }

    @app.post("/collections/{collection_id}/ask")
    def ask(collection_id: str, body: AskRequest):
        collection = collections.get(collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="unknown collection")
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="question is empty")
        try:
            return answer_question(collection, question, embedder, llm, settings).to_dict()
        except ConfigError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except (LLMError, EmbeddingError) as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    return app


app = create_app()
