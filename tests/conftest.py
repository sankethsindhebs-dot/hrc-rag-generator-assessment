from pathlib import Path

import pytest

from rag.config import Settings
from rag.pipeline import build_collection
from tests.fakes import HashEmbedder

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def embedder():
    return HashEmbedder()


@pytest.fixture
def settings():
    # Small chunks so the short fixtures yield several chunks each.
    return Settings(chunk_size=300, chunk_overlap=40, top_k=3, min_score=0.15)


def fixture_bytes(name: str) -> tuple[str, bytes]:
    return name, (FIXTURES / name).read_bytes()


@pytest.fixture
def both_collection(embedder, settings):
    files = [fixture_bytes("sourdough.txt"), fixture_bytes("remote_policy.txt")]
    return build_collection(files, embedder, settings)
