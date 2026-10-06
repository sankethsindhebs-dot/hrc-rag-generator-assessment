"""Environment-driven settings. Nothing here is a secret."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    embed_model: str = "BAAI/bge-small-en-v1.5"
    chunk_size: int = 800
    chunk_overlap: int = 100
    top_k: int = 5
    # Provisional: similarity scales differ per embedding model, so this must be
    # calibrated (scripts/calibrate.py). It is a cheap pre-filter, not proof of grounding.
    min_score: float = 0.45
    max_file_mb: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        return cls(
            embed_model=os.getenv("RAG_EMBED_MODEL", d.embed_model),
            chunk_size=int(os.getenv("RAG_CHUNK_SIZE", d.chunk_size)),
            chunk_overlap=int(os.getenv("RAG_CHUNK_OVERLAP", d.chunk_overlap)),
            top_k=int(os.getenv("RAG_TOP_K", d.top_k)),
            min_score=float(os.getenv("RAG_MIN_SCORE", d.min_score)),
            max_file_mb=int(os.getenv("RAG_MAX_FILE_MB", d.max_file_mb)),
        )
