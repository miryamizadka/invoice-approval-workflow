"""Unit tests for BulkheadLLMProvider (N3) - wraps any LLMProvider with a
shared.bulkhead.Bulkhead, so a slow/hung Groq call can't tie up every
worker. This is the one Decision call site not Dapr-mediated at all
(a direct HTTP call to Groq's API), so Dapr's own resiliency policies
can't apply here - hand-rolled, using the same primitive Intake's own
bulkhead adapter uses (see test_bulkhead_approval_status_client.py).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from services.decision.accessors.bulkhead_llm_provider import BulkheadLLMProvider
from services.decision.accessors.llm_provider import LLMProvider, LLMProviderError


class _SampleSchema(BaseModel):
    answer: str


class _FakeProvider:
    def __init__(self, *, delay_seconds: float = 0.0, response: str = "ok") -> None:
        self.delay_seconds = delay_seconds
        self.response = response
        self.call_count = 0
        self.last_kwargs: dict[str, Any] | None = None

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        json_mode: bool = False,
        schema: type[BaseModel] | None = None,
    ) -> str:
        self.call_count += 1
        self.last_kwargs = {
            "system_prompt": system_prompt,
            "user_message": user_message,
            "json_mode": json_mode,
            "schema": schema,
        }
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.response


def test_satisfies_llm_provider_protocol() -> None:
    assert isinstance(BulkheadLLMProvider(_FakeProvider()), LLMProvider)


async def test_complete_delegates_to_the_wrapped_provider_and_returns_its_result() -> None:
    fake = _FakeProvider(response="delegated response")
    provider = BulkheadLLMProvider(fake, max_concurrency=2, timeout_seconds=1.0)

    result = await provider.complete("system", "user")

    assert result == "delegated response"
    assert fake.call_count == 1


async def test_complete_forwards_json_mode_and_schema_to_the_wrapped_provider() -> None:
    fake = _FakeProvider()
    provider = BulkheadLLMProvider(fake, max_concurrency=2, timeout_seconds=1.0)

    await provider.complete("system", "user", json_mode=True, schema=_SampleSchema)

    assert fake.last_kwargs == {
        "system_prompt": "system",
        "user_message": "user",
        "json_mode": True,
        "schema": _SampleSchema,
    }


async def test_complete_raises_llm_provider_error_on_bulkhead_timeout() -> None:
    fake = _FakeProvider(delay_seconds=0.3)
    provider = BulkheadLLMProvider(fake, max_concurrency=1, timeout_seconds=0.05)

    with pytest.raises(LLMProviderError, match="bulkhead"):
        await provider.complete("system", "user")


async def test_complete_limits_concurrent_calls_to_the_wrapped_provider() -> None:
    fake = _FakeProvider(delay_seconds=0.05)
    provider = BulkheadLLMProvider(fake, max_concurrency=2, timeout_seconds=1.0)

    results = await asyncio.gather(*[provider.complete("system", "user") for _ in range(5)])

    assert results == ["ok"] * 5
    assert fake.call_count == 5


def test_reads_max_concurrency_and_timeout_from_env_vars_when_not_given_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_BULKHEAD_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("LLM_BULKHEAD_TIMEOUT_SECONDS", "7.5")

    provider = BulkheadLLMProvider(_FakeProvider())

    assert provider._bulkhead._semaphore._value == 3  # noqa: SLF001 - internal check
    assert provider._bulkhead._timeout_seconds == 7.5  # noqa: SLF001 - internal check


def test_falls_back_to_documented_defaults_when_env_vars_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_BULKHEAD_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("LLM_BULKHEAD_TIMEOUT_SECONDS", raising=False)

    provider = BulkheadLLMProvider(_FakeProvider())

    assert provider._bulkhead._semaphore._value == 5  # noqa: SLF001
    assert provider._bulkhead._timeout_seconds == 15.0  # noqa: SLF001
