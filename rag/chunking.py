"""Paragraph-aware chunking with a small character overlap."""
import re


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    """Pack paragraphs into chunks of about `size` chars.

    Paragraphs are kept whole when they fit; longer ones are split on whitespace.
    Each chunk after the first starts with the last `overlap` chars (word-aligned)
    of the previous one, so a chunk may reach size + overlap.
    """
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("require size > 0 and 0 <= overlap < size")
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if para:
            units.extend(_split_long(para, size))

    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + 1 + len(unit) > size:
            chunks.append(current)
            current = (_tail(current, overlap) + " " + unit).strip()
        else:
            current = f"{current} {unit}".strip() if current else unit
    if current:
        chunks.append(current)
    return chunks


def _split_long(para: str, size: int) -> list[str]:
    if len(para) <= size:
        return [para]
    out, current = [], ""
    for word in para.split(" "):
        while len(word) > size:  # unbroken string: hard split
            if current:
                out.append(current)
                current = ""
            out.append(word[:size])
            word = word[size:]
        if current and len(current) + 1 + len(word) > size:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip() if current else word
    if current:
        out.append(current)
    return out


def _tail(text: str, overlap: int) -> str:
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    tail = text[-overlap:]
    cut = tail.find(" ")
    return tail[cut + 1:] if cut != -1 else tail
