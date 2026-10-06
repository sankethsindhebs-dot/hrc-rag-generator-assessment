"""In-memory collection: chunk records plus a normalized embedding matrix."""
import uuid
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Chunk:
    id: int  # index within the collection
    doc: str
    page: int
    text: str


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float  # cosine similarity


@dataclass
class Collection:
    chunks: list[Chunk]
    vectors: np.ndarray  # (n_chunks, dim), L2-normalized
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def doc_names(self) -> list[str]:
        return sorted({c.doc for c in self.chunks})

    def search(self, query_vec: np.ndarray, k: int) -> list[Hit]:
        if not self.chunks or k <= 0:
            return []
        scores = self.vectors @ query_vec
        top = np.argsort(-scores)[:k]
        return [Hit(self.chunks[i], float(scores[i])) for i in top]
