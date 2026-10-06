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


class ScriptedLLM:
    """Returns a fixed reply (str) or computes one from (system, user). Records every call."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple[str, str, dict]] = []

    def generate(self, system, user, schema):
        self.calls.append((system, user, schema))
        return self.reply(system, user) if callable(self.reply) else self.reply


SOURCE_RE = re.compile(r'<source id="(S\d+)"[^>]*>\n(.*?)\n</source>', re.S)
QUESTION_RE = re.compile(r"Question: (.*)$", re.S)


class EvidenceLLM:
    """Deterministic stand-in for an honest model: cites the source sharing the most
    content words with the question, and declines if fewer than two words overlap."""

    def __init__(self):
        self.calls = 0

    @staticmethod
    def _words(text):
        return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP}

    def generate(self, system, user, schema):
        import json

        self.calls += 1
        q = self._words(QUESTION_RE.search(user).group(1))
        best, best_overlap = None, 0
        for label, text in SOURCE_RE.findall(user):
            overlap = len(q & self._words(text))
            if overlap > best_overlap:
                best, best_overlap = (label, text), overlap
        if best is None or best_overlap < 2:
            return json.dumps({"sufficient": False, "answer": "", "citations": []})
        label, text = best
        return json.dumps({"sufficient": True, "answer": f"{text[:120]} [{label}]", "citations": [label]})
