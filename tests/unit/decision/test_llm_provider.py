"""Tests for the LLM provider abstraction (M15).

GroqProvider is never tested against the real network (ADR-005: "stub in
CI") - a fake client is injected via the `client` constructor parameter.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from groq import GroqError
from pydantic import BaseModel

from services.decision.accessors.factory import get_llm_provider
from services.decision.accessors.groq_provider import DEFAULT_MODEL, GroqProvider
from services.decision.accessors.llm_provider import LLMProvider, LLMProviderError
from services.decision.accessors.mock_provider import MockProvider


class _SampleSchema(BaseModel):
    """A throwaway schema for exercising the `schema` parameter in tests."""

    answer: str


class _FakeCompletions:
    def __init__(self, *, content: str | None = None, error: Exception | None = None) -> None:
        self._content = content
        self._error = error
        self.last_kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeGroqClient:
    def __init__(self, *, content: str | None = None, error: Exception | None = None) -> None:
        self.completions = _FakeCompletions(content=content, error=error)
        self.chat = _FakeChat(self.completions)


@pytest.fixture(autouse=True)
def _no_real_dotenv_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests must not depend on this machine's real .env file.

    get_llm_provider() calls load_dotenv(), which would otherwise re-inject
    this developer's real GROQ_API_KEY into os.environ even after a test
    explicitly deletes it - making test outcomes depend on local machine
    state instead of being deterministic (CI has no .env at all).
    """
    monkeypatch.setattr("services.decision.accessors.factory.load_dotenv", lambda *a, **k: None)


# --- MockProvider ---------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_provider_returns_default_response() -> None:
    provider = MockProvider()
    result = await provider.complete("system", "user")
    assert result == "This is a mock response from MockProvider."


@pytest.mark.asyncio
async def test_mock_provider_returns_configured_response() -> None:
    provider = MockProvider(response="custom canned response")
    result = await provider.complete("system", "user", json_mode=True)
    assert result == "custom canned response"


def test_mock_provider_satisfies_llm_provider_protocol() -> None:
    assert isinstance(MockProvider(), LLMProvider)


@pytest.mark.asyncio
async def test_mock_provider_ignores_schema_even_when_response_does_not_match_it() -> None:
    """Locks in the design decision: Mock never validates against schema.

    The configured response deliberately does NOT match _SampleSchema - if
    MockProvider ever started validating, this test would catch it.
    """
    provider = MockProvider(response="not valid json for any schema")
    result = await provider.complete("system", "user", schema=_SampleSchema)
    assert result == "not valid json for any schema"


# --- Cross-provider Protocol conformance -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [
        MockProvider(),
        GroqProvider(api_key="k", client=_FakeGroqClient(content="{}")),
    ],
    ids=["mock", "groq"],
)
async def test_every_provider_accepts_the_schema_parameter(provider: LLMProvider) -> None:
    """Guards against a future provider silently forgetting to add `schema`
    to its complete() signature - would otherwise only surface as a
    TypeError the first time something actually passes schema=..., which
    could easily be much later than when the provider was added."""
    await provider.complete("system", "user", schema=_SampleSchema)


# --- GroqProvider: fail-fast (M15) ----------------------------------------


def test_groq_provider_fails_fast_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMProviderError, match="GROQ_API_KEY"):
        GroqProvider()


def test_groq_provider_accepts_explicit_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    provider = GroqProvider(api_key="explicit-key", client=_FakeGroqClient(content="ok"))
    assert isinstance(provider, LLMProvider)


def test_groq_provider_uses_default_model_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    provider = GroqProvider(api_key="k", client=_FakeGroqClient(content="ok"))
    assert provider._model == DEFAULT_MODEL  # noqa: SLF001 - internal check, no public getter needed


def test_groq_provider_reads_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_MODEL", "some-other-model")
    provider = GroqProvider(api_key="k", client=_FakeGroqClient(content="ok"))
    assert provider._model == "some-other-model"  # noqa: SLF001


# --- GroqProvider: complete() ----------------------------------------------


@pytest.mark.asyncio
async def test_groq_provider_returns_completion_content() -> None:
    fake_client = _FakeGroqClient(content="hello from groq")
    provider = GroqProvider(api_key="k", client=fake_client)
    result = await provider.complete("system prompt", "user message")
    assert result == "hello from groq"


@pytest.mark.asyncio
async def test_groq_provider_passes_json_mode_to_the_api() -> None:
    fake_client = _FakeGroqClient(content="{}")
    provider = GroqProvider(api_key="k", client=fake_client)
    await provider.complete("sys", "user", json_mode=True)
    assert fake_client.completions.last_kwargs is not None
    assert fake_client.completions.last_kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_groq_provider_omits_response_format_when_json_mode_false() -> None:
    fake_client = _FakeGroqClient(content="plain text")
    provider = GroqProvider(api_key="k", client=fake_client)
    await provider.complete("sys", "user", json_mode=False)
    assert fake_client.completions.last_kwargs is not None
    assert fake_client.completions.last_kwargs["response_format"] is None


@pytest.mark.asyncio
async def test_groq_provider_wraps_sdk_errors_as_llm_provider_error() -> None:
    fake_client = _FakeGroqClient(error=GroqError("simulated network failure"))
    provider = GroqProvider(api_key="k", client=fake_client)
    with pytest.raises(LLMProviderError, match="Groq API call failed"):
        await provider.complete("sys", "user")


@pytest.mark.asyncio
async def test_groq_provider_fails_clean_on_empty_completion() -> None:
    fake_client = _FakeGroqClient(content=None)
    provider = GroqProvider(api_key="k", client=fake_client)
    with pytest.raises(LLMProviderError, match="empty completion"):
        await provider.complete("sys", "user")


@pytest.mark.asyncio
async def test_groq_provider_builds_strict_json_schema_response_format() -> None:
    fake_client = _FakeGroqClient(content='{"answer": "ok"}')
    provider = GroqProvider(api_key="k", client=fake_client)
    await provider.complete("sys", "user", schema=_SampleSchema)
    assert fake_client.completions.last_kwargs is not None
    response_format = fake_client.completions.last_kwargs["response_format"]
    assert response_format == {
        "type": "json_schema",
        "json_schema": {
            "name": "_SampleSchema",
            "strict": True,
            "schema": _SampleSchema.model_json_schema(),
        },
    }


@pytest.mark.asyncio
async def test_groq_provider_schema_takes_precedence_over_json_mode() -> None:
    fake_client = _FakeGroqClient(content='{"answer": "ok"}')
    provider = GroqProvider(api_key="k", client=fake_client)
    await provider.complete("sys", "user", json_mode=True, schema=_SampleSchema)
    assert fake_client.completions.last_kwargs is not None
    assert fake_client.completions.last_kwargs["response_format"]["type"] == "json_schema"


# --- factory ----------------------------------------------------------------


def test_factory_defaults_to_mock_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    provider = get_llm_provider()
    assert isinstance(provider, MockProvider)


def test_factory_selects_mock_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    provider = get_llm_provider()
    assert isinstance(provider, MockProvider)


def test_factory_selects_groq_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-factory-test")
    provider = get_llm_provider()
    assert isinstance(provider, GroqProvider)


def test_factory_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "MOCK")
    provider = get_llm_provider()
    assert isinstance(provider, MockProvider)


def test_factory_fails_clean_on_unknown_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "not-a-real-provider")
    with pytest.raises(LLMProviderError, match="Unknown LLM_PROVIDER"):
        get_llm_provider()


def test_factory_propagates_missing_api_key_when_groq_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMProviderError, match="GROQ_API_KEY"):
        get_llm_provider()
