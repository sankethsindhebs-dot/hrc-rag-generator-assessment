"""Collections: isolated document sets, each with its own on-disk index.

Layout: <data_dir>/collections/<id>/{meta.json, index.npz}

Isolation is structural. Every operation resolves a collection id to that collection's
own directory and VectorStore; there is no shared index and no metadata filter to forget.

Concurrency model
-----------------
* One lock PER COLLECTION guards the commit step of writes (and the first load from
  disk). Work on different collections never contends.
* The expensive part of ingestion -- text extraction, chunking, embedding -- runs OUTSIDE
  any lock, so a slow upload does not block queries or other uploads.
* Stores are immutable. A write builds the next store, persists it, and only then
  publishes it; readers take a snapshot reference and never wait on writers.
* Because new chunks are independent of existing ones (ids are per-document UUIDs), the
  commit simply appends them to the CURRENT store under the lock, so concurrent uploads
  into one collection cannot overwrite each other.

Limitation: the locks are in-process. Two server processes sharing one data directory are
not supported.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.rag.chunking import chunk_text
from app.rag.embeddings import Embedder
from app.rag.errors import (
    CollectionNotFoundError,
    DocumentTooLargeError,
    EmbedderMismatchError,
    EmptyDocumentError,
    InvalidInputError,
    StorageCorruptionError,
    StorageError,
)
from app.rag.loaders import load_text
from app.rag.store import Chunk, SearchHit, VectorStore, atomic_write_bytes

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
META_FILE = "meta.json"
MAX_NAME_LENGTH = 100
_META_KEYS = ("id", "name", "created_at", "embedder")


@dataclass(frozen=True)
class CollectionInfo:
    id: str
    name: str
    created_at: str
    document_count: int
    chunk_count: int


@dataclass(frozen=True)
class DocumentInfo:
    document_id: str
    filename: str
    chunk_count: int


class CollectionManager:
    def __init__(self, settings: Settings, embedder: Embedder):
        self._settings = settings
        self._embedder = embedder
        self._root = Path(settings.data_dir) / "collections"
        self._stores: dict[str, VectorStore] = {}  # published, immutable snapshots
        self._locks: dict[str, threading.Lock] = {}  # one per existing collection
        self._registry_lock = threading.Lock()  # guards _locks only; never held during I/O work

    # ---- lifecycle -------------------------------------------------------------

    def create_collection(self, name: str) -> CollectionInfo:
        name = (name or "").strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            raise InvalidInputError(f"collection name must be 1-{MAX_NAME_LENGTH} characters")
        collection_id = uuid.uuid4().hex
        meta = {
            "id": collection_id,
            "name": name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "embedder": {"name": self._embedder.name, "dimension": self._embedder.dimension},
        }
        directory = self._dir(collection_id)
        try:
            directory.mkdir(parents=True)
        except OSError as exc:
            raise StorageError(f"could not create collection directory: {exc}") from exc
        # meta.json is written last and atomically: a directory without it is "not a
        # collection", so a crash mid-create leaves nothing visible.
        atomic_write_bytes(directory / META_FILE, json.dumps(meta).encode("utf-8"))
        return CollectionInfo(collection_id, name, meta["created_at"], 0, 0)

    def list_collections(self) -> list[CollectionInfo]:
        infos = []
        if self._root.is_dir():
            for entry in self._root.iterdir():
                if not (_ID_RE.match(entry.name) and entry.is_dir()):
                    continue
                try:
                    infos.append(self.get_collection(entry.name))
                except (CollectionNotFoundError, EmbedderMismatchError, StorageError) as exc:
                    log.warning("skipping collection %s: %s", entry.name, exc)
        return sorted(infos, key=lambda i: (i.created_at, i.id))

    def get_collection(self, collection_id: str) -> CollectionInfo:
        meta, store = self._snapshot(collection_id)
        return self._info(meta, store)

    def delete_collection(self, collection_id: str) -> None:
        # Deliberately does NOT parse meta.json: deleting is how a damaged or
        # embedder-incompatible collection is cleaned up, so it must work on those too.
        with self._lock_for(collection_id):
            if not (self._dir(collection_id) / META_FILE).is_file():  # deleted while we waited
                raise CollectionNotFoundError("collection not found")
            self._stores.pop(collection_id, None)
            try:
                # Removing meta.json first makes the collection vanish atomically; the rest
                # is cleanup that a crash can leave behind harmlessly.
                (self._dir(collection_id) / META_FILE).unlink()
                shutil.rmtree(self._dir(collection_id))
            except OSError as exc:
                raise StorageError(f"could not delete collection {collection_id}: {exc}") from exc
        with self._registry_lock:
            self._locks.pop(collection_id, None)

    # ---- documents -------------------------------------------------------------

    def add_document(self, collection_id: str, filename: str, data: bytes) -> DocumentInfo:
        """Extract, chunk, embed and index one document.

        All-or-nothing: the collection's visible state changes only after the new index
        has been durably written. Any earlier failure leaves it untouched."""
        filename = (filename or "").strip() or "untitled.txt"
        if len(data) > self._settings.max_upload_bytes:
            raise DocumentTooLargeError(
                f"{filename!r} is {len(data)} bytes; the limit is {self._settings.max_upload_bytes}"
            )
        self._read_meta(collection_id)  # fail fast on unknown ids, before any expensive work

        # Slow part, no lock held.
        text = load_text(filename, data)
        pieces = chunk_text(text, self._settings.chunk_size, self._settings.chunk_overlap)
        if not pieces:
            raise EmptyDocumentError(f"{filename!r} contains no extractable text")
        vectors = self._embedder.embed([p.text for p in pieces])
        document_id = uuid.uuid4().hex
        chunks = [
            Chunk(f"{document_id}:{i}", document_id, filename, i, p.text, p.start, p.end)
            for i, p in enumerate(pieces)
        ]

        # Fast part, under this collection's lock only.
        with self._lock_for(collection_id):
            self._read_meta(collection_id)  # may have been deleted while we were embedding
            current = self._load_locked(collection_id)
            updated = current.with_added(chunks, vectors)  # new object; `current` is untouched
            updated.save(self._dir(collection_id))  # raises StorageError -> nothing published
            self._stores[collection_id] = updated  # publish only after persistence succeeded
        return DocumentInfo(document_id, filename, len(chunks))

    def list_documents(self, collection_id: str) -> list[DocumentInfo]:
        _, store = self._snapshot(collection_id)
        docs: dict[str, list[Chunk]] = {}
        for chunk in store.chunks:
            docs.setdefault(chunk.document_id, []).append(chunk)
        return [DocumentInfo(doc_id, cs[0].filename, len(cs)) for doc_id, cs in docs.items()]

    # ---- retrieval -------------------------------------------------------------

    def query(self, collection_id: str, question: str, top_k: int | None = None) -> list[SearchHit]:
        """Semantic top-k within ONE collection. Scores are raw cosine similarity; deciding
        whether they are good enough to answer from is the caller's job."""
        question = (question or "").strip()
        if not question:
            raise InvalidInputError("question must not be empty")
        k = self._settings.top_k if top_k is None else top_k
        if k <= 0:
            raise InvalidInputError("top_k must be positive")
        _, store = self._snapshot(collection_id)  # immutable snapshot: no lock held while embedding
        if len(store) == 0:
            return []
        query_vector = self._embedder.embed([question])[0]
        return store.search(query_vector, k)

    # ---- internals -------------------------------------------------------------

    def _dir(self, collection_id: str) -> Path:
        return self._root / collection_id

    def _lock_for(self, collection_id: str) -> threading.Lock:
        """The lock for an EXISTING collection. Unknown or malformed ids never get one, so
        the registry cannot grow from garbage ids."""
        self._validate_id(collection_id)
        with self._registry_lock:
            lock = self._locks.get(collection_id)
            if lock is None:
                if not (self._dir(collection_id) / META_FILE).is_file():
                    raise CollectionNotFoundError("collection not found")
                lock = self._locks[collection_id] = threading.Lock()
            return lock

    @staticmethod
    def _validate_id(collection_id: str) -> None:
        # A malformed id is reported exactly like a missing one, and never touches the disk.
        if not isinstance(collection_id, str) or not _ID_RE.match(collection_id):
            raise CollectionNotFoundError("collection not found")

    def _read_meta(self, collection_id: str) -> dict:
        self._validate_id(collection_id)
        path = self._dir(collection_id) / META_FILE
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CollectionNotFoundError("collection not found") from None
        except (OSError, ValueError) as exc:
            raise StorageCorruptionError(f"collection {collection_id} metadata is unreadable: {exc}") from exc
        if not isinstance(meta, dict) or any(k not in meta for k in _META_KEYS) or meta["id"] != collection_id:
            raise StorageCorruptionError(f"collection {collection_id} metadata is invalid")
        stored = meta["embedder"]
        if not isinstance(stored, dict) or stored.get("name") != self._embedder.name \
                or stored.get("dimension") != self._embedder.dimension:
            raise EmbedderMismatchError(
                f"collection {collection_id} was built with {stored.get('name') if isinstance(stored, dict) else stored} "
                f"but the active embedder is {self._embedder.name} (dim {self._embedder.dimension}); "
                "re-create the collection"
            )
        return meta

    def _load_locked(self, collection_id: str) -> VectorStore:
        """Current published store, loading it from disk on first use. Caller holds the lock."""
        store = self._stores.get(collection_id)
        if store is None:
            store = VectorStore.load(self._dir(collection_id), self._embedder.dimension)
            self._stores[collection_id] = store
        return store

    def _snapshot(self, collection_id: str) -> tuple[dict, VectorStore]:
        meta = self._read_meta(collection_id)
        store = self._stores.get(collection_id)
        if store is None:  # cold path: first access since start-up
            with self._lock_for(collection_id):
                self._read_meta(collection_id)
                store = self._load_locked(collection_id)
        return meta, store

    @staticmethod
    def _info(meta: dict, store: VectorStore) -> CollectionInfo:
        chunks = store.chunks
        return CollectionInfo(
            id=meta["id"],
            name=meta["name"],
            created_at=meta["created_at"],
            document_count=len({c.document_id for c in chunks}),
            chunk_count=len(chunks),
        )
