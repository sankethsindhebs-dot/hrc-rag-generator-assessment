"""Ingest documents into a Collection and retrieve chunks for a question."""
from dataclasses import dataclass

import numpy as np

from .chunking import chunk_text
from .config import Settings
from .embeddings import Embedder
from .loaders import DocumentError, load_document
from .store import Chunk, Collection, Hit


def build_collection(files: list[tuple[str, bytes]], embedder: Embedder, settings: Settings) -> Collection:
    """files: (filename, raw bytes). Raises DocumentError naming the first bad file."""
    if not files:
        raise DocumentError("no files provided")
    limit = settings.max_file_mb * 1024 * 1024
    chunks: list[Chunk] = []
    for filename, data in files:
        if len(data) > limit:
            raise DocumentError(f"{filename}: larger than {settings.max_file_mb} MB")
        doc = load_document(filename, data)
        for page in doc.pages:
            for text in chunk_text(page.text, settings.chunk_size, settings.chunk_overlap):
                chunks.append(Chunk(len(chunks), doc.name, page.number, text))
    vectors = embedder.embed_documents([c.text for c in chunks])
    return Collection(chunks=chunks, vectors=np.asarray(vectors, dtype=np.float32))


@dataclass(frozen=True)
class Retrieval:
    hits: list[Hit]
    min_score: float

    @property
    def top_score(self) -> float:
        return self.hits[0].score if self.hits else 0.0

    @property
    def passes_gate(self) -> bool:
        """Cheap pre-filter only. Passing does NOT mean the chunks answer the question;
        the LLM (stage 2) must still judge sufficiency."""
        return self.top_score >= self.min_score


def retrieve(collection: Collection, question: str, embedder: Embedder, settings: Settings) -> Retrieval:
    hits = collection.search(embedder.embed_query(question), settings.top_k)
    return Retrieval(hits=hits, min_score=settings.min_score)
