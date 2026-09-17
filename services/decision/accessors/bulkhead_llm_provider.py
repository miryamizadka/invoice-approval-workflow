"""Bulkhead (N3) around any LLMProvider - caps concurrent Groq calls and
bounds their total latency, so a slow/hung LLM call can't tie up every
worker. Decision->Groq is the one call site in this project that isn't
Dapr-mediated at all (a direct HTTP call via the AsyncGroq SDK), so Dapr's
own resiliency policies can't apply here - hand-rolled with shared.bulkhead,
the same primitive services/intake/bulkhead_approval_status_client.py uses
for its own (Dapr-mediated) call site.
"""

from __future__ import annotations

import os

from pydantic import BaseModel

from services.decision.accessors.llm_provider import LLMProvider, LLMProviderError
from shared.bulkhead import Bulkhead, BulkheadTimeoutError

_DEFAULT_MAX_CONCURRENCY = 5
_DEFAULT_TIMEOUT_SECONDS = 15.0  # generous vs. real Groq latency (typically
# 1-5s) - and closes a real pre-existing gap: GroqProvider itself has no
# timeout at all today.


class BulkheadLLMProvider:
    """LLMProvider decorator - constructed once, wrapping whichever provider
    was resolved (Groq or Mock), at the same two call sites that already
    construct/rebuild the provider (see services/decision/service/app.py) -
    never inside build_decider()/Decider itself, which stay provider-
    implementation-agnostic. Wrap the resolved provider exactly once; the
    call sites' own `is not` identity check (app.py's _lifespan) already
    prevents re-wrapping an unchanged provider.

    No correlation_id is passed to the bulkhead's log line -
    LLMProvider.complete()'s Protocol has no per-call id in its signature,
    and widening it would ripple into GroqProvider, MockProvider, and every
    caller/test - out of scope for this wrapper.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_concurrency: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._provider = provider
        resolved_max_concurrency = max_concurrency
        if resolved_max_concurrency is None:
            resolved_max_concurrency = int(
                os.environ.get("LLM_BULKHEAD_MAX_CONCURRENCY", str(_DEFAULT_MAX_CONCURRENCY))
            )
        resolved_timeout_seconds = timeout_seconds
        if resolved_timeout_seconds is None:
            resolved_timeout_seconds = float(
                os.environ.get("LLM_BULKHEAD_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
            )
        self._bulkhead = Bulkhead(
            max_concurrency=resolved_max_concurrency, timeout_seconds=resolved_timeout_seconds
        )

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        json_mode: bool = False,
        schema: type[BaseModel] | None = None,
    ) -> str:
        try:
            return await self._bulkhead.run(
                lambda: self._provider.complete(
                    system_prompt, user_message, json_mode=json_mode, schema=schema
                ),
                label="groq",
            )
        except BulkheadTimeoutError as exc:
            raise LLMProviderError(str(exc)) from exc
