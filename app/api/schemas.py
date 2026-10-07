"""Request and response models. Presentation concerns only (e.g. snippets); no business logic."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.rag.answering import Answer, Citation, Evidence
from app.rag.collections import CollectionInfo, DocumentInfo

SNIPPET_CHARS = 500


def snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


class CreateCollectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str
    top_k: int | None = Field(default=None, ge=1, le=20)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)


class CollectionOut(BaseModel):
    id: str
    name: str
    created_at: str
    document_count: int
    chunk_count: int


class DocumentOut(BaseModel):
    document_id: str
    filename: str
    chunk_count: int


class RejectedOut(BaseModel):
    filename: str
    code: str
    message: str


class UploadOut(BaseModel):
    indexed: list[DocumentOut]
    rejected: list[RejectedOut]


class EvidenceOut(BaseModel):
    chunk_id: str
    document_id: str
    filename: str
    chunk_index: int
    snippet: str
    score: float
    label: str | None
    used: bool  # True if this passage was sent to the generator


class CitationOut(BaseModel):
    label: str
    chunk_id: str
    document_id: str
    filename: str
    chunk_index: int
    snippet: str
    score: float


class AskOut(BaseModel):
    question: str
    status: Literal["answered", "insufficient_context", "unverified"]
    grounded: bool
    answer: str
    reason: str | None
    detail: str
    citations: list[CitationOut]
    evidence: list[EvidenceOut]
    generator_called: bool
    threshold: float
    warnings: list[str]


def collection_out(info: CollectionInfo) -> CollectionOut:
    return CollectionOut(**vars(info))


def document_out(info: DocumentInfo) -> DocumentOut:
    return DocumentOut(**vars(info))


def evidence_out(e: Evidence) -> EvidenceOut:
    return EvidenceOut(chunk_id=e.chunk_id, document_id=e.document_id, filename=e.filename,
                       chunk_index=e.chunk_index, snippet=snippet(e.text), score=round(e.score, 4),
                       label=e.label, used=e.label is not None)


def citation_out(c: Citation) -> CitationOut:
    return CitationOut(label=c.label, chunk_id=c.chunk_id, document_id=c.document_id, filename=c.filename,
                       chunk_index=c.chunk_index, snippet=snippet(c.text), score=round(c.score, 4))


def ask_out(a: Answer) -> AskOut:
    return AskOut(question=a.question, status=a.status, grounded=a.grounded, answer=a.answer, reason=a.reason,
                  detail=a.detail, citations=[citation_out(c) for c in a.citations],
                  evidence=[evidence_out(e) for e in a.evidence], generator_called=a.generator_called,
                  threshold=a.threshold, warnings=list(a.warnings))
