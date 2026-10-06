"""LLM interface and the Claude implementation."""
from typing import Protocol


class ConfigError(RuntimeError):
    """Required configuration (model id or API credentials) is missing."""


class LLMError(RuntimeError):
    """The LLM call failed or was declined. The message is safe to show a user."""


class LLM(Protocol):
    def generate(self, system: str, user: str, schema: dict) -> str:
        """Return the model's reply as text; `schema` is the JSON schema it must follow."""
        ...


class ClaudeLLM:
    """Calls Claude with a JSON-schema constrained reply (output_config.format).

    The model id comes from configuration (RAG_LLM_MODEL). Credentials come from the
    Anthropic SDK's own environment lookup (ANTHROPIC_API_KEY); this class never sees a key.
    """

    def __init__(self, model: str, client=None, max_tokens: int = 2000):
        if not model:
            raise ConfigError("RAG_LLM_MODEL is not set; set it to the Claude model id to use")
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def generate(self, system: str, user: str, schema: dict) -> str:
        import anthropic

        client = self._get_client()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.AuthenticationError as exc:
            raise ConfigError("Claude rejected the API credentials; check ANTHROPIC_API_KEY") from exc
        except anthropic.NotFoundError as exc:
            raise ConfigError(f"Claude model '{self.model}' was not found; check RAG_LLM_MODEL") from exc
        except anthropic.APIError as exc:
            raise LLMError(f"Claude API error ({type(exc).__name__})") from exc
        except TypeError as exc:
            # The SDK raises TypeError at request time when no credentials resolve.
            if "authentication method" in str(exc):
                raise ConfigError("no Claude credentials found; set ANTHROPIC_API_KEY") from exc
            raise

        if response.stop_reason == "refusal":
            raise LLMError("Claude declined to answer this request")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("Claude returned no text")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude's reply was cut off (max_tokens)")
        return text
