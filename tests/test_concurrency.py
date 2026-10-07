"""Concurrency regressions: per-collection locking, lock-free embedding, no lost updates."""

import threading
import time
from pathlib import Path

import pytest

from app.rag.collections import CollectionManager
from app.rag.errors import CollectionNotFoundError
from app.rag.store import VectorStore
from tests.helpers import FakeEmbedder

CARS = b"The automobile engine in this car needs service. A truck is a vehicle too."
FRUIT = b"An apple orchard grows pear and banana fruit every year."
WAIT = 10  # generous ceiling so a broken test fails instead of hanging the suite


class Worker(threading.Thread):
    """Runs one call in a thread and records its result or exception."""

    def __init__(self, fn, *args):
        super().__init__(daemon=True)
        self._fn, self._args = fn, args
        self.result = self.error = None

    def run(self):
        try:
            self.result = self._fn(*self._args)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the asserting test
            self.error = exc

    def finished(self, timeout):
        self.join(timeout)
        return not self.is_alive()


def gate_saves_for(monkeypatch, collection_id, entered, release):
    """Make VectorStore.save block (while the caller holds the collection lock) for one collection."""
    original = VectorStore.save

    def gated(self, directory):
        if Path(directory).name == collection_id:
            entered.set()
            assert release.wait(WAIT)
        return original(self, directory)

    monkeypatch.setattr(VectorStore, "save", gated)


def test_concurrent_ingestion_into_one_collection_loses_nothing(settings, monkeypatch):
    manager = CollectionManager(settings, FakeEmbedder())
    original = VectorStore.save

    def slow_save(self, directory):
        time.sleep(0.01)  # widen the read-modify-write window so a missing lock would lose updates
        return original(self, directory)

    monkeypatch.setattr(VectorStore, "save", slow_save)
    c = manager.create_collection("c")
    n = 12
    barrier = threading.Barrier(n)

    def ingest(i):
        barrier.wait(WAIT)
        return manager.add_document(c.id, f"doc{i}.txt", (f"Document {i}. " + "The car engine runs. " * 30).encode())

    workers = [Worker(ingest, i) for i in range(n)]
    for w in workers:
        w.start()
    assert all(w.finished(WAIT) for w in workers)
    assert [w.error for w in workers if w.error] == []

    expected_chunks = sum(w.result.chunk_count for w in workers)
    info = manager.get_collection(c.id)
    assert info.document_count == n
    assert info.chunk_count == expected_chunks
    chunk_ids = [ch.id for ch in manager._stores[c.id].chunks]
    assert len(chunk_ids) == len(set(chunk_ids)) == expected_chunks

    # What is on disk agrees with what was published in memory.
    reloaded = CollectionManager(settings, FakeEmbedder())
    assert reloaded.get_collection(c.id) == info
    assert {d.filename for d in reloaded.list_documents(c.id)} == {f"doc{i}.txt" for i in range(n)}


def test_each_collection_has_its_own_lock(manager):
    a, b = manager.create_collection("a"), manager.create_collection("b")
    assert manager._lock_for(a.id) is manager._lock_for(a.id)
    assert manager._lock_for(a.id) is not manager._lock_for(b.id)


def test_unknown_ids_do_not_grow_the_lock_registry(manager):
    for bad in ["../x", "z" * 32, "0" * 32]:
        with pytest.raises(CollectionNotFoundError):
            manager._lock_for(bad)
    assert manager._locks == {}


def test_a_busy_collection_does_not_block_other_collections_or_readers(manager, monkeypatch):
    a, b = manager.create_collection("a"), manager.create_collection("b")
    entered, release = threading.Event(), threading.Event()
    gate_saves_for(monkeypatch, a.id, entered, release)

    first_writer = Worker(manager.add_document, a.id, "a1.txt", CARS)
    first_writer.start()
    assert entered.wait(WAIT)  # writer 1 now holds A's lock, mid-commit
    try:
        # A different collection is completely unaffected.
        other = Worker(manager.add_document, b.id, "b.txt", FRUIT)
        other.start()
        assert other.finished(WAIT) and other.error is None
        assert manager.get_collection(b.id).document_count == 1

        # Readers of A see the last PUBLISHED snapshot without waiting for the writer.
        reader = Worker(manager.query, a.id, "car")
        reader.start()
        assert reader.finished(WAIT) and reader.error is None and reader.result == []

        # A second writer to the SAME collection must wait for the first.
        second_writer = Worker(manager.add_document, a.id, "a2.txt", CARS)
        second_writer.start()
        assert not second_writer.finished(0.3)
    finally:
        release.set()
    assert first_writer.finished(WAIT) and second_writer.finished(WAIT)
    assert first_writer.error is None and second_writer.error is None
    assert manager.get_collection(a.id).document_count == 2  # neither update was lost


def test_embedding_runs_outside_the_collection_lock(settings):
    entered, release = threading.Event(), threading.Event()

    class BlockingEmbedder(FakeEmbedder):
        def embed(self, texts):
            if any("BLOCKME" in t for t in texts):
                entered.set()
                assert release.wait(WAIT)
            return super().embed(texts)

    manager = CollectionManager(settings, BlockingEmbedder())
    c = manager.create_collection("c")
    slow = Worker(manager.add_document, c.id, "slow.txt", b"BLOCKME car engine")
    slow.start()
    assert entered.wait(WAIT)  # slow upload is mid-embedding
    try:
        fast = Worker(manager.add_document, c.id, "fast.txt", FRUIT)
        fast.start()
        assert fast.finished(WAIT) and fast.error is None  # same collection, not blocked
        assert manager.query(c.id, "apple")[0].chunk.filename == "fast.txt"
        assert manager.get_collection(c.id).document_count == 1  # slow one not committed yet
    finally:
        release.set()
    assert slow.finished(WAIT) and slow.error is None
    assert manager.get_collection(c.id).document_count == 2


def test_deleting_a_collection_during_ingestion_does_not_resurrect_it(settings):
    entered, release = threading.Event(), threading.Event()

    class BlockingEmbedder(FakeEmbedder):
        def embed(self, texts):
            entered.set()
            assert release.wait(WAIT)
            return super().embed(texts)

    manager = CollectionManager(settings, BlockingEmbedder())
    c = manager.create_collection("c")
    upload = Worker(manager.add_document, c.id, "a.txt", CARS)
    upload.start()
    assert entered.wait(WAIT)
    manager.delete_collection(c.id)  # deleted while the upload is still embedding
    release.set()
    assert upload.finished(WAIT)
    assert isinstance(upload.error, CollectionNotFoundError)
    assert not (settings.data_dir / "collections" / c.id).exists()
    assert manager.list_collections() == []
