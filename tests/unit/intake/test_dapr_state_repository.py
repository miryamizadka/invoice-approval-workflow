"""Tests for DaprStateInvoiceRepository - Dapr-state-backed replacement for
InMemoryInvoiceRepository. Uses a fake matching the minimal _DaprStateClient
Protocol (save_state/get_state/execute_state_transaction), never a real
DaprClient.

Both the Submission record and the dedup pointer are written together in one
atomic transaction (see module docstring on DaprStateInvoiceRepository for
the invariants this relies on).
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from services.intake.dapr_state_repository import (
    DEDUP_KEY_PREFIX,
    SUBMISSION_KEY_PREFIX,
    DaprStateInvoiceRepository,
    InvoiceRepositoryError,
)
from services.intake.models import Submission, SubmissionStatus
from tests.support.decision_fixtures import clean_invoice


class _FakeStateResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _FakeDaprStateClient:
    def __init__(self, *, raise_on_transaction: bool = False) -> None:
        self.store: dict[str, str] = {}
        self._raise_on_transaction = raise_on_transaction

    async def get_state(self, store_name: str, key: str) -> _FakeStateResponse:
        return _FakeStateResponse(self.store.get(key, "").encode("utf-8"))

    async def execute_state_transaction(self, store_name: str, operations: list[Any]) -> None:
        if self._raise_on_transaction:
            raise grpc.RpcError()
        for op in operations:
            data = op.data if isinstance(op.data, str) else op.data.decode("utf-8")
            self.store[op.key] = data

    async def save_state(self, store_name: str, key: str, value: str) -> None:
        self.store[key] = value


def _submission(tracking_id: str = "tid-1", dedup_key: str = "dedup-1") -> Submission:
    return Submission(
        tracking_id=tracking_id,
        invoice=clean_invoice(),
        dedup_key=dedup_key,
        is_duplicate=False,
        status=SubmissionStatus.RECEIVED,
    )


async def test_save_writes_both_submission_and_dedup_pointer_keys() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateInvoiceRepository(client=client)

    await repo.save(_submission(tracking_id="tid-1", dedup_key="dedup-1"))

    assert f"{SUBMISSION_KEY_PREFIX}tid-1" in client.store
    assert client.store[f"{DEDUP_KEY_PREFIX}dedup-1"] == "tid-1"


async def test_get_returns_none_for_unknown_tracking_id() -> None:
    repo = DaprStateInvoiceRepository(client=_FakeDaprStateClient())

    assert await repo.get("does-not-exist") is None


async def test_get_returns_saved_submission() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateInvoiceRepository(client=client)
    submission = _submission(tracking_id="tid-1")
    await repo.save(submission)

    result = await repo.get("tid-1")

    assert result == submission


async def test_find_by_dedup_key_returns_none_for_unknown_key() -> None:
    repo = DaprStateInvoiceRepository(client=_FakeDaprStateClient())

    assert await repo.find_by_dedup_key("does-not-exist") is None


async def test_find_by_dedup_key_returns_submission_via_pointer() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateInvoiceRepository(client=client)
    submission = _submission(tracking_id="tid-1", dedup_key="dedup-1")
    await repo.save(submission)

    result = await repo.find_by_dedup_key("dedup-1")

    assert result == submission


async def test_find_by_dedup_key_raises_on_dangling_pointer() -> None:
    """Invariant: if a dedup pointer exists, its Submission must too. A
    pointer with no backing record is corruption - raise loudly, never
    return None silently (None here would wrongly mean "not a duplicate")."""
    client = _FakeDaprStateClient()
    client.store[f"{DEDUP_KEY_PREFIX}dedup-1"] = "tid-missing"
    repo = DaprStateInvoiceRepository(client=client)

    with pytest.raises(InvoiceRepositoryError):
        await repo.find_by_dedup_key("dedup-1")


async def test_save_wraps_transaction_failure_as_invoice_repository_error() -> None:
    client = _FakeDaprStateClient(raise_on_transaction=True)
    repo = DaprStateInvoiceRepository(client=client)

    with pytest.raises(InvoiceRepositoryError):
        await repo.save(_submission())


def test_construction_does_not_call_factory_eagerly() -> None:
    built: list[_FakeDaprStateClient] = []

    def factory() -> _FakeDaprStateClient:
        client = _FakeDaprStateClient()
        built.append(client)
        return client

    DaprStateInvoiceRepository(factory=factory)

    assert built == []
