import pytest

from rag.config import Settings
from rag.loaders import DocumentError
from rag.pipeline import build_collection, retrieve
from tests.conftest import fixture_bytes
from tests.fixtures.questions import ANSWERABLE, POLICY, SOURDOUGH, UNANSWERABLE
from tests.test_loaders import make_pdf


def test_build_collection_metadata(both_collection):
    assert both_collection.doc_names == [POLICY, SOURDOUGH]
    assert len(both_collection.chunks) > 4
    assert [c.id for c in both_collection.chunks] == list(range(len(both_collection.chunks)))
    assert both_collection.vectors.shape[0] == len(both_collection.chunks)


@pytest.mark.parametrize("question,expected_doc", ANSWERABLE)
def test_answerable_question_retrieves_right_document(both_collection, embedder, settings, question, expected_doc):
    r = retrieve(both_collection, question, embedder, settings)
    assert r.hits[0].chunk.doc == expected_doc
    assert r.passes_gate


def test_retrieval_finds_the_specific_chunk(both_collection, embedder, settings):
    r = retrieve(both_collection, "How much is the monthly internet stipend?", embedder, settings)
    assert "fifty dollars" in r.hits[0].chunk.text


def test_gate_separates_answerable_from_unanswerable_on_fixtures(both_collection, embedder, settings):
    """Calibration check for the fake embedder: configured threshold must sit in the gap."""
    good = [retrieve(both_collection, q, embedder, settings).top_score for q, _ in ANSWERABLE]
    bad = [retrieve(both_collection, q, embedder, settings).top_score for q in UNANSWERABLE]
    assert max(bad) < settings.min_score <= min(good), (sorted(bad), sorted(good), settings.min_score)


def test_gate_threshold_is_configurable(both_collection, embedder):
    q = "Who is eligible to work remotely?"
    lenient = Settings(chunk_size=300, chunk_overlap=40, top_k=3, min_score=0.0)
    strict = Settings(chunk_size=300, chunk_overlap=40, top_k=3, min_score=0.99)
    assert retrieve(both_collection, q, embedder, lenient).passes_gate
    assert not retrieve(both_collection, q, embedder, strict).passes_gate


def test_same_code_different_document_sets_are_isolated(embedder, settings):
    only_policy = build_collection([fixture_bytes(POLICY)], embedder, settings)
    only_bread = build_collection([fixture_bytes(SOURDOUGH)], embedder, settings)
    assert only_policy.id != only_bread.id
    q = "How often should I feed my sourdough starter?"
    assert retrieve(only_bread, q, embedder, settings).passes_gate
    assert not retrieve(only_policy, q, embedder, settings).passes_gate  # unrelated set: gate rejects
    assert {h.chunk.doc for h in retrieve(only_policy, q, embedder, settings).hits} == {POLICY}


def test_pdf_and_txt_mixed_with_page_metadata(embedder, settings):
    pdf = make_pdf(["Quarterly revenue grew by twelve percent.", "The Zephyr project launches in March."])
    col = build_collection([("report.pdf", pdf), fixture_bytes(POLICY)], embedder, settings)
    r = retrieve(col, "When does the Zephyr project launch?", embedder, settings)
    assert (r.hits[0].chunk.doc, r.hits[0].chunk.page) == ("report.pdf", 2)


def test_no_files_rejected(embedder, settings):
    with pytest.raises(DocumentError, match="no files"):
        build_collection([], embedder, settings)


def test_one_bad_file_fails_loudly_naming_it(embedder, settings):
    with pytest.raises(DocumentError, match="bad.pdf"):
        build_collection([fixture_bytes(POLICY), ("bad.pdf", b"nope")], embedder, settings)


def test_oversized_file_rejected(embedder):
    s = Settings(max_file_mb=0)
    with pytest.raises(DocumentError, match="larger than"):
        build_collection([("a.txt", b"hello")], embedder, s)
