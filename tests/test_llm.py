from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from rag.config import Settings
from rag.llm import ClaudeLLM, ConfigError, LLMError

SCHEMA = {"type": "object"}


class FakeMessages:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.kwargs = response, exc, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


def client_with(response=None, exc=None):
    return SimpleNamespace(messages=FakeMessages(response, exc))


def response(text="{}", stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def api_error(cls, status):
    req = httpx.Request("POST", "https://example.invalid")
    return cls("boom", response=httpx.Response(status, request=req), body=None)


def test_model_comes_from_config_and_has_no_hardcoded_default():
    assert Settings().llm_model == ""
    with pytest.raises(ConfigError, match="RAG_LLM_MODEL"):
        ClaudeLLM("")


def test_request_shape():
    c = client_with(response('{"a": 1}'))
    out = ClaudeLLM("model-from-config", client=c).generate("SYS", "USER", SCHEMA)
    kw = c.messages.kwargs
    assert out == '{"a": 1}'
    assert kw["model"] == "model-from-config" and kw["system"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert "api_key" not in kw


def test_refusal_is_an_llm_error():
    with pytest.raises(LLMError, match="declined"):
        ClaudeLLM("m", client=client_with(response(stop_reason="refusal"))).generate("s", "u", SCHEMA)


def test_truncated_reply_is_an_llm_error():
    with pytest.raises(LLMError, match="cut off"):
        ClaudeLLM("m", client=client_with(response(stop_reason="max_tokens"))).generate("s", "u", SCHEMA)


def test_no_text_block_is_an_llm_error():
    empty = SimpleNamespace(stop_reason="end_turn", content=[])
    with pytest.raises(LLMError, match="no text"):
        ClaudeLLM("m", client=client_with(empty)).generate("s", "u", SCHEMA)


def test_auth_failure_is_a_config_error_without_leaking_details():
    c = client_with(exc=api_error(anthropic.AuthenticationError, 401))
    with pytest.raises(ConfigError, match="credentials") as info:
        ClaudeLLM("m", client=c).generate("s", "u", SCHEMA)
    assert "boom" not in str(info.value)


def test_unknown_model_is_a_config_error():
    c = client_with(exc=api_error(anthropic.NotFoundError, 404))
    with pytest.raises(ConfigError, match="RAG_LLM_MODEL"):
        ClaudeLLM("nope", client=c).generate("s", "u", SCHEMA)


def test_other_api_errors_are_llm_errors():
    c = client_with(exc=api_error(anthropic.InternalServerError, 500))
    with pytest.raises(LLMError, match="InternalServerError"):
        ClaudeLLM("m", client=c).generate("s", "u", SCHEMA)


def test_missing_credentials_typeerror_becomes_config_error():
    c = client_with(exc=TypeError("Could not resolve authentication method. Expected ..."))
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        ClaudeLLM("m", client=c).generate("s", "u", SCHEMA)


def test_unrelated_typeerror_is_not_swallowed():
    with pytest.raises(TypeError, match="real bug"):
        ClaudeLLM("m", client=client_with(exc=TypeError("real bug"))).generate("s", "u", SCHEMA)
