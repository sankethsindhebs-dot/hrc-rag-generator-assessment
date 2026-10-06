import numpy as np

from rag.store import Chunk, Collection


def make(vecs):
    chunks = [Chunk(i, "d.txt", 1, f"c{i}") for i in range(len(vecs))]
    return Collection(chunks=chunks, vectors=np.array(vecs, dtype=np.float32))


def test_search_orders_by_cosine():
    col = make([[1, 0], [0.6, 0.8], [0, 1]])
    hits = col.search(np.array([0, 1], dtype=np.float32), 3)
    assert [h.chunk.id for h in hits] == [2, 1, 0]
    assert hits[0].score == 1.0


def test_k_larger_than_corpus_and_empty():
    assert len(make([[1, 0]]).search(np.array([1, 0], dtype=np.float32), 10)) == 1
    empty = Collection(chunks=[], vectors=np.zeros((0, 2), dtype=np.float32))
    assert empty.search(np.array([1, 0], dtype=np.float32), 3) == []


def test_collection_ids_are_unique():
    assert make([[1, 0]]).id != make([[1, 0]]).id
