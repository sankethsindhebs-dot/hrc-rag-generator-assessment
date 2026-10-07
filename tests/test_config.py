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


# ---- generation settings -------------------------------------------------------

def test_generation_defaults():
    s = Settings.from_env({})
    assert s.min_score == 0.15 and s.anthropic_model == "claude-opus-5-5" and s.anthropic_api_key is None


def test_api_key_and_model_come_from_the_environment_and_never_appear_in_repr():
    s = Settings.from_env({"ANTHROPIC_API_KEY": "  sk-ant-super-secret  ", "ANTHROPIC_MODEL": "claude-sonnet-5-5"})
    assert s.anthropic_api_key == "sk-ant-super-secret" and s.anthropic_model == "claude-sonnet-5-5"
    assert "super-secret" not in repr(s) and "super-secret" not in str(s)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_api_key_counts_as_missing(value):
    assert Settings.from_env({"ANTHROPIC_API_KEY": value}).anthropic_api_key is None


def test_min_score_is_configurable_and_validated():
    assert Settings.from_env({"RAG_MIN_SCORE": "0.3"}).min_score == 0.3
    assert Settings.from_env({"RAG_MIN_SCORE": "0"}).min_score == 0.0
    for bad in ["abc", "-0.1", "1.5"]:
        with pytest.raises(ConfigError):
            Settings.from_env({"RAG_MIN_SCORE": bad})
