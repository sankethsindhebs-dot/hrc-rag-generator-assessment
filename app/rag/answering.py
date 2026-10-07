"""The question-answering flow: retrieve -> gate -> generate -> validate.

    1. Retrieve from ONE collection only (CollectionManager.query embeds the question).
    2. Retrieval-confidence gate: if no retrieved passage reaches ``min_score``, return the
       fixed "insufficient context" answer WITHOUT calling the generator.
    3. Otherwise send the generator ONLY the passages that passed the gate.
    4. The generator may itself decline ("grounded": false): second layer of refusal, for
       passages that are on-topic but do not contain the answer (similarity cannot see this).
    5. Validate the generator's output. Citations may only refer to passages that were sent;
       invented ones are dropped, and a "grounded" answer left with no valid citation is
       withheld rather than shown.

Everything here is independent of the generator in use, so the guarantees hold for any
Generator implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.config import Settings
from app.rag.collections import CollectionManager
from app.rag.errors import GenerationUnavailableError, InvalidInputError
from app.rag.generation import GenerationResult, Generator, Passage
from app.rag.store import SearchHit

INSUFFICIENT_MESSAGE = "I could not find enough information in this collection's documents to answer that question."
UNVERIFIED_MESSAGE = "An answer was generated but could not be verified against the documents, so it was withheld."

_MARKER_RE = re.compile(r"\[S\d+\]")
_MAX_DETAIL = 500
MAX_QUESTION_CHARS = 2000  # the embedder only reads ~256 tokens; the rest would just be sent to the model

Status = Literal["answered", "insufficient_context", "unverified"]
Reason = Literal["empty_collection", "below_threshold", "model_declined", "no_valid_citations"]


@dataclass(frozen=True)
class Evidence:
    """A retrieved passage with its similarity score. ``label`` is set only if it was sent
    to the generator."""

    chunk_id: str
    document_id: str
    filename: str
    chunk_index: int
    text: str
    score: float
    label: str | None


@dataclass(frozen=True)
class Citation:
    label: str
    chunk_id: str
    document_id: str
    filename: str
    chunk_index: int
    text: str
    score: float


@dataclass(frozen=True)
class Answer:
    question: str
    status: Status
    grounded: bool
    answer: str
    citations: tuple[Citation, ...]
    evidence: tuple[Evidence, ...]  # everything retrieved, best first
    generator_called: bool
    threshold: float
    reason: Reason | None = None
    detail: str = ""  # the model's own explanation when it declined (never shown as the answer)
    warnings: tuple[str, ...] = ()


class AnswerService:
    def __init__(self, manager: CollectionManager, generator: Generator, settings: Settings):
        self._manager = manager
        self._generator = generator
        self._settings = settings

    @property
    def generation_available(self) -> bool:
        return self._generator.available

    def ask(
        self,
        collection_id: str,
        question: str,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> Answer:
        threshold = self._settings.min_score if min_score is None else min_score
        if not 0.0 <= threshold <= 1.0:
            raise InvalidInputError("min_score must be between 0 and 1")

        question = (question or "").strip()
        if len(question) > MAX_QUESTION_CHARS:
            raise InvalidInputError(f"question is too long ({len(question)} characters; the limit is {MAX_QUESTION_CHARS})")
        hits = self._manager.query(collection_id, question, top_k)  # one collection, nothing else
        passing = [h for h in hits if h.score >= threshold]
        labels = {h.chunk.id: f"S{i + 1}" for i, h in enumerate(passing)}
        evidence = tuple(_evidence(h, labels.get(h.chunk.id)) for h in hits)

        def insufficient(reason: Reason, called: bool = False, detail: str = "") -> Answer:
            return Answer(question, "insufficient_context", False, INSUFFICIENT_MESSAGE, (), evidence,
                          called, threshold, reason, detail)

        if not hits:
            return insufficient("empty_collection")
        if not passing:  # layer 1: the retrieval gate; the generator is never called
            return insufficient("below_threshold")

        if not self._generator.available:
            raise GenerationUnavailableError(
                getattr(self._generator, "reason", "Answer generation is unavailable."), evidence
            )

        passages = [Passage(labels[h.chunk.id], h.chunk.filename, h.chunk.text) for h in passing]
        result = self._generator.generate(question, passages)  # GenerationError propagates

        if not result.grounded:  # layer 2: the model itself says the passages are not enough
            return insufficient("model_declined", called=True, detail=_clean_detail(result.answer))
        return self._validate(question, result, passing, labels, evidence, threshold)

    def _validate(
        self,
        question: str,
        result: GenerationResult,
        passing: list[SearchHit],
        labels: dict[str, str],
        evidence: tuple[Evidence, ...],
        threshold: float,
    ) -> Answer:
        by_label = {labels[h.chunk.id]: h for h in passing}
        warnings: list[str] = []

        # Labels the model claimed: from the citations list and from [S#] markers in the text.
        listed = [c for c in result.cited_labels]
        in_text = _MARKER_RE.findall(result.answer)
        claimed = [m[1:-1] for m in in_text] + listed

        valid: list[str] = []
        for label in claimed:
            if label in by_label and label not in valid:
                valid.append(label)
            elif label not in by_label:
                note = f"discarded citation {label[:40]!r}: not a retrieved passage"
                if note not in warnings:
                    warnings.append(note)

        text = _MARKER_RE.sub(lambda m: m.group(0) if m.group(0)[1:-1] in by_label else "", result.answer)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()

        if not valid or not text:
            return Answer(question, "unverified", False, UNVERIFIED_MESSAGE, (), evidence, True, threshold,
                          "no_valid_citations", warnings=tuple(warnings))

        citations = tuple(_citation(label, by_label[label]) for label in valid)
        return Answer(question, "answered", True, text, citations, evidence, True, threshold,
                      None, warnings=tuple(warnings))


def _evidence(hit: SearchHit, label: str | None) -> Evidence:
    c = hit.chunk
    return Evidence(c.id, c.document_id, c.filename, c.index, c.text, hit.score, label)


def _citation(label: str, hit: SearchHit) -> Citation:
    c = hit.chunk
    return Citation(label, c.id, c.document_id, c.filename, c.index, c.text, hit.score)


def _clean_detail(text: str) -> str:
    text = _MARKER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()[:_MAX_DETAIL]
