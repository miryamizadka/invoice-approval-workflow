"""Real LLMProvider backed by Groq's API."""

from __future__ import annotations

import os
from typing import Any, Protocol

from groq import AsyncGroq, GroqError

from services.decision.accessors.llm_provider import LLMProviderError

DEFAULT_MODEL = "openai/gpt-oss-120b"


class _CompletionMessage(Protocol):
    @property
    def content(self) -> str | None: ...


class _CompletionChoice(Protocol):
    @property
    def message(self) -> _CompletionMessage: ...


class _ChatCompletion(Protocol):
    @property
    def choices(self) -> list[_CompletionChoice]: ...


class _Completions(Protocol):
    async def create(self, **kwargs: Any) -> _ChatCompletion: ...


class _Chat(Protocol):
    @property
    def completions(self) -> _Completions: ...


class GroqClient(Protocol):
    """The minimal shape GroqProvider needs - not the full AsyncGroq API.

    Lets tests inject a lightweight fake instead of the real SDK client,
    with mypy still verifying the fake matches what's actually used.
    Attributes are declared as read-only properties (not plain fields) so
    Protocol matching is covariant, not invariant - otherwise a fake class
    whose attribute type is merely compatible (not identical) would fail
    mypy even though it's perfectly usable at runtime.
    """

    @property
    def chat(self) -> _Chat: ...


class GroqProvider:
    """LLMProvider implementation calling Groq's chat completions API.

    Fails fast at construction if no API key is available - never lazily
    at the first request (M15).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: GroqClient | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("GROQ_API_KEY")
        if not resolved_key:
            raise LLMProviderError("GROQ_API_KEY is not set; cannot construct GroqProvider.")
        self._model = model or os.environ.get("GROQ_MODEL", DEFAULT_MODEL)
        self._client = client or AsyncGroq(api_key=resolved_key)

    async def complete(
        self, system_prompt: str, user_message: str, *, json_mode: bool = False
    ) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"} if json_mode else None,
            )
        except GroqError as exc:
            raise LLMProviderError(f"Groq API call failed ({self._model}): {exc}") from exc
        content = response.choices[0].message.content
        if not content:
            raise LLMProviderError(f"Groq returned an empty completion ({self._model}).")
        return content
