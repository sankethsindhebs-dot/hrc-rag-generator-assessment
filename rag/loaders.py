"""Turn uploaded bytes (PDF/TXT) into per-page text."""
import io
from dataclasses import dataclass
from pathlib import PurePath

from pypdf import PdfReader
from pypdf.errors import PyPdfError


class DocumentError(ValueError):
    """The uploaded file cannot be used; the message is safe to show a user."""


@dataclass(frozen=True)
class Page:
    number: int  # 1-based
    text: str


@dataclass(frozen=True)
class Document:
    name: str
    pages: list[Page]


def load_document(filename: str, data: bytes) -> Document:
    name = PurePath(filename).name
    ext = PurePath(name).suffix.lower()
    if ext == ".txt":
        pages = _load_txt(data)
    elif ext == ".pdf":
        pages = _load_pdf(name, data)
    else:
        raise DocumentError(f"{name}: unsupported file type '{ext}' (use .pdf or .txt)")
    pages = [p for p in pages if p.text.strip()]
    if not pages:
        raise DocumentError(f"{name}: no extractable text (scanned PDFs need OCR, which is not supported)")
    return Document(name=name, pages=pages)


def _load_txt(data: bytes) -> list[Page]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    return [Page(1, text)]


def _load_pdf(name: str, data: bytes) -> list[Page]:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError(f"{name}: encrypted PDFs are not supported")
        return [Page(i, page.extract_text() or "") for i, page in enumerate(reader.pages, start=1)]
    except DocumentError:
        raise
    except (PyPdfError, ValueError, OSError, KeyError) as exc:
        raise DocumentError(f"{name}: could not read PDF ({type(exc).__name__})") from exc
