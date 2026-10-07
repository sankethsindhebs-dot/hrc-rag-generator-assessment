"""Guards the default retrieval threshold against the calibration set, with the REAL embedding model.

If someone changes the model, the chunking, or RAG_MIN_SCORE's default, this tells them whether the
gate still separates unrelated questions from answerable ones. Run with:  pytest -m model
"""

import json
from pathlib import Path

import pytest

from app.config import DEFAULT_MIN_SCORE, Settings
from app.rag.collections import CollectionManager
from app.rag.embeddings import OnnxMiniLMEmbedder, ensure_model
from app.rag.errors import ModelUnavailableError

pytestmark = pytest.mark.model
SAMPLES = Path(__file__).resolve().parent.parent / "samples"
ANSWERABLE_KINDS = ("answerable", "paraphrase", "exact_term")


@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    settings = Settings.from_env({"RAG_DATA_DIR": str(tmp_path_factory.mktemp("calibration"))})
    try:
        embedder = OnnxMiniLMEmbedder(ensure_model(settings.model_cache_dir, settings.model_url, settings.model_sha256))
    except ModelUnavailableError as exc:
        pytest.skip(f"embedding model unavailable: {exc}")
    manager = CollectionManager(settings, embedder)
    ids = {}
    for folder in sorted(p for p in SAMPLES.iterdir() if p.is_dir()):
        ids[folder.name] = manager.create_collection(folder.name).id
        for doc in sorted(folder.iterdir()):
            manager.add_document(ids[folder.name], doc.name, doc.read_bytes())
    out = []
    for q in json.loads((SAMPLES / "calibration.json").read_text("utf-8"))["questions"]:
        for target in (ids if q["collection"] == "*" else [q["collection"]]):
            hits = manager.query(ids[target], q["question"], top_k=settings.top_k)
            phrase = q.get("expected_phrase")
            out.append({
                "category": q["category"], "question": q["question"], "top1": hits[0].score,
                "recalled": phrase is None or any(phrase.lower() in h.chunk.text.lower() for h in hits),
            })
    return out


def test_default_threshold_rejects_almost_all_unrelated_questions(rows):
    unrelated = [r for r in rows if r["category"] == "unrelated"]
    rejected = sum(r["top1"] < DEFAULT_MIN_SCORE for r in unrelated)
    assert rejected >= len(unrelated) - 2, [(r["question"], round(r["top1"], 3)) for r in unrelated if r["top1"] >= DEFAULT_MIN_SCORE]


def test_default_threshold_keeps_most_answerable_questions(rows):
    answerable = [r for r in rows if r["category"] in ANSWERABLE_KINDS]
    kept = sum(r["top1"] >= DEFAULT_MIN_SCORE for r in answerable)
    assert kept >= len(answerable) - 4, [(r["question"], round(r["top1"], 3)) for r in answerable if r["top1"] < DEFAULT_MIN_SCORE]


def test_answer_bearing_text_is_almost_always_retrieved(rows):
    answerable = [r for r in rows if r["category"] in ANSWERABLE_KINDS]
    assert sum(r["recalled"] for r in answerable) >= len(answerable) - 2


def test_related_but_unanswered_questions_are_NOT_separable_by_similarity(rows):
    """Documents a limitation rather than a goal: on-topic questions the documents cannot answer score
    like answerable ones, so the gate lets them through and the generator's own refusal must catch them."""
    related = [r["top1"] for r in rows if r["category"] == "related_unanswered"]
    answerable = [r["top1"] for r in rows if r["category"] in ANSWERABLE_KINDS]
    passing = sum(s >= DEFAULT_MIN_SCORE for s in related)
    print(f"\nrelated-but-unanswered passing the gate: {passing}/{len(related)}")
    assert passing >= len(related) - 1  # i.e. the gate does not (and cannot) reject these
    assert max(related) > min(answerable)  # their scores overlap the answerable range
