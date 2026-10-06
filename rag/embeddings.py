"""Embedder interface plus the local FastEmbed implementation."""
from typing import Protocol

import numpy as np


class EmbeddingError(RuntimeError):
    """The embedding model could not be loaded or run (e.g. the download is blocked)."""


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.where(norms == 0, 1, norms)


class FastEmbedEmbedder:
    """Local ONNX embeddings. The model is downloaded on first use (needs huggingface.co)."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.model_name = model_name
        self._model = None

    def _get(self):
        if self._model is None:
            from fastembed import TextEmbedding  # lazy: import and download only when used
            try:
                self._model = TextEmbedding(self.model_name)
            except Exception as exc:  # download/network/ONNX failures all mean "no embedder"
                raise EmbeddingError(
                    f"could not load embedding model '{self.model_name}' ({type(exc).__name__}); "
                    "it is downloaded on first use and needs access to huggingface.co"
                ) from exc
        return self._model

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return _normalize(np.array(list(self._get().embed(texts))))

    def embed_query(self, text: str) -> np.ndarray:
        # query_embed applies the model's query instruction prefix where one exists
        return _normalize(np.array(list(self._get().query_embed(text)))[0])
