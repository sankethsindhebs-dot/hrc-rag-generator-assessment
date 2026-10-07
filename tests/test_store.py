import numpy as np
import pytest

from app.rag.errors import InvalidInputError
from app.rag.store import Chunk, VectorStore


def make_chunks(n, prefix="c"):
    return [Chunk(f"{prefix}{i}", "doc", "f.txt", i, f"text {i}", 0, 5) for i in range(n)]


def basis(dim, *indices):
    m = np.zeros((len(indices), dim), dtype=np.float32)
    for row, i in enumerate(indices):
        m[row, i] = 1.0
    return m


def built(dim, chunks, vectors):
    return VectorStore(dim).with_added(chunks, vectors)


def test_empty_store_returns_no_hits():
    assert VectorStore(3).search(np.array([1, 0, 0]), 5) == []


def test_search_ranks_by_cosine_similarity():
    vecs = np.array([[1, 0, 0], [0.8, 0.6, 0], [0, 0, 1]], dtype=np.float32)
    store = built(3, make_chunks(3), vecs)
    hits = store.search(np.array([1, 0, 0]), 3)
    assert [h.chunk.id for h in hits] == ["c0", "c1", "c2"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.8)
    assert hits[2].score == pytest.approx(0.0)


def test_scores_are_scale_invariant():
    store = built(2, make_chunks(1), np.array([[10.0, 0.0]]))
    assert store.search(np.array([0.001, 0.0]), 1)[0].score == pytest.approx(1.0)


def test_k_larger_than_store_returns_everything():
    store = built(2, make_chunks(2), basis(2, 0, 1))
    assert len(store.search(np.array([1, 0]), 50)) == 2


def test_k_limits_results():
    store = built(4, make_chunks(4), basis(4, 0, 1, 2, 3))
    assert len(store.search(np.array([1, 1, 1, 1]), 2)) == 2


def test_ties_resolve_in_insertion_order():
    store = built(2, make_chunks(3), np.array([[1, 0], [1, 0], [1, 0]], dtype=np.float32))
    assert [h.chunk.id for h in store.search(np.array([1, 0]), 3)] == ["c0", "c1", "c2"]


def test_with_added_is_cumulative_and_does_not_mutate_the_original():
    first = built(2, make_chunks(1, "a"), basis(2, 0))
    second = first.with_added(make_chunks(1, "b"), basis(2, 1))
    assert len(first) == 1 and len(second) == 2
    assert [c.id for c in first.chunks] == ["a0"]
    assert [h.chunk.id for h in first.search(np.array([0, 1]), 5)] == ["a0"]  # old snapshot unaffected


def test_adding_nothing_returns_an_equivalent_store():
    store = built(2, make_chunks(1), basis(2, 0))
    assert len(store.with_added([], np.zeros((0, 2)))) == 1


def test_stored_vectors_cannot_be_mutated_from_outside():
    store = built(2, make_chunks(1), basis(2, 0))
    with pytest.raises(ValueError):
        store._vectors[0, 0] = 5.0


def test_zero_vector_scores_zero_without_nan():
    store = built(2, make_chunks(1), np.zeros((1, 2), dtype=np.float32))
    assert store.search(np.array([1, 0]), 1)[0].score == 0.0


def test_rejects_bad_shapes_and_values_and_leaves_store_unchanged():
    store = VectorStore(3)
    with pytest.raises(InvalidInputError):
        store.with_added(make_chunks(2), basis(3, 0))  # 2 chunks, 1 vector
    with pytest.raises(InvalidInputError):
        store.with_added(make_chunks(1), np.ones((1, 4)))  # wrong dimension
    with pytest.raises(InvalidInputError):
        store.with_added(make_chunks(1), np.array([[np.nan, 0, 0]]))
    with pytest.raises(InvalidInputError):
        store.search(np.ones(5), 1)
    with pytest.raises(InvalidInputError):
        store.search(np.ones(3), 0)
    assert len(store) == 0


def test_save_and_load_round_trip(tmp_path):
    store = built(3, make_chunks(3), basis(3, 0, 1, 2))
    store.save(tmp_path / "idx")
    loaded = VectorStore.load(tmp_path / "idx", 3)
    assert loaded.chunks == store.chunks
    assert [h.chunk.id for h in loaded.search(np.array([0, 1, 0]), 1)] == ["c1"]


def test_save_writes_a_single_index_file(tmp_path):
    built(2, make_chunks(1), basis(2, 0)).save(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.npz"]  # no .tmp, no second file


def test_load_missing_directory_gives_empty_store(tmp_path):
    assert len(VectorStore.load(tmp_path / "nope", 3)) == 0


def test_persisted_index_needs_no_pickle(tmp_path):
    built(2, make_chunks(1), basis(2, 0)).save(tmp_path)
    with np.load(tmp_path / "index.npz", allow_pickle=False) as data:  # raises if objects were pickled
        assert set(data.files) == {"format_version", "vectors", "chunks"}
