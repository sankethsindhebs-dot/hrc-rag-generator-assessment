import pytest

from rag.chunking import chunk_text


def test_short_text_is_one_chunk():
    assert chunk_text("Hello world.", 100, 10) == ["Hello world."]


def test_empty_and_whitespace_yield_no_chunks():
    assert chunk_text("", 100, 10) == []
    assert chunk_text("  \n\n \n ", 100, 10) == []


def test_paragraphs_packed_and_size_bounded():
    text = "\n\n".join(f"Paragraph number {i} with some filler words here." for i in range(20))
    chunks = chunk_text(text, 200, 30)
    assert len(chunks) > 1
    assert all(0 < len(c) <= 200 + 30 + 1 for c in chunks)


def test_overlap_carries_text_forward():
    text = "\n\n".join(f"Sentence about topic{i} " + "x" * 60 for i in range(6))
    chunks = chunk_text(text, 150, 40)
    assert len(chunks) > 1
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.split()[0] in prev  # first word of next chunk was in previous one


def test_no_overlap_option():
    text = "\n\n".join(["alpha " * 20, "beta " * 20, "gamma " * 20])
    chunks = chunk_text(text, 130, 0)
    assert len(chunks) == 3
    assert "alpha" not in chunks[1]


def test_all_words_preserved():
    words = [f"w{i}" for i in range(500)]
    chunks = chunk_text(" ".join(words), 120, 20)
    seen = {w for c in chunks for w in c.split()}
    assert set(words) <= seen


def test_unbroken_long_token_is_hard_split():
    chunks = chunk_text("a" * 1000, 100, 10)
    assert len(chunks) >= 10
    assert all(len(c) <= 111 for c in chunks)


@pytest.mark.parametrize("size,overlap", [(0, 0), (10, 10), (10, -1)])
def test_invalid_params(size, overlap):
    with pytest.raises(ValueError):
        chunk_text("text", size, overlap)
