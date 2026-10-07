"""Split text into overlapping, boundary-aware chunks with character offsets."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.rag.errors import InvalidInputError


@dataclass(frozen=True)
class TextChunk:
    text: str
    start: int  # offsets into normalize_text(original)
    end: int


def normalize_text(text: str) -> str:
    """Canonical whitespace so chunk offsets are stable: LF newlines, no runs of blanks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int = 800, overlap: int = 150) -> list[TextChunk]:
    """Greedy windows of at most ``size`` characters, ending at a paragraph, sentence or
    word boundary where one is reasonably close, each starting ``overlap`` characters
    before the previous one ended."""
    if size <= 0:
        raise InvalidInputError("chunk size must be positive")
    if not 0 <= overlap < size:
        raise InvalidInputError("chunk overlap must be >= 0 and smaller than chunk size")

    text = normalize_text(text)
    chunks: list[TextChunk] = []
    n = len(text)
    start = 0
    while start < n:
        hard_end = min(start + size, n)
        end = hard_end if hard_end == n else _find_break(text, start, hard_end, size)
        piece = text[start:end]
        stripped = piece.strip()
        if stripped:
            lead = len(piece) - len(piece.lstrip())
            chunks.append(TextChunk(stripped, start + lead, start + lead + len(stripped)))
        if end >= n:
            break
        next_start = _snap_to_word_start(text, max(end - overlap, start + 1), end)
        start = next_start if next_start > start else end
    return chunks


def _find_break(text: str, start: int, hard_end: int, size: int) -> int:
    """Prefer a paragraph break, then a sentence end, then whitespace, in the last 40%
    of the window; fall back to cutting at the hard limit."""
    lo = start + int(size * 0.6)
    window = text[lo:hard_end]
    for marker in ("\n\n", "\n", ". ", "? ", "! ", " "):
        idx = window.rfind(marker)
        if idx != -1:
            return lo + idx + len(marker)
    return hard_end


def _snap_to_word_start(text: str, pos: int, limit: int) -> int:
    """Move ``pos`` forward to the start of a word so chunks don't begin mid-word."""
    if pos <= 0 or pos >= len(text) or text[pos - 1].isspace():
        return pos
    while pos < limit and not text[pos].isspace():
        pos += 1
    while pos < limit and text[pos].isspace():
        pos += 1
    return pos
