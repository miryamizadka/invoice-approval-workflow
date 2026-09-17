"""Bulkhead pattern (N3): caps concurrent calls to a specific dependency so
a slow/hung one can't exhaust capacity needed elsewhere. Pure asyncio, no
Dapr/FastAPI knowledge - used by two adapters, the only call sites in this
project not already decoupled via async Dapr pub/sub:
services/decision/accessors/bulkhead_llm_provider.py (Decision->Groq) and
services/intake/bulkhead_approval_status_client.py (Intake->Approval).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


class BulkheadTimeoutError(Exception):
    """Never allowed to propagate past a call site's own adapter - each
    adapter catches this and re-raises its existing domain error type
    (LLMProviderError / ApprovalStatusClientError), reusing the fail-clean
    fallback paths those already have."""


class Bulkhead:
    """Caps concurrent calls to `max_concurrency` and bounds total latency
    (time spent queueing for a slot, plus the call itself) to
    `timeout_seconds`.

    Deliberately takes a zero-arg callable, not a bare awaitable/coroutine -
    a coroutine object can only be awaited once, so a callable keeps this
    safe if `run()` ever grows retry logic later, at zero cost today (call
    sites already need a thin lambda either way to defer construction until
    the semaphore is acquired).

    Fairness: asyncio.Semaphore does not guarantee FIFO ordering between
    waiters as part of its documented API contract (CPython's current
    implementation happens to be roughly FIFO, but that's an implementation
    detail, not a guarantee) - acceptable for a throttle, not something to
    rely on for correctness.

    An external asyncio.CancelledError (e.g. the caller's own task being
    cancelled for a reason unrelated to this bulkhead) is deliberately never
    caught here - only this bulkhead's own internal timeout is translated
    into BulkheadTimeoutError. asyncio.timeout() raises TimeoutError (not
    CancelledError) at the `async with` boundary when its own deadline
    fires, which is exactly what the `except TimeoutError` below is scoped
    to; CancelledError is a BaseException subtype and simply isn't caught
    by that clause, so it propagates unchanged.
    """

    def __init__(self, *, max_concurrency: int, timeout_seconds: float) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout_seconds = timeout_seconds

    async def run(
        self,
        call: Callable[[], Coroutine[Any, Any, T]],
        *,
        label: str,
        correlation_id: str | None = None,
    ) -> T:
        """`label` identifies the dependency being called (e.g. "groq",
        "approval-status"), for the log line below. `correlation_id` is
        optional - Intake's adapter has one (the tracking id, already used
        as this project's correlation id everywhere); Decision's LLMProvider
        Protocol has no per-call id in its signature, so its adapter omits
        one rather than widening that Protocol just for this."""
        acquired = False
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._semaphore:
                    acquired = True
                    return await call()
        except TimeoutError as exc:
            phase = "call" if acquired else "capacity"
            logger.warning(
                "bulkhead_timeout",
                extra={
                    "label": label,
                    "phase": phase,
                    "timeout_seconds": self._timeout_seconds,
                    "correlation_id": correlation_id,
                },
            )
            raise BulkheadTimeoutError(
                f"{label}: bulkhead {phase} timeout after {self._timeout_seconds}s"
            ) from exc
