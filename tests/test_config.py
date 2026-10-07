import pytest

from app.config import DEFAULT_MODEL_SHA256, ConfigError, Settings


def test_defaults():
    s = Settings.from_env({})
    assert s.chunk_size == 800 and s.chunk_overlap == 150 and s.top_k == 4
    assert s.model_sha256 == DEFAULT_MODEL_SHA256
    assert str(s.data_dir) == "data"


def test_env_overrides():
    s = Settings.from_env({"RAG_DATA_DIR": "/x", "RAG_TOP_K": "7", "RAG_CHUNK_SIZE": "500", "RAG_CHUNK_OVERLAP": "50"})
    assert str(s.data_dir) == "/x" and s.top_k == 7 and s.chunk_size == 500


@pytest.mark.parametrize(
    "env",
    [
        {"RAG_TOP_K": "abc"},
        {"RAG_TOP_K": "0"},
        {"RAG_CHUNK_SIZE": "100", "RAG_CHUNK_OVERLAP": "100"},
        {"RAG_MODEL_SHA256": "not-a-digest"},
        {"RAG_MODEL_URL": "https://example.com/other.tar.gz"},  # new URL without its own digest
    ],
)
def test_invalid_values_are_rejected(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_custom_url_with_digest_is_accepted():
    s = Settings.from_env({"RAG_MODEL_URL": "https://example.com/m.tar.gz", "RAG_MODEL_SHA256": "A" * 64})
    assert s.model_sha256 == "a" * 64


def test_blank_values_fall_back_to_defaults():
    assert Settings.from_env({"RAG_TOP_K": "  "}).top_k == 4
