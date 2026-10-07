"""Embedding abstraction plus the default local backend (ONNX all-MiniLM-L6-v2).

The model is fetched on first use into a cache directory (never into git), verified
against a SHA-256 digest, and only then unpacked. If it cannot be obtained, we raise
ModelUnavailableError -- we do NOT quietly switch to a different retrieval method.
"""

from __future__ import annotations

import hashlib
import os
import logging
import shutil
import tarfile
import threading
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from app.rag.errors import ModelUnavailableError

log = logging.getLogger(__name__)

MODEL_NAME = "onnx-all-MiniLM-L6-v2"
MODEL_DIMENSION = 384
REQUIRED_FILES = ("model.onnx", "tokenizer.json")
MAX_SEQUENCE_LENGTH = 256  # all-MiniLM-L6-v2's standard window; longer text is truncated
_DOWNLOAD_TIMEOUT_S = 60


class Embedder(Protocol):
    """Maps texts to L2-normalised float32 vectors of a fixed dimension."""

    name: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Return an array of shape (len(texts), dimension)."""
        ...


def ensure_model(cache_dir: Path, url: str, sha256: str) -> Path:
    """Return a directory containing the model files, downloading and verifying if needed."""
    cache_dir = Path(cache_dir)
    target = cache_dir / f"all-MiniLM-L6-v2-{sha256[:12]}"
    if all((target / name).is_file() for name in REQUIRED_FILES):
        return target

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_dir, prefix=".download-") as tmp:
        archive = Path(tmp) / "model.tar.gz"
        _download(url, archive, sha256)
        staging = Path(tmp) / "staging"
        staging.mkdir()
        _extract_required(archive, staging)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    return target


def _download(url: str, dest: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp, open(dest, "wb") as out:
            while chunk := resp.read(1024 * 1024):
                digest.update(chunk)
                out.write(chunk)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ModelUnavailableError(
            f"Could not download the embedding model from {url}: {exc}. "
            "Check network access, or pre-populate the model cache directory."
        ) from exc
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise ModelUnavailableError(
            f"Embedding model checksum mismatch: expected {expected_sha256}, got {actual}. "
            "Refusing to use the downloaded file."
        )


def _extract_required(archive: Path, dest: Path) -> None:
    """Copy only the files we need, by exact basename, ignoring every other member
    (so a hostile archive cannot write outside ``dest``)."""
    found: set[str] = set()
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            for member in tar:
                base = os.path.basename(member.name)
                if member.isfile() and base in REQUIRED_FILES and base not in found:
                    src = tar.extractfile(member)
                    if src is None:
                        continue
                    with src, open(dest / base, "wb") as out:
                        shutil.copyfileobj(src, out)
                    found.add(base)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ModelUnavailableError(f"The embedding model archive is unreadable: {exc}") from exc
    missing = set(REQUIRED_FILES) - found
    if missing:
        raise ModelUnavailableError(f"The embedding model archive is missing: {', '.join(sorted(missing))}")


class OnnxMiniLMEmbedder:
    """all-MiniLM-L6-v2 on onnxruntime: tokenise, run, mean-pool, L2-normalise."""

    name = MODEL_NAME

    def __init__(self, model_dir: Path, batch_size: int = 32):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = Path(model_dir)
        try:
            self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
            self._session = ort.InferenceSession(
                str(model_dir / "model.onnx"), providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # corrupt/incompatible files surface as assorted library errors
            raise ModelUnavailableError(f"Could not load the embedding model from {model_dir}: {exc}") from exc
        # The shipped tokenizer.json hard-codes a 128-token truncation and fixed padding;
        # use the model's standard 256-token window and pad only to the longest in a batch.
        self._tokenizer.enable_truncation(max_length=MAX_SEQUENCE_LENGTH)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._batch_size = batch_size
        self.dimension = int(self._session.get_outputs()[0].shape[-1])

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if len(texts) == 0:
            return np.zeros((0, self.dimension), dtype=np.float32)
        parts = [self._embed_batch(list(texts[i : i + self._batch_size])) for i in range(0, len(texts), self._batch_size)]
        return np.vstack(parts)

    def _embed_batch(self, batch: list[str]) -> np.ndarray:
        encodings = self._tokenizer.encode_batch(batch)
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feeds)[0]  # (batch, seq, dim)
        weights = mask[:, :, None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norms, 1e-12, None)).astype(np.float32)


class LazyOnnxEmbedder:
    """The default embedder, loaded on first use so the app can start (and report its status)
    without the model. ``name`` and ``dimension`` are known up front; the first ``embed`` -- or
    ``warm()`` -- downloads, verifies and loads the model. A failure is raised as
    ModelUnavailableError and retried on the next call."""

    name = MODEL_NAME
    dimension = MODEL_DIMENSION

    def __init__(self, cache_dir: Path, url: str, sha256: str):
        self._args = (cache_dir, url, sha256)
        self._embedder: OnnxMiniLMEmbedder | None = None
        self._lock = threading.Lock()
        self.last_error: str | None = None

    @property
    def loaded(self) -> bool:
        return self._embedder is not None

    def warm(self) -> None:
        self._load()

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        return self._load().embed(texts)

    def _load(self) -> OnnxMiniLMEmbedder:
        embedder = self._embedder
        if embedder is not None:
            return embedder
        with self._lock:  # concurrent first requests wait for one download instead of racing
            if self._embedder is None:
                try:
                    loaded = OnnxMiniLMEmbedder(ensure_model(*self._args))
                    if loaded.dimension != self.dimension:
                        raise ModelUnavailableError(
                            f"model produces {loaded.dimension}-dimensional vectors, expected {self.dimension}"
                        )
                except ModelUnavailableError as exc:
                    self.last_error = str(exc)
                    log.error("embedding model unavailable: %s", exc)
                    raise
                self._embedder, self.last_error = loaded, None
            return self._embedder
