"""Deterministic LLMProvider stub - CI/tests and the PLAN.md Fallback Strategy."""

from __future__ import annotations


class MockProvider:
    """A fixed, configurable response - no network call, no domain assumptions.

    Used when LLM integration is unavailable or risky: the real agent/graph
    still runs, only this provider is swapped in (PLAN.md Fallback Strategy).
    """

    def __init__(self, response: str = "This is a mock response from MockProvider.") -> None:
        self._response = response

    async def complete(
        self, system_prompt: str, user_message: str, *, json_mode: bool = False
    ) -> str:
        return self._response
