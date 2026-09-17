"""Unit tests for shared/bulkhead.py (N3) - pure asyncio, no Dapr/FastAPI.

Two failure phases are deliberately distinguished (same exception type,
different message/log "phase"): "capacity" (never got a free slot in time)
vs "call" (got a slot, but the wrapped call itself was too slow) - see
Bulkhead.run()'s own docstring for why both matter operationally.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from shared.bulkhead import Bulkhead, BulkheadTimeoutError


async def test_run_returns_the_calls_result() -> None:
    bulkhead = Bulkhead(max_concurrency=2, timeout_seconds=1.0)

    async def call() -> str:
        return "ok"

    result = await bulkhead.run(call, label="test")

    assert result == "ok"


async def test_run_limits_concurrent_calls_to_max_concurrency() -> None:
    bulkhead = Bulkhead(max_concurrency=2, timeout_seconds=1.0)
    running = 0
    max_running = 0
    lock = asyncio.Lock()

    async def slow_call() -> str:
        nonlocal running, max_running
        async with lock:
            running += 1
            max_running = max(max_running, running)
        await asyncio.sleep(0.05)
        async with lock:
            running -= 1
        return "done"

    results = await asyncio.gather(*[bulkhead.run(slow_call, label="test") for _ in range(5)])

    assert results == ["done"] * 5
    assert max_running <= 2


async def test_run_raises_bulkhead_timeout_on_a_slow_call_with_a_free_slot() -> None:
    """acquired the slot fine - the call itself was too slow ("call" phase)."""
    bulkhead = Bulkhead(max_concurrency=1, timeout_seconds=0.05)

    async def slow_call() -> str:
        await asyncio.sleep(0.3)
        return "too slow"

    with pytest.raises(BulkheadTimeoutError, match="call"):
        await bulkhead.run(slow_call, label="groq")


async def test_run_raises_bulkhead_timeout_on_capacity_exhaustion() -> None:
    """The only slot is already held - simulated by acquiring the semaphore
    directly, not via run(), so the holder isn't itself subject to the same
    short timeout under test. A caller trying to acquire it should time out
    waiting, never even starting its own call ("capacity" phase)."""
    bulkhead = Bulkhead(max_concurrency=1, timeout_seconds=0.05)
    await bulkhead._semaphore.acquire()
    try:

        async def instant() -> str:
            return "second"

        with pytest.raises(BulkheadTimeoutError, match="capacity"):
            await bulkhead.run(instant, label="groq")
    finally:
        bulkhead._semaphore.release()


async def test_run_logs_the_timeout_phase_and_label(caplog: pytest.LogCaptureFixture) -> None:
    bulkhead = Bulkhead(max_concurrency=1, timeout_seconds=0.05)

    async def slow_call() -> str:
        await asyncio.sleep(0.3)
        return "too slow"

    with caplog.at_level(logging.WARNING, logger="shared.bulkhead"):
        with pytest.raises(BulkheadTimeoutError):
            await bulkhead.run(slow_call, label="groq")

    record = next(r for r in caplog.records if r.getMessage() == "bulkhead_timeout")
    assert record.label == "groq"  # type: ignore[attr-defined]
    assert record.phase == "call"  # type: ignore[attr-defined]


async def test_run_includes_correlation_id_in_the_log_when_given(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bulkhead = Bulkhead(max_concurrency=1, timeout_seconds=0.05)

    async def slow_call() -> str:
        await asyncio.sleep(0.3)
        return "too slow"

    with caplog.at_level(logging.WARNING, logger="shared.bulkhead"):
        with pytest.raises(BulkheadTimeoutError):
            await bulkhead.run(slow_call, label="approval-status", correlation_id="corr-1")

    record = next(r for r in caplog.records if r.getMessage() == "bulkhead_timeout")
    assert record.correlation_id == "corr-1"  # type: ignore[attr-defined]


async def test_an_external_cancelled_error_propagates_unchanged() -> None:
    """The wrapped call raising CancelledError (simulating the caller's own
    task being cancelled) must never be swallowed/converted - only this
    bulkhead's own internal timeout becomes BulkheadTimeoutError."""
    bulkhead = Bulkhead(max_concurrency=1, timeout_seconds=1.0)

    async def cancelled_call() -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await bulkhead.run(cancelled_call, label="test")


def test_max_concurrency_below_one_raises_value_error() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        Bulkhead(max_concurrency=0, timeout_seconds=1.0)


@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0])
def test_timeout_seconds_not_positive_raises_value_error(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        Bulkhead(max_concurrency=1, timeout_seconds=timeout_seconds)
