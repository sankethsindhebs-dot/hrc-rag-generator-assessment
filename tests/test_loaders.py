import pytest

from app.rag.errors import DocumentLoadError, EmptyDocumentError, UnsupportedDocumentError
from app.rag.loaders import load_text
from tests.helpers import make_blank_pdf, make_docx, make_pdf


def test_plain_text_and_markdown():
    assert load_text("a.txt", b"hello world") == "hello world"
    assert "# Title" in load_text("README.MD", b"# Title\n\nbody")  # extension is case-insensitive


def test_utf8_bom_is_stripped():
    assert load_text("a.txt", "﻿café".encode("utf-8")) == "café"


def test_legacy_windows_1252_fallback():
    assert load_text("a.txt", "café".encode("cp1252")) == "café"


def test_binary_data_is_rejected_even_with_text_extension():
    with pytest.raises(DocumentLoadError, match="binary"):
        load_text("a.txt", b"abc\x00def")


def test_html_strips_scripts_and_styles_but_keeps_text():
    html = b"<html><head><style>p{color:red}</style><script>alert(1)</script></head><body><h1>Hi</h1><p>there</p></body></html>"
    text = load_text("page.html", html)
    assert "Hi" in text and "there" in text
    assert "alert" not in text and "color" not in text


def test_pdf_text_extraction():
    assert "Quarterly refund policy" in load_text("doc.pdf", make_pdf("Quarterly refund policy"))


def test_pdf_with_no_text_reports_ocr_limitation():
    with pytest.raises(EmptyDocumentError, match="OCR"):
        load_text("scan.pdf", make_blank_pdf())


def test_corrupt_pdf():
    with pytest.raises(DocumentLoadError):
        load_text("bad.pdf", b"%PDF-1.4 this is not really a pdf")


def test_docx_paragraphs_and_tables():
    data = make_docx(["First paragraph.", "Second paragraph."], table=[["Item", "Cost"], ["Widget", "5"]])
    text = load_text("doc.docx", data)
    assert "First paragraph." in text and "Second paragraph." in text
    assert "Widget | 5" in text


def test_corrupt_docx():
    with pytest.raises(DocumentLoadError):
        load_text("bad.docx", b"not a zip file")


@pytest.mark.parametrize("name", ["notes.exe", "archive.zip", "noextension"])
def test_unsupported_types(name):
    with pytest.raises(UnsupportedDocumentError):
        load_text(name, b"data")


@pytest.mark.parametrize("data", [b"", b"   \n\t  "])
def test_empty_documents(data):
    with pytest.raises(EmptyDocumentError):
        load_text("a.txt", data)
