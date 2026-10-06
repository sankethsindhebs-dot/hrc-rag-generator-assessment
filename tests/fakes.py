"""Deterministic offline embedder: hashed bag-of-words, so cosine ~ lexical overlap.

It is NOT a stand-in for a semantic model's score scale; it lets tests exercise the
pipeline and gate mechanics without a network or model download.
"""
import hashlib
import re

import numpy as np

STOP = {"the", "a", "an", "of", "to", "in", "on", "is", "are", "and", "or", "for", "it",
        "how", "what", "who", "do", "does", "i", "my", "be", "by", "with", "as", "at", "that",
        "this", "must", "should", "can", "will", "all", "if", "then"}
DIM = 1 << 16  # large enough that hash collisions do not create spurious similarity


class HashEmbedder:
    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(DIM, dtype=np.float32)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            if tok in STOP:
                continue
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIM] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts):
        return np.array([self._vec(t) for t in texts], dtype=np.float32).reshape(len(texts), DIM)

    def embed_query(self, text):
        return self._vec(text)
