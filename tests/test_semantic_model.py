"""Integration tests against the REAL ONNX MiniLM model.

The model is downloaded on first use into the git-ignored .cache/models directory.
If it cannot be fetched (offline machine), these tests are skipped, not faked.
Run only these with:  pytest -m model
"""

from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.rag.collections import CollectionManager
from app.rag.embeddings import OnnxMiniLMEmbedder, ensure_model
from app.rag.errors import ModelUnavailableError

pytestmark = pytest.mark.model
SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(scope="module")
def real_embedder():
    s = Settings.from_env()
    try:
        return OnnxMiniLMEmbedder(ensure_model(s.model_cache_dir, s.model_url, s.model_sha256))
    except ModelUnavailableError as exc:
        pytest.skip(f"embedding model unavailable: {exc}")


def cos(a, b):
    return float(np.dot(a, b))


def test_vectors_are_unit_length_with_expected_dimension(real_embedder):
    v = real_embedder.embed(["hello world", "a much longer sentence about telescopes and cafes"])
    assert v.shape == (2, 384) and real_embedder.dimension == 384
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-4)


def test_batching_does_not_change_embeddings(real_embedder):
    texts = ["short", "a considerably longer piece of text " * 8, "mid length sentence here"]
    together = real_embedder.embed(texts)
    separately = np.vstack([real_embedder.embed([t]) for t in texts])
    assert np.allclose(together, separately, atol=1e-4)  # padding must not leak into results


def test_empty_input_and_overlong_text(real_embedder):
    assert real_embedder.embed([]).shape == (0, 384)
    v = real_embedder.embed(["word " * 5000])  # far beyond the token window: truncated, not an error
    assert v.shape == (1, 384) and np.isfinite(v).all()


def test_embeddings_are_semantic_not_lexical(real_embedder):
    q, related, unrelated = real_embedder.embed(
        ["How do I fix a flat tire on my bicycle?", "Repairing a punctured bike wheel", "Quarterly tax filing deadlines"]
    )
    assert cos(q, related) > cos(q, unrelated) + 0.2  # no shared content words with `related`


def _ingest(manager, folder):
    c = manager.create_collection(folder.name)
    for path in sorted(folder.iterdir()):
        manager.add_document(c.id, path.name, path.read_bytes())
    return c


@pytest.fixture
def real_manager(tmp_path, real_embedder):
    s = Settings.from_env({"RAG_DATA_DIR": str(tmp_path / "data")})
    return CollectionManager(s, real_embedder)


@pytest.mark.parametrize(
    "folder,question,expected_file,expected_phrase",
    [
        ("harbor_light_cafe", "What time do I need to show up to open the shop?", "staff_handbook.md", "5:45"),
        ("harbor_light_cafe", "Can I get my money back for a muffin?", "customer_policies.txt", "refunded within one hour"),
        ("kestrel_telescope", "How much does the scope weigh?", "kestrel9_user_guide.md", "7.4 kilograms"),
        ("kestrel_telescope", "My stars look smeared like comets", "care_and_troubleshooting.txt", "collimation"),
    ],
)
def test_real_retrieval_finds_the_answering_chunk(real_manager, folder, question, expected_file, expected_phrase):
    c = _ingest(real_manager, SAMPLES / folder)
    hits = real_manager.query(c.id, question, top_k=3)
    assert any(expected_phrase.lower() in h.chunk.text.lower() for h in hits), [h.chunk.text[:60] for h in hits]
    assert hits[0].chunk.filename == expected_file


def test_real_isolation_between_sample_sets(real_manager):
    cafe = _ingest(real_manager, SAMPLES / "harbor_light_cafe")
    scope = _ingest(real_manager, SAMPLES / "kestrel_telescope")
    for hit in real_manager.query(cafe.id, "telescope mirror collimation", top_k=50):
        assert hit.chunk.filename in {"staff_handbook.md", "customer_policies.txt"}
    for hit in real_manager.query(scope.id, "espresso loyalty stamps", top_k=50):
        assert hit.chunk.filename in {"kestrel9_user_guide.md", "care_and_troubleshooting.txt"}


def test_score_gap_between_relevant_and_irrelevant_questions(real_manager):
    """Informational for later threshold calibration: on-topic scores should clearly beat off-topic."""
    c = _ingest(real_manager, SAMPLES / "kestrel_telescope")
    on = real_manager.query(c.id, "How do I clean the mirror?", top_k=1)[0].score
    off = real_manager.query(c.id, "Who won the 1998 football world cup?", top_k=1)[0].score
    print(f"\non-topic top score={on:.3f}  off-topic top score={off:.3f}")
    assert on > off


def test_lazy_embedder_downloads_verifies_loads_and_matches_the_direct_embedder(real_embedder):
    """The production default: nothing loads until first use, then it behaves like the direct embedder."""
    from app.rag.embeddings import MODEL_DIMENSION, MODEL_NAME, LazyOnnxEmbedder

    s = Settings.from_env()
    lazy = LazyOnnxEmbedder(s.model_cache_dir, s.model_url, s.model_sha256)
    assert lazy.loaded is False and lazy.name == MODEL_NAME and lazy.dimension == MODEL_DIMENSION
    vectors = lazy.embed(["Opening hours at the cafe"])
    assert lazy.loaded is True and lazy.last_error is None
    assert np.allclose(vectors, real_embedder.embed(["Opening hours at the cafe"]), atol=1e-5)
    assert lazy.dimension == real_embedder.dimension
