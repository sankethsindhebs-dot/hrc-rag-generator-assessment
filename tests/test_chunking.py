import pytest

from app.rag.chunking import chunk_text, normalize_text
from app.rag.errors import InvalidInputError

PARAGRAPHS = "\n\n".join(f"Paragraph {i}. " + "The quick brown fox jumps over the lazy dog. " * 4 for i in range(12))


def test_short_text_is_one_chunk():
    chunks = chunk_text("Just a short note.", size=200, overlap=20)
    assert [c.text for c in chunks] == ["Just a short note."]


def test_empty_and_blank_text_yield_no_chunks():
    assert chunk_text("", 100, 10) == []
    assert chunk_text("  \n\n  ", 100, 10) == []


def test_chunks_respect_size_limit():
    chunks = chunk_text(PARAGRAPHS, size=300, overlap=50)
    assert len(chunks) > 3
    assert all(0 < len(c.text) <= 300 for c in chunks)


def test_offsets_point_at_the_chunk_text():
    norm = normalize_text(PARAGRAPHS)
    for c in chunk_text(PARAGRAPHS, size=300, overlap=50):
        assert norm[c.start : c.end] == c.text


def test_every_character_is_covered():
    norm = normalize_text(PARAGRAPHS)
    chunks = chunk_text(PARAGRAPHS, size=300, overlap=50)
    covered = [False] * len(norm)
    for c in chunks:
        for i in range(c.start, c.end):
            covered[i] = True
    uncovered = [i for i, ok in enumerate(covered) if not ok and not norm[i].isspace()]
    assert uncovered == []


def test_consecutive_chunks_overlap_and_advance():
    chunks = chunk_text(PARAGRAPHS, size=300, overlap=60)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start > prev.start  # always makes progress
        assert nxt.start < prev.end  # and overlaps the previous chunk


def test_zero_overlap_is_allowed():
    chunks = chunk_text(PARAGRAPHS, size=300, overlap=0)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start >= prev.end - 1


def test_prefers_paragraph_boundaries():
    text = ("A" * 45 + " first.\n\n") + ("B" * 100)
    first = chunk_text(text, size=70, overlap=10)[0]
    assert first.text.endswith("first.")


def test_long_unbroken_token_does_not_loop_forever():
    chunks = chunk_text("x" * 1000, size=100, overlap=20)
    assert len(chunks) >= 10
    assert all(len(c.text) <= 100 for c in chunks)


def test_chunks_do_not_start_mid_word():
    text = " ".join(f"word{i:03d}" for i in range(200))
    for c in chunk_text(text, size=100, overlap=30)[1:]:
        assert c.text.startswith("word")


def test_unicode_text():
    text = "Zażółć gęślą jaźń. " * 60
    assert all(len(c.text) <= 120 for c in chunk_text(text, size=120, overlap=20))


def test_normalize_text():
    assert normalize_text("a \t b\r\n\r\n\r\n\r\nc  ") == "a b\n\nc"


@pytest.mark.parametrize("size,overlap", [(0, 0), (-5, 0), (100, 100), (100, 150), (100, -1)])
def test_invalid_parameters(size, overlap):
    with pytest.raises(InvalidInputError):
        chunk_text("some text", size, overlap)
