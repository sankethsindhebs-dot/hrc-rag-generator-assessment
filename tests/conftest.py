import socket

import pytest

from app.config import Settings
from app.rag.collections import CollectionManager
from tests.helpers import FakeEmbedder


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        model_cache_dir=tmp_path / "models",
        model_url="file:///unused",
        model_sha256="0" * 64,
        chunk_size=200,
        chunk_overlap=40,
        top_k=3,
        max_upload_bytes=50_000,
    )


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def manager(settings, embedder):
    return CollectionManager(settings, embedder)


@pytest.fixture(autouse=True)
def _offline_guard(request, monkeypatch):
    """Offline tests must never touch the network. Only tests marked `model` (which may
    download the embedding model) are exempt."""
    if request.node.get_closest_marker("model"):
        return

    def refuse(*args, **kwargs):
        raise RuntimeError("network access attempted in an offline test")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
