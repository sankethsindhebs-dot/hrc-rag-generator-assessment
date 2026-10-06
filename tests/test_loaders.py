import io

import pytest
from reportlab.pdfgen import canvas

from rag.loaders import DocumentError, load_document


def make_pdf(pages: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for text in pages:
        if text:
            c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_txt_utf8_and_bom():
    doc = load_document("a.txt", "﻿Café résumé".encode("utf-8"))
    assert doc.pages[0].text == "Café résumé" and doc.pages[0].number == 1


def test_txt_cp1252_fallback():
    doc = load_document("a.txt", "naïve".encode("cp1252"))
    assert "na" in doc.pages[0].text


def test_pdf_pages_numbered():
    doc = load_document("x.pdf", make_pdf(["first page text", "second page text"]))
    assert [p.number for p in doc.pages] == [1, 2]
    assert "second" in doc.pages[1].text


def test_pdf_blank_pages_skipped_but_numbers_kept():
    doc = load_document("x.pdf", make_pdf(["one", "", "three"]))
    assert [p.number for p in doc.pages] == [1, 3]


def test_extension_is_case_insensitive_and_path_stripped():
    assert load_document("dir/NOTES.TXT", b"hello").name == "NOTES.TXT"


def test_empty_txt_rejected():
    with pytest.raises(DocumentError, match="no extractable text"):
        load_document("a.txt", b"  \n ")


def test_pdf_without_text_rejected():
    with pytest.raises(DocumentError, match="no extractable text"):
        load_document("scan.pdf", make_pdf(["", ""]))


def test_corrupt_pdf_rejected():
    with pytest.raises(DocumentError, match="could not read PDF"):
        load_document("bad.pdf", b"this is not a pdf")


def test_unsupported_type_rejected():
    with pytest.raises(DocumentError, match="unsupported"):
        load_document("a.docx", b"data")
