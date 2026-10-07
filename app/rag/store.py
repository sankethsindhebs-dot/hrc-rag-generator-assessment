"""A small exact-search vector store: a NumPy matrix plus the chunks it was built from.

One instance holds exactly one collection's data; nothing here knows about other
collections, so cross-collection leakage is structurally impossible at this layer.

Stores are IMMUTABLE. ``with_added`` returns a new store and leaves the original
untouched, so a caller can build the next state, persist it, and only then publish it;
readers holding the old store keep a consistent snapshot.

On disk a store is ONE file, ``index.npz``, holding the vectors and the chunk metadata
together. It is written to a temp file, fsynced, then moved into place with an atomic
``os.replace``. Because there is no second file, vectors and metadata can never disagree
after a crash: a reader sees either the complete old index or the complete new one.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from app.rag.errors import InvalidInputError, StorageCorruptionError, StorageError

INDEX_FILE = "index.npz"
FORMAT_VERSION = 1


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    filename: str
    index: int  # position of the chunk within its document
    text: str
    start: int  # character offsets into the document's normalised text
    end: int


_CHUNK_FIELD_TYPES = {
    "id": str, "document_id": str, "filename": str, "index": int, "text": str, "start": int, "end": int,
}


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float  # cosine similarity in [-1, 1]


def _unit(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return (matrix / np.where(norms == 0, 1.0, norms)).astype(np.float32)


class VectorStore:
    def __init__(self, dimension: int, vectors: np.ndarray | None = None, chunks: Sequence[Chunk] = ()):
        if dimension <= 0:
            raise InvalidInputError("dimension must be positive")
        self.dimension = dimension
        self._chunks: tuple[Chunk, ...] = tuple(chunks)
        if vectors is None:
            vectors = np.zeros((0, dimension), dtype=np.float32)
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.shape != (len(self._chunks), dimension):
            raise InvalidInputError(
                f"expected vectors of shape ({len(self._chunks)}, {dimension}), got {vectors.shape}"
            )
        vectors.setflags(write=False)
        self._vectors = vectors

    def __len__(self) -> int:
        return len(self._chunks)

    @property
    def chunks(self) -> tuple[Chunk, ...]:
        return self._chunks

    def with_added(self, chunks: Sequence[Chunk], vectors: np.ndarray) -> "VectorStore":
        """Return a NEW store containing this one's contents plus the given chunks."""
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape != (len(chunks), self.dimension):
            raise InvalidInputError(
                f"expected vectors of shape ({len(chunks)}, {self.dimension}), got {vectors.shape}"
            )
        if not np.isfinite(vectors).all():
            raise InvalidInputError("vectors contain NaN or infinity")
        if len(chunks) == 0:
            return self
        return VectorStore(
            self.dimension,
            np.vstack([self._vectors, _unit(vectors)]),
            self._chunks + tuple(chunks),
        )

    def search(self, query: np.ndarray, k: int) -> list[SearchHit]:
        """Top-k chunks by cosine similarity, best first. Empty store -> empty list."""
        if k <= 0:
            raise InvalidInputError("k must be positive")
        query = np.asarray(query, dtype=np.float32).reshape(-1)
        if query.shape[0] != self.dimension:
            raise InvalidInputError(f"query has dimension {query.shape[0]}, store has {self.dimension}")
        if not self._chunks:
            return []
        scores = self._vectors @ _unit(query)
        k = min(k, len(self._chunks))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.lexsort((top, -scores[top]))]  # score desc, then insertion order
        return [SearchHit(self._chunks[i], float(scores[i])) for i in top]

    # ---- persistence -----------------------------------------------------------

    def save(self, directory: Path) -> None:
        """Atomically replace ``<directory>/index.npz`` with this store's contents.

        On any failure the previous index (if any) is left exactly as it was and
        StorageError is raised. Callers must serialise saves to the same directory."""
        directory = Path(directory)
        final = directory / INDEX_FILE
        tmp = directory / (INDEX_FILE + ".tmp")
        chunks_json = json.dumps([asdict(c) for c in self._chunks]).encode("utf-8")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as f:
                np.savez(
                    f,
                    format_version=np.int64(FORMAT_VERSION),
                    vectors=self._vectors,
                    chunks=np.frombuffer(chunks_json, dtype=np.uint8),
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)  # the commit point: atomic on POSIX filesystems
        except OSError as exc:
            _remove_quietly(tmp)
            raise StorageError(f"could not persist index in {directory}: {exc}") from exc
        _fsync_directory(directory)

    @classmethod
    def load(cls, directory: Path, dimension: int) -> "VectorStore":
        """Load ``<directory>/index.npz``. A missing file means an empty store; a present
        but unreadable or inconsistent one raises StorageCorruptionError."""
        path = Path(directory) / INDEX_FILE
        if not path.exists():
            return cls(dimension)
        try:
            with np.load(path, allow_pickle=False) as data:
                if not isinstance(data, np.lib.npyio.NpzFile):
                    raise StorageCorruptionError(f"{path} is not an index archive")
                version = int(data["format_version"])
                vectors = np.array(data["vectors"])  # reading a member verifies its CRC-32
                chunks_raw = data["chunks"].tobytes()
            if version != FORMAT_VERSION:
                raise StorageCorruptionError(f"{path} has unsupported format version {version}")
            records = json.loads(chunks_raw.decode("utf-8"))
            chunks = [_chunk_from_record(r) for r in records]
            if vectors.ndim != 2 or vectors.shape != (len(chunks), dimension):
                raise StorageCorruptionError(
                    f"{path} holds vectors of shape {vectors.shape} for {len(chunks)} chunks "
                    f"(expected dimension {dimension})"
                )
            if not np.isfinite(vectors).all():
                raise StorageCorruptionError(f"{path} contains NaN or infinite vectors")
            return cls(dimension, vectors.astype(np.float32, copy=False), chunks)
        except StorageCorruptionError:
            raise
        except Exception as exc:  # any failure while parsing a persisted file means it is damaged
            raise StorageCorruptionError(f"{path} is unreadable: {exc}") from exc


def _chunk_from_record(record: object) -> Chunk:
    if not isinstance(record, dict) or set(record) != set(_CHUNK_FIELD_TYPES):
        raise StorageCorruptionError("chunk record has unexpected fields")
    for name, expected in _CHUNK_FIELD_TYPES.items():
        if not isinstance(record[name], expected) or isinstance(record[name], bool):
            raise StorageCorruptionError(f"chunk field {name!r} has the wrong type")
    return Chunk(**record)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so a crash leaves either the old file or the new one."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        _remove_quietly(tmp)
        raise StorageError(f"could not write {path}: {exc}") from exc
    _fsync_directory(path.parent)


def _remove_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    """Make the rename itself durable. Best effort: the new file is already in place by
    now, so a failure here (or a platform that cannot open directories) is not an error."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
