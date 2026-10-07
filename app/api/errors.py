"""Deliberate mapping from domain errors to HTTP responses.

Clients get a stable machine-readable ``code`` and a message that is safe to show. Messages for
server-side failures are fixed strings: file-system paths, URLs, API keys and exception text go
to the server log only.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.rag.errors import (
    CollectionNotFoundError,
    DocumentLoadError,
    DocumentTooLargeError,
    EmbedderMismatchError,
    EmptyDocumentError,
    GenerationError,
    GenerationUnavailableError,
    InvalidInputError,
    ModelUnavailableError,
    StorageCorruptionError,
    StorageError,
    UnsupportedDocumentError,
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True)
class ApiError:
    status: int
    code: str
    message: str


def describe(exc: Exception) -> ApiError:
    """Most specific classes first: several of these are subclasses of one another."""
    if isinstance(exc, CollectionNotFoundError):
        return ApiError(404, "collection_not_found", "Collection not found.")
    if isinstance(exc, UnsupportedDocumentError):
        return ApiError(415, "unsupported_document", str(exc))
    if isinstance(exc, DocumentTooLargeError):
        return ApiError(413, "document_too_large", str(exc))
    if isinstance(exc, EmptyDocumentError):
        return ApiError(422, "empty_document", str(exc))
    if isinstance(exc, DocumentLoadError):
        return ApiError(422, "unreadable_document", str(exc))
    if isinstance(exc, InvalidInputError):
        return ApiError(422, "invalid_input", str(exc))
    if isinstance(exc, GenerationUnavailableError):
        return ApiError(503, "generation_unavailable", str(exc))  # curated text, no secrets
    if isinstance(exc, GenerationError):
        return ApiError(502, "generation_failed", str(exc))  # curated text from our own mapping
    if isinstance(exc, ModelUnavailableError):
        return ApiError(503, "embedding_unavailable",
                        "The embedding model is not available. See the server log for details.")
    if isinstance(exc, EmbedderMismatchError):
        return ApiError(409, "embedder_mismatch",
                        "This collection was built with a different embedding model. Delete and re-create it.")
    if isinstance(exc, StorageCorruptionError):
        return ApiError(500, "storage_corruption",
                        "Stored data for this collection is damaged. Delete and re-create the collection.")
    if isinstance(exc, StorageError):
        return ApiError(500, "storage_error", "The server could not read or write its storage.")
    return ApiError(500, "internal_error", "An unexpected error occurred.")


def envelope(error: ApiError, **extra: object) -> dict:
    return {"error": {"code": error.code, "message": error.message, **extra}}
