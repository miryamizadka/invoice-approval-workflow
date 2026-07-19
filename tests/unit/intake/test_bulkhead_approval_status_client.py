"""Unit tests for BulkheadApprovalStatusClient (N3) - wraps any
ApprovalStatusClient with a shared.bulkhead.Bulkhead, so a slow/hung
Approval Service can't tie up every Intake worker. Mirrors
test_bulkhead_llm_provider.py's own structure - see that file's docstring
for why both call sites use the same underlying primitive.
"""

from __future__ import annotations

import asyncio

import pytest

from services.intake.approval_status_client import ApprovalStatusClientError
from services.intake.bulkhead_approval_status_client import BulkheadApprovalStatusClient
from services.intake.models import ApprovalStatusSnapshot


class _FakeApprovalStatusClient:
    def __init__(
        self, *, delay_seconds: float = 0.0, result: ApprovalStatusSnapshot | None = None
    ) -> None:
        self.delay_seconds = delay_seconds
        self.result = result
        self.call_count = 0
        self.last_tracking_id: str | None = None

    async def get_status(self, tracking_id: str) -> ApprovalStatusSnapshot | None:
        self.call_count += 1
        self.last_tracking_id = tracking_id
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.result


async def test_get_status_delegates_to_the_wrapped_client_and_returns_its_result() -> None:
    snapshot = ApprovalStatusSnapshot(status="pending", additional_info=None)
    fake = _FakeApprovalStatusClient(result=snapshot)
    client = BulkheadApprovalStatusClient(fake, max_concurrency=2, timeout_seconds=1.0)

    result = await client.get_status("corr-1")

    assert result == snapshot
    assert fake.call_count == 1
    assert fake.last_tracking_id == "corr-1"


async def test_get_status_returns_none_when_wrapped_client_returns_none() -> None:
    """None is a valid, non-error result (not-yet-escalated) - the bulkhead
    must pass it through unchanged, not treat it as a failure."""
    fake = _FakeApprovalStatusClient(result=None)
    client = BulkheadApprovalStatusClient(fake, max_concurrency=2, timeout_seconds=1.0)

    result = await client.get_status("corr-1")

    assert result is None


async def test_get_status_raises_approval_status_client_error_on_bulkhead_timeout() -> None:
    fake = _FakeApprovalStatusClient(delay_seconds=0.3)
    client = BulkheadApprovalStatusClient(fake, max_concurrency=1, timeout_seconds=0.05)

    with pytest.raises(ApprovalStatusClientError, match="bulkhead"):
        await client.get_status("corr-1")


async def test_get_status_limits_concurrent_calls_to_the_wrapped_client() -> None:
    snapshot = ApprovalStatusSnapshot(status="pending", additional_info=None)
    fake = _FakeApprovalStatusClient(delay_seconds=0.05, result=snapshot)
    client = BulkheadApprovalStatusClient(fake, max_concurrency=2, timeout_seconds=1.0)

    results = await asyncio.gather(*[client.get_status(f"corr-{i}") for i in range(5)])

    assert results == [snapshot] * 5
    assert fake.call_count == 5


def test_reads_max_concurrency_and_timeout_from_env_vars_when_not_given_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPROVAL_STATUS_BULKHEAD_MAX_CONCURRENCY", "7")
    monkeypatch.setenv("APPROVAL_STATUS_BULKHEAD_TIMEOUT_SECONDS", "2.5")

    client = BulkheadApprovalStatusClient(_FakeApprovalStatusClient())

    assert client._bulkhead._semaphore._value == 7  # noqa: SLF001
    assert client._bulkhead._timeout_seconds == 2.5  # noqa: SLF001


def test_falls_back_to_documented_defaults_when_env_vars_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APPROVAL_STATUS_BULKHEAD_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("APPROVAL_STATUS_BULKHEAD_TIMEOUT_SECONDS", raising=False)

    client = BulkheadApprovalStatusClient(_FakeApprovalStatusClient())

    assert client._bulkhead._semaphore._value == 20  # noqa: SLF001
    assert client._bulkhead._timeout_seconds == 5.0  # noqa: SLF001
