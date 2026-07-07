"""LLM provider contract (M15): decouples the agent from any specific model."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class LLMProviderError(Exception):
    """Raised on any provider failure - missing config, API/network errors.

    Never caught and silenced; callers let it propagate (fail clean, M15).
    """


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self, system_prompt: str, user_message: str, *, json_mode: bool = False
    ) -> str:
        """Return the model's raw text completion.

        Raw text, not a parsed Recommendation - interpreting/validating the
        agent's expected schema is the agent's job (Engine layer), not this
        Accessor's. json_mode requests a JSON-parseable string from
        providers that support it natively; it does not imply any specific
        schema - enforcing the Recommendation schema is decided when the
        agent is designed, not here.
        """
        ...
