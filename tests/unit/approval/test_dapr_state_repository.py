"""Tests for DaprStateApprovalRepository - Dapr-state-backed replacement for
InMemoryApprovalRepository. Uses a fake matching the minimal _DaprStateClient
Protocol (get_state/execute_state_transaction), never a real DaprClient.

Key layout: approval:{tracking_id} -> full PendingApproval, JSON.
approval:index -> JSON array of tracking_ids, append-only, each id at most
once (see module docstring on DaprStateApprovalRepository for invariants).
"""

from __future__ import annotations

import json
from typing import Any

import grpc
import pytest

from services.approval.dapr_state_repository import (
    APPROVAL_INDEX_KEY,
    APPROVAL_KEY_PREFIX,
    ApprovalRepositoryError,
    DaprStateApprovalRepository,
)
from services.approval.models import ApprovalStatus, PendingApproval
from shared.contracts.models import Decision, Route
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


def _decision() -> Decision:
    return Decision(
        route=Route.HUMAN_REVIEW, reason="test reason", triggered_rules=[], correlation_id="corr-1"
    )


def _pending_approval(tracking_id: str = "tid-1", **overrides: Any) -> PendingApproval:
    base: dict[str, Any] = {
        "tracking_id": tracking_id,
        "invoice": clean_invoice(),
        "decision": _decision(),
        "recommendation": None,
        "status": ApprovalStatus.PENDING,
    }
    base.update(overrides)
    return PendingApproval(**base)


async def test_save_writes_record_and_appends_to_index() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateApprovalRepository(client=client)

    await repo.save(_pending_approval(tracking_id="tid-1"))

    assert f"{APPROVAL_KEY_PREFIX}tid-1" in client.store
    assert json.loads(client.store[APPROVAL_INDEX_KEY]) == ["tid-1"]


async def test_get_returns_none_for_unknown_tracking_id() -> None:
    repo = DaprStateApprovalRepository(client=_FakeDaprStateClient())

    assert await repo.get("does-not-exist") is None


async def test_get_returns_saved_approval() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateApprovalRepository(client=client)
    approval = _pending_approval(tracking_id="tid-1")
    await repo.save(approval)

    result = await repo.get("tid-1")

    assert result == approval


async def test_list_pending_returns_empty_when_index_absent() -> None:
    repo = DaprStateApprovalRepository(client=_FakeDaprStateClient())

    assert await repo.list_pending() == []


async def test_list_pending_returns_saved_items_in_index_order() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateApprovalRepository(client=client)
    first = _pending_approval(tracking_id="tid-1")
    second = _pending_approval(tracking_id="tid-2")
    await repo.save(first)
    await repo.save(second)

    assert await repo.list_pending() == [first, second]


async def test_save_again_for_existing_tracking_id_does_not_duplicate_index_entry() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateApprovalRepository(client=client)
    approval = _pending_approval(tracking_id="tid-1")
    await repo.save(approval)

    updated = approval.model_copy(update={"status": ApprovalStatus.APPROVED})
    await repo.save(updated)

    assert json.loads(client.store[APPROVAL_INDEX_KEY]) == ["tid-1"]
    assert await repo.get("tid-1") == updated
    assert await repo.list_pending() == [updated]


async def test_save_wraps_transaction_failure_as_approval_repository_error() -> None:
    client = _FakeDaprStateClient(raise_on_transaction=True)
    repo = DaprStateApprovalRepository(client=client)

    with pytest.raises(ApprovalRepositoryError):
        await repo.save(_pending_approval())


def test_construction_does_not_call_factory_eagerly() -> None:
    built: list[_FakeDaprStateClient] = []

    def factory() -> _FakeDaprStateClient:
        client = _FakeDaprStateClient()
        built.append(client)
        return client

    DaprStateApprovalRepository(factory=factory)

    assert built == []
