"""Deterministic LLMProvider stub - CI/tests and the PLAN.md Fallback Strategy."""

from __future__ import annotations

from pydantic import BaseModel


class MockProvider:
    """A fixed, configurable response - no network call, no domain assumptions.

    Used when LLM integration is unavailable or risky: the real agent/graph
    still runs, only this provider is swapped in (PLAN.md Fallback Strategy).

    Deliberately ignores `schema`: it never validates the configured
    response against it, even if the response wouldn't actually parse as
    that schema. Callers who need a schema-shaped mock reply are
    responsible for configuring `response` accordingly.
    """

    def __init__(self, response: str = "This is a mock response from MockProvider.") -> None:
        self._response = response

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        json_mode: bool = False,
        schema: type[BaseModel] | None = None,
    ) -> str:
        return self._response
