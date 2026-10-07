"""Domain errors. The API layer maps these to HTTP status codes."""


class RagError(Exception):
    """Base class for all expected, user-facing domain errors."""


class InvalidInputError(RagError):
    """A caller-supplied value (name, question, parameter) is not acceptable."""


class CollectionNotFoundError(RagError):
    """No collection with this id exists (also raised for malformed ids)."""


class DocumentLoadError(RagError):
    """A document could not be turned into text."""


class UnsupportedDocumentError(DocumentLoadError):
    """The file type is not supported."""


class EmptyDocumentError(DocumentLoadError):
    """The document contained no extractable text."""


class DocumentTooLargeError(DocumentLoadError):
    """The document exceeds the configured size limit."""


class ModelUnavailableError(RagError):
    """The embedding model could not be fetched, verified or loaded."""


class EmbedderMismatchError(RagError):
    """A persisted collection was built with a different embedder than the one in use."""


class StorageError(RagError):
    """Server-side persistence failed (disk full, permissions, ...). Not the caller's fault."""


class StorageCorruptionError(StorageError):
    """Persisted state exists but is unreadable, truncated, or internally inconsistent."""
