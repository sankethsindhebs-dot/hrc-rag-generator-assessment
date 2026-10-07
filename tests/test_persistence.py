"""Transactional persistence and corruption detection."""

import errno
import json

import numpy as np
import pytest

import app.rag.store as store_module
from app.rag.collections import CollectionManager
from app.rag.errors import (
    InvalidInputError,
    RagError,
    StorageCorruptionError,
    StorageError,
)
from app.rag.store import Chunk, VectorStore
from tests.helpers import FakeEmbedder

CARS = b"The automobile engine in this car needs service. A truck is a vehicle too."
FRUIT = b"An apple orchard grows pear and banana fruit every year."


def collection_dir(settings, collection_id):
    return settings.data_dir / "collections" / collection_id


def snapshot_of(manager, collection_id):
    """Everything a client can observe about a collection."""
    hits = [(h.chunk.id, round(h.score, 6)) for h in manager.query(collection_id, "car apple", top_k=50)]
    docs = [(d.document_id, d.filename, d.chunk_count) for d in manager.list_documents(collection_id)]
    return manager.get_collection(collection_id), docs, hits


# ---- error taxonomy ------------------------------------------------------------

def test_storage_errors_are_distinct_from_invalid_input():
    assert issubclass(StorageCorruptionError, StorageError) and issubclass(StorageError, RagError)
    assert not issubclass(StorageError, InvalidInputError)
    assert not issubclass(StorageCorruptionError, InvalidInputError)


# ---- a failed commit changes nothing -------------------------------------------

def _fail_replace(m):
    def boom(*a, **k):
        raise OSError(errno.EIO, "I/O error during rename")
    m.setattr(store_module.os, "replace", boom)


def _fail_fsync(m):
    def boom(*a, **k):
        raise OSError(errno.EIO, "I/O error during fsync")
    m.setattr(store_module.os, "fsync", boom)


def _fail_mid_write(m):
    def partial(f, **arrays):
        f.write(b"PK-partial-garbage")  # a half-written temp file, then the disk fills up
        raise OSError(errno.ENOSPC, "No space left on device")
    m.setattr(store_module.np, "savez", partial)


FAILURES = [
    pytest.param(_fail_replace, id="rename-fails"),
    pytest.param(_fail_fsync, id="fsync-fails"),
    pytest.param(_fail_mid_write, id="disk-full-mid-write"),
]


@pytest.mark.parametrize("inject", FAILURES)
def test_persistence_failure_leaves_visible_state_and_disk_unchanged(manager, settings, monkeypatch, inject):
    c = manager.create_collection("c")
    manager.add_document(c.id, "cars.txt", CARS)
    before = snapshot_of(manager, c.id)
    index = collection_dir(settings, c.id) / "index.npz"
    bytes_before = index.read_bytes()

    with monkeypatch.context() as m:
        inject(m)
        with pytest.raises(StorageError) as excinfo:
            manager.add_document(c.id, "fruit.txt", FRUIT)
    assert not isinstance(excinfo.value, InvalidInputError)

    assert snapshot_of(manager, c.id) == before  # nothing new is visible...
    assert index.read_bytes() == bytes_before  # ...nothing changed on disk...
    assert [p.name for p in index.parent.iterdir() if p.name.endswith(".tmp")] == []  # ...and no debris

    # The collection is not wedged: the same upload now succeeds and is fully persisted.
    manager.add_document(c.id, "fruit.txt", FRUIT)
    assert manager.get_collection(c.id).document_count == 2
    reloaded = CollectionManager(settings, FakeEmbedder())
    assert reloaded.get_collection(c.id).document_count == 2


def test_failure_on_the_very_first_commit_creates_no_index(manager, settings, monkeypatch):
    c = manager.create_collection("c")
    with monkeypatch.context() as m:
        _fail_replace(m)
        with pytest.raises(StorageError):
            manager.add_document(c.id, "cars.txt", CARS)
    assert manager.get_collection(c.id).chunk_count == 0
    assert manager.query(c.id, "car") == []
    assert list(collection_dir(settings, c.id).iterdir()) == [collection_dir(settings, c.id) / "meta.json"]


def test_state_is_published_only_after_the_index_is_durable(manager, settings, monkeypatch):
    """At the instant save() returns, the new index is on disk but memory still shows the old
    state; memory only changes after save() succeeds."""
    c = manager.create_collection("c")
    manager.add_document(c.id, "cars.txt", CARS)
    seen = {}
    original = VectorStore.save

    def spying_save(self, directory):
        seen["in_memory_during_save"] = manager._stores[c.id].chunks
        original(self, directory)
        seen["on_disk_after_save"] = len(VectorStore.load(directory, FakeEmbedder().dimension))

    monkeypatch.setattr(VectorStore, "save", spying_save)
    doc = manager.add_document(c.id, "fruit.txt", FRUIT)
    assert len(seen["in_memory_during_save"]) < seen["on_disk_after_save"]
    assert len(manager._stores[c.id].chunks) == seen["on_disk_after_save"]
    assert doc.chunk_count > 0


# ---- interrupted writes --------------------------------------------------------

class SimulatedCrash(BaseException):
    """Not an OSError, so cleanup code does not run -- like the process dying."""


def test_crash_before_the_rename_leaves_the_previous_index_intact(settings, monkeypatch):
    first = CollectionManager(settings, FakeEmbedder())
    c = first.create_collection("c")
    first.add_document(c.id, "cars.txt", CARS)
    index = collection_dir(settings, c.id) / "index.npz"
    good = index.read_bytes()

    with monkeypatch.context() as m:
        def die(*a, **k):
            raise SimulatedCrash()
        m.setattr(store_module.os, "replace", die)
        with pytest.raises(SimulatedCrash):
            first.add_document(c.id, "fruit.txt", FRUIT)

    assert index.read_bytes() == good
    assert (index.parent / "index.npz.tmp").exists()  # the orphaned temp file a real crash would leave

    restarted = CollectionManager(settings, FakeEmbedder())  # a "new process"
    assert restarted.get_collection(c.id).document_count == 1
    restarted.add_document(c.id, "fruit.txt", FRUIT)  # recovers and overwrites the stale temp file
    assert restarted.get_collection(c.id).document_count == 2
    assert not (index.parent / "index.npz.tmp").exists()


def test_a_truncated_leftover_temp_file_is_ignored(tmp_path):
    store = VectorStore(2).with_added([Chunk("c0", "d", "f.txt", 0, "t", 0, 1)], np.array([[1.0, 0.0]]))
    store.save(tmp_path)
    (tmp_path / "index.npz.tmp").write_bytes((tmp_path / "index.npz").read_bytes()[:20])
    assert VectorStore.load(tmp_path, 2).chunks == store.chunks


# ---- corruption detection ------------------------------------------------------

def good_index_bytes(tmp_path):
    chunks = [Chunk(f"c{i}", "d", "f.txt", i, f"text {i}", 0, 5) for i in range(3)]
    vectors = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)
    store = VectorStore(3).with_added(chunks, vectors)
    store.save(tmp_path)
    return (tmp_path / "index.npz").read_bytes(), store


def write_npz(path, *, vectors=None, chunks=None, version=1, omit=()):
    records = chunks if chunks is not None else [
        {"id": f"c{i}", "document_id": "d", "filename": "f.txt", "index": i, "text": "t", "start": 0, "end": 1}
        for i in range(3)
    ]
    arrays = {
        "format_version": np.int64(version),
        "vectors": np.eye(3, dtype=np.float32) if vectors is None else vectors,
        "chunks": np.frombuffer(records if isinstance(records, bytes) else json.dumps(records).encode(), dtype=np.uint8),
    }
    for key in omit:
        arrays.pop(key)
    with open(path, "wb") as f:
        np.savez(f, **arrays)


def _rec(**overrides):
    base = {"id": "c0", "document_id": "d", "filename": "f.txt", "index": 0, "text": "t", "start": 0, "end": 1}
    base.update(overrides)
    return base


BAD_INDEXES = {
    "empty-file": lambda p, raw: p.write_bytes(b""),
    "truncated-half": lambda p, raw: p.write_bytes(raw[: len(raw) // 2]),
    "truncated-to-header": lambda p, raw: p.write_bytes(raw[:30]),
    "garbage-bytes": lambda p, raw: p.write_bytes(b"this is definitely not an index" * 5),
    "plain-npy-not-npz": lambda p, raw: np.save(p.open("wb"), np.eye(3, dtype=np.float32)),
    "bit-flip-in-vectors": lambda p, raw: p.write_bytes(
        raw[: raw.index(np.eye(3, dtype=np.float32).tobytes())]
        + bytes([raw[raw.index(np.eye(3, dtype=np.float32).tobytes())] ^ 0xFF])
        + raw[raw.index(np.eye(3, dtype=np.float32).tobytes()) + 1 :]
    ),
    "wrong-dimension": lambda p, raw: write_npz(p, vectors=np.ones((3, 5), dtype=np.float32)),
    "fewer-vectors-than-chunks": lambda p, raw: write_npz(p, vectors=np.eye(3, dtype=np.float32)[:2]),
    "more-vectors-than-chunks": lambda p, raw: write_npz(p, vectors=np.ones((4, 3), dtype=np.float32)),
    "vectors-not-2d": lambda p, raw: write_npz(p, vectors=np.ones(9, dtype=np.float32)),
    "nan-vector": lambda p, raw: write_npz(p, vectors=np.full((3, 3), np.nan, dtype=np.float32)),
    "unknown-format-version": lambda p, raw: write_npz(p, version=99),
    "missing-vectors-member": lambda p, raw: write_npz(p, omit=("vectors",)),
    "missing-chunks-member": lambda p, raw: write_npz(p, omit=("chunks",)),
    "chunks-not-json": lambda p, raw: write_npz(p, chunks=b"{oops"),
    "chunks-not-utf8": lambda p, raw: write_npz(p, chunks=b"\xff\xfe\xfa"),
    "chunks-not-a-list": lambda p, raw: write_npz(p, chunks=b'{"a": 1}'),
    "chunk-record-missing-field": lambda p, raw: write_npz(p, chunks=[_rec(), _rec(), {"id": "x"}]),
    "chunk-record-extra-field": lambda p, raw: write_npz(p, chunks=[_rec(), _rec(), _rec(evil=1)]),
    "chunk-field-wrong-type": lambda p, raw: write_npz(p, chunks=[_rec(), _rec(), _rec(index="0")]),
}


@pytest.mark.parametrize("name", list(BAD_INDEXES))
def test_damaged_index_is_reported_as_corruption(tmp_path, name):
    raw, _ = good_index_bytes(tmp_path)
    BAD_INDEXES[name](tmp_path / "index.npz", raw)
    with pytest.raises(StorageCorruptionError):
        VectorStore.load(tmp_path, 3)


def test_the_good_index_used_as_a_baseline_loads_fine(tmp_path):
    _, store = good_index_bytes(tmp_path)
    assert VectorStore.load(tmp_path, 3).chunks == store.chunks


# ---- corruption surfaced through the manager -----------------------------------

def test_manager_reports_a_damaged_index_as_storage_corruption_and_does_not_overwrite_it(settings):
    first = CollectionManager(settings, FakeEmbedder())
    healthy = first.create_collection("healthy")
    first.add_document(healthy.id, "cars.txt", CARS)
    broken = first.create_collection("broken")
    first.add_document(broken.id, "cars.txt", CARS)
    index = collection_dir(settings, broken.id) / "index.npz"
    index.write_bytes(index.read_bytes()[:50])  # torn / truncated file
    damaged = index.read_bytes()

    restarted = CollectionManager(settings, FakeEmbedder())
    operations = [
        lambda: restarted.get_collection(broken.id),
        lambda: restarted.list_documents(broken.id),
        lambda: restarted.query(broken.id, "car"),
        lambda: restarted.add_document(broken.id, "more.txt", FRUIT),
    ]
    for op in operations:
        with pytest.raises(StorageCorruptionError) as excinfo:
            op()
        assert not isinstance(excinfo.value, InvalidInputError)
    assert index.read_bytes() == damaged  # we never clobber evidence with a fresh index

    assert [c.name for c in restarted.list_collections()] == ["healthy"]  # others still work
    assert restarted.query(healthy.id, "automobile")[0].chunk.filename == "cars.txt"


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", json.dumps({"id": "0" * 32, "name": "x"}), None],
    ids=["invalid-json", "wrong-shape", "missing-keys", "id-mismatch"],
)
def test_damaged_collection_metadata_is_storage_corruption(settings, content):
    manager = CollectionManager(settings, FakeEmbedder())
    c = manager.create_collection("c")
    meta = collection_dir(settings, c.id) / "meta.json"
    if content is None:  # a valid-looking file that belongs to a different collection
        content = meta.read_text().replace(c.id, "f" * 32)
    meta.write_text(content)
    for op in (lambda: manager.get_collection(c.id), lambda: manager.query(c.id, "car")):
        with pytest.raises(StorageCorruptionError):
            op()


# ---- damaged collections can still be cleaned up --------------------------------

def test_a_collection_with_damaged_metadata_can_still_be_deleted(settings):
    manager = CollectionManager(settings, FakeEmbedder())
    c = manager.create_collection("c")
    (collection_dir(settings, c.id) / "meta.json").write_text("{broken")
    manager.delete_collection(c.id)
    assert not collection_dir(settings, c.id).exists()


def test_a_collection_with_a_damaged_index_can_still_be_deleted(settings):
    manager = CollectionManager(settings, FakeEmbedder())
    c = manager.create_collection("c")
    manager.add_document(c.id, "cars.txt", CARS)
    index = collection_dir(settings, c.id) / "index.npz"
    index.write_bytes(index.read_bytes()[:40])
    manager.delete_collection(c.id)
    assert not collection_dir(settings, c.id).exists()


def test_an_embedder_incompatible_collection_can_still_be_deleted(settings):
    first = CollectionManager(settings, FakeEmbedder())
    c = first.create_collection("c")

    class Other(FakeEmbedder):
        name = "some-other-model"

    second = CollectionManager(settings, Other())
    second.delete_collection(c.id)  # the error message says "re-create the collection"; that must be possible
    assert not collection_dir(settings, c.id).exists()
