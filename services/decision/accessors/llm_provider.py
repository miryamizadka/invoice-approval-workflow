"""LLM provider contract (M15): decouples the agent from any specific model."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class LLMProviderError(Exception):
    """Raised on any provider failure - missing config, API/network errors.

    Never caught and silenced; callers let it propagate (fail clean, M15).
    """


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        json_mode: bool = False,
        schema: type[BaseModel] | None = None,
    ) -> str:
        """Return the model's raw text completion.

        Raw text, not a parsed Recommendation - interpreting/validating the
        agent's expected schema is the agent's job (Engine layer), not this
        Accessor's. The Accessor accepts any Pydantic model class via
        `schema` and stays domain-agnostic - it never imports or knows about
        Recommendation specifically.

        schema, if given, requests strict constrained decoding: the
        provider guarantees the returned text parses into that exact
        schema, and takes precedence over json_mode (which becomes
        irrelevant when schema is set). json_mode alone requests a
        JSON-parseable string from providers that support it, without
        enforcing any particular shape.
        """
        ...
