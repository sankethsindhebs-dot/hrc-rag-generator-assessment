import pytest

from app.rag.collections import CollectionManager
from app.rag.errors import (
    CollectionNotFoundError,
    DocumentTooLargeError,
    EmbedderMismatchError,
    EmptyDocumentError,
    InvalidInputError,
    UnsupportedDocumentError,
)
from tests.helpers import FakeEmbedder

CARS = b"The automobile engine in this car needs service. A truck is a vehicle too."
FRUIT = b"An apple orchard grows pear and banana fruit every year."


# ---- lifecycle ---------------------------------------------------------------

def test_create_list_get_delete(manager):
    a = manager.create_collection("  Alpha ")
    b = manager.create_collection("Beta")
    assert a.name == "Alpha" and a.id != b.id and a.document_count == 0
    assert {c.name for c in manager.list_collections()} == {"Alpha", "Beta"}
    assert manager.get_collection(a.id).name == "Alpha"
    manager.delete_collection(a.id)
    assert [c.name for c in manager.list_collections()] == ["Beta"]
    with pytest.raises(CollectionNotFoundError):
        manager.get_collection(a.id)


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_invalid_names(manager, name):
    with pytest.raises(InvalidInputError):
        manager.create_collection(name)


@pytest.mark.parametrize("bad_id", ["../etc", "..", "", "a" * 32, "0" * 31, "0" * 33, "../" + "0" * 32, "0" * 32])
def test_malformed_or_unknown_ids_are_not_found(manager, bad_id):
    for call in (
        lambda: manager.get_collection(bad_id),
        lambda: manager.delete_collection(bad_id),
        lambda: manager.add_document(bad_id, "a.txt", b"hi"),
        lambda: manager.query(bad_id, "hi"),
        lambda: manager.list_documents(bad_id),
    ):
        with pytest.raises(CollectionNotFoundError):
            call()


def test_delete_removes_files_and_only_that_collection(manager, settings):
    keep = manager.create_collection("keep")
    drop = manager.create_collection("drop")
    manager.add_document(drop.id, "a.txt", CARS)
    manager.delete_collection(drop.id)
    assert not (settings.data_dir / "collections" / drop.id).exists()
    assert (settings.data_dir / "collections" / keep.id).exists()


# ---- ingestion ---------------------------------------------------------------

def test_add_document_chunks_embeds_and_indexes(manager, embedder):
    c = manager.create_collection("c")
    doc = manager.add_document(c.id, "cars.txt", CARS)
    assert doc.filename == "cars.txt" and doc.chunk_count >= 1
    info = manager.get_collection(c.id)
    assert info.document_count == 1 and info.chunk_count == doc.chunk_count
    assert manager.list_documents(c.id) == [doc]
    assert embedder.calls  # real embedding step was used


def test_long_document_is_split_into_multiple_chunks(manager):
    c = manager.create_collection("c")
    doc = manager.add_document(c.id, "long.txt", ("The car engine hums. " * 60).encode())
    assert doc.chunk_count > 1


def test_multiple_documents_are_tracked_separately(manager):
    c = manager.create_collection("c")
    d1 = manager.add_document(c.id, "a.txt", CARS)
    d2 = manager.add_document(c.id, "b.txt", FRUIT)
    assert d1.document_id != d2.document_id
    assert {d.filename for d in manager.list_documents(c.id)} == {"a.txt", "b.txt"}
    assert manager.get_collection(c.id).document_count == 2


def test_blank_filename_gets_a_default(manager):
    c = manager.create_collection("c")
    assert manager.add_document(c.id, "  ", b"hello").filename == "untitled.txt"


def test_failed_ingestion_leaves_collection_unchanged(manager):
    c = manager.create_collection("c")
    manager.add_document(c.id, "ok.txt", CARS)
    before = manager.get_collection(c.id)
    for filename, data, error in [
        ("x.exe", b"data", UnsupportedDocumentError),
        ("empty.txt", b"   ", EmptyDocumentError),
        ("big.txt", b"a" * 50_001, DocumentTooLargeError),
    ]:
        with pytest.raises(error):
            manager.add_document(c.id, filename, data)
    assert manager.get_collection(c.id) == before


def test_embedder_failure_leaves_collection_unchanged(settings):
    class Exploding(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("boom")

    manager = CollectionManager(settings, Exploding())
    c = manager.create_collection("c")
    with pytest.raises(RuntimeError):
        manager.add_document(c.id, "a.txt", CARS)
    assert manager.get_collection(c.id).chunk_count == 0


# ---- retrieval ---------------------------------------------------------------

def test_query_returns_semantically_relevant_chunk_first(manager):
    c = manager.create_collection("c")
    manager.add_document(c.id, "cars.txt", CARS)
    manager.add_document(c.id, "fruit.txt", FRUIT)
    hits = manager.query(c.id, "automobile")  # shares a concept with "car"/"engine", not the literal word
    assert hits[0].chunk.filename == "cars.txt"
    assert hits[0].score > hits[-1].score
    assert hits[0].chunk.text in CARS.decode()


def test_query_respects_top_k(manager):
    c = manager.create_collection("c")
    manager.add_document(c.id, "long.txt", ("The car engine hums. " * 60).encode())
    assert len(manager.query(c.id, "car", top_k=2)) == 2
    assert len(manager.query(c.id, "car")) == 3  # settings.top_k


def test_query_on_empty_collection_returns_nothing_and_skips_embedding(manager, embedder):
    c = manager.create_collection("c")
    assert manager.query(c.id, "anything") == []
    assert embedder.calls == []


def test_unrelated_question_scores_near_zero(manager):
    c = manager.create_collection("c")
    manager.add_document(c.id, "cars.txt", CARS)
    assert manager.query(c.id, "zebra")[0].score == pytest.approx(0.0)


@pytest.mark.parametrize("question,k", [("", None), ("   ", None), ("car", 0), ("car", -1)])
def test_invalid_query_arguments(manager, question, k):
    c = manager.create_collection("c")
    with pytest.raises(InvalidInputError):
        manager.query(c.id, question, top_k=k)


# ---- isolation ---------------------------------------------------------------

def test_collections_never_leak_into_each_other(manager):
    cars = manager.create_collection("cars")
    fruit = manager.create_collection("fruit")
    manager.add_document(cars.id, "cars.txt", CARS)
    manager.add_document(fruit.id, "fruit.txt", FRUIT)

    # Ask each collection about the *other's* topic, with k far larger than either index.
    for collection, question, own_file in [(cars, "banana orchard", "cars.txt"), (fruit, "automobile truck", "fruit.txt")]:
        hits = manager.query(collection.id, question, top_k=100)
        assert hits, "collection should still return its own (weak) matches"
        assert {h.chunk.filename for h in hits} == {own_file}


def test_isolation_holds_for_identical_filenames_and_text(manager):
    a = manager.create_collection("a")
    b = manager.create_collection("b")
    manager.add_document(a.id, "same.txt", CARS)
    assert manager.query(b.id, "car") == []  # b is empty even though a has "same.txt"
    manager.add_document(b.id, "same.txt", FRUIT)
    chunk_ids_a = {c.id for c in manager._stores[a.id].chunks}
    chunk_ids_b = {c.id for c in manager._stores[b.id].chunks}
    assert chunk_ids_a.isdisjoint(chunk_ids_b)
    assert all("apple" not in h.chunk.text for h in manager.query(a.id, "apple", top_k=50))


def test_deleting_one_collection_does_not_affect_another(manager):
    a = manager.create_collection("a")
    b = manager.create_collection("b")
    manager.add_document(a.id, "a.txt", CARS)
    manager.add_document(b.id, "b.txt", FRUIT)
    manager.delete_collection(a.id)
    assert manager.query(b.id, "apple")[0].chunk.filename == "b.txt"
    with pytest.raises(CollectionNotFoundError):
        manager.query(a.id, "car")


def test_deleted_collection_id_is_not_resurrected_by_a_new_one(manager):
    a = manager.create_collection("a")
    manager.add_document(a.id, "a.txt", CARS)
    manager.delete_collection(a.id)
    b = manager.create_collection("b")
    assert b.id != a.id and manager.query(b.id, "car") == []


# ---- persistence -------------------------------------------------------------

def test_collections_survive_a_restart(settings):
    first = CollectionManager(settings, FakeEmbedder())
    c = first.create_collection("persisted")
    first.add_document(c.id, "cars.txt", CARS)

    second = CollectionManager(settings, FakeEmbedder())  # fresh process, same data dir
    assert [i.name for i in second.list_collections()] == ["persisted"]
    assert second.get_collection(c.id).chunk_count >= 1
    assert second.query(c.id, "automobile")[0].chunk.filename == "cars.txt"
    assert [d.filename for d in second.list_documents(c.id)] == ["cars.txt"]


def test_reloading_with_a_different_embedder_is_refused(settings):
    first = CollectionManager(settings, FakeEmbedder())
    c = first.create_collection("c")
    first.add_document(c.id, "cars.txt", CARS)

    class Other(FakeEmbedder):
        name = "some-other-model"

    second = CollectionManager(settings, Other())
    with pytest.raises(EmbedderMismatchError):
        second.query(c.id, "car")
    assert second.list_collections() == []  # incompatible collections are skipped, not served


def test_list_ignores_foreign_directories(manager, settings):
    manager.create_collection("real")
    (settings.data_dir / "collections" / "not-an-id").mkdir()
    (settings.data_dir / "collections" / ("f" * 32)).mkdir()  # id-shaped but no meta.json
    assert [c.name for c in manager.list_collections()] == ["real"]
