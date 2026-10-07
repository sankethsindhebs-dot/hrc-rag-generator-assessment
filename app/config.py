"""Runtime configuration, read from environment variables. Nothing secret lives here."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

# Chroma publishes the ONNX export of sentence-transformers/all-MiniLM-L6-v2 here.
# The digest below was computed from the archive actually downloaded from this URL
# (83,178,821 bytes); the download is rejected if the bytes ever differ.
DEFAULT_MODEL_URL = "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
DEFAULT_MODEL_SHA256 = "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
# Retrieval-confidence gate: questions whose best chunk scores below this (cosine similarity,
# all-MiniLM-L6-v2) are answered "insufficient context" without calling the generator.
# Chosen from scripts/calibrate_threshold.py; see the README for the calibration table and limits.
DEFAULT_MIN_SCORE = 0.15


class ConfigError(ValueError):
    """An environment variable holds an unusable value."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_cache_dir: Path
    model_url: str
    model_sha256: str
    chunk_size: int  # characters; ~800 chars is ~200 tokens, inside MiniLM's 256-token window
    chunk_overlap: int  # characters
    top_k: int
    max_upload_bytes: int
    min_score: float = DEFAULT_MIN_SCORE
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    # Never printed or logged: repr/compare are disabled so a Settings object is safe to show.
    anthropic_api_key: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        def get_int(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
            if value <= 0:
                raise ConfigError(f"{name} must be positive, got {value}")
            return value

        def get_str(name: str, default: str) -> str:
            raw = env.get(name)
            return default if raw is None or raw.strip() == "" else raw.strip()

        model_url = get_str("RAG_MODEL_URL", DEFAULT_MODEL_URL)
        model_sha256 = get_str("RAG_MODEL_SHA256", DEFAULT_MODEL_SHA256).lower()
        if not _SHA256_RE.match(model_sha256):
            raise ConfigError("RAG_MODEL_SHA256 must be 64 hex characters")
        if model_url != DEFAULT_MODEL_URL and model_sha256 == DEFAULT_MODEL_SHA256:
            raise ConfigError("RAG_MODEL_URL was changed, so RAG_MODEL_SHA256 must be set for the new archive")

        chunk_size = get_int("RAG_CHUNK_SIZE", 800)
        chunk_overlap = get_int("RAG_CHUNK_OVERLAP", 150)
        if chunk_overlap >= chunk_size:
            raise ConfigError("RAG_CHUNK_OVERLAP must be smaller than RAG_CHUNK_SIZE")

        raw_score = env.get("RAG_MIN_SCORE")
        try:
            min_score = DEFAULT_MIN_SCORE if raw_score is None or raw_score.strip() == "" else float(raw_score)
        except ValueError:
            raise ConfigError(f"RAG_MIN_SCORE must be a number, got {raw_score!r}") from None
        if not 0.0 <= min_score <= 1.0:
            raise ConfigError(f"RAG_MIN_SCORE must be between 0 and 1, got {min_score}")

        return cls(
            data_dir=Path(get_str("RAG_DATA_DIR", "data")),
            model_cache_dir=Path(get_str("RAG_MODEL_CACHE_DIR", ".cache/models")),
            model_url=model_url,
            model_sha256=model_sha256,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            top_k=get_int("RAG_TOP_K", 4),
            max_upload_bytes=get_int("RAG_MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
            min_score=min_score,
            anthropic_model=get_str("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL),
            anthropic_api_key=(env.get("ANTHROPIC_API_KEY") or "").strip() or None,
        )
