"""Tests for DaprStatePaymentRepository and DaprStateBudgetRepository -
Dapr-state-backed replacements for InMemoryPaymentRepository/
InMemoryBudgetRepository. Uses fakes matching the minimal _DaprStateClient
Protocol (get_state/execute_state_transaction), never a real DaprClient.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import grpc
import pytest

from services.payment.dapr_state_repository import (
    BUDGET_KEY_PREFIX,
    PAYMENT_INDEX_KEY,
    PAYMENT_KEY_PREFIX,
    BudgetRepositoryError,
    DaprStateBudgetRepository,
    DaprStatePaymentRepository,
    PaymentRepositoryError,
)
from services.payment.models import Budget, PaymentRecord, PaymentStatus
from services.payment.repository import (
    BudgetCorruptionError,
    BudgetNotFoundError,
    InsufficientBudgetError,
)
from shared.contracts.models import Decision, Route
from tests.support.decision_fixtures import clean_invoice


class _FakeStateResponse:
    def __init__(self, data: bytes, etag: str = "") -> None:
        self.data = data
        self.etag = etag


class _FakeDaprStateClient:
    """Tracks a per-key etag, incrementing on every successful write via
    execute_state_transaction - close enough to real Redis-backed Dapr state
    to exercise the optimistic-concurrency read-check-write loop."""

    def __init__(self, *, fail_transactions: int = 0) -> None:
        self.store: dict[str, str] = {}
        self.etags: dict[str, str] = {}
        self._etag_counter = 0
        self._fail_transactions = fail_transactions
        self.get_state_calls = 0
        self.transaction_calls = 0

    async def get_state(self, store_name: str, key: str) -> _FakeStateResponse:
        self.get_state_calls += 1
        data = self.store.get(key, "")
        return _FakeStateResponse(data.encode("utf-8"), self.etags.get(key, ""))

    async def execute_state_transaction(self, store_name: str, operations: list[Any]) -> None:
        self.transaction_calls += 1
        if self.transaction_calls <= self._fail_transactions:
            raise grpc.RpcError()
        for op in operations:
            if op.etag is not None and op.etag != self.etags.get(op.key, ""):
                raise grpc.RpcError()
        for op in operations:
            data = op.data if isinstance(op.data, str) else op.data.decode("utf-8")
            self.store[op.key] = data
            self._etag_counter += 1
            self.etags[op.key] = str(self._etag_counter)


def _decision() -> Decision:
    return Decision(
        route=Route.AUTO_APPROVE, reason="test reason", triggered_rules=[], correlation_id="corr-1"
    )


def _payment_record(tracking_id: str = "tid-1", **overrides: Any) -> PaymentRecord:
    base: dict[str, Any] = {
        "tracking_id": tracking_id,
        "invoice": clean_invoice(),
        "decision": _decision(),
        "status": PaymentStatus.RESERVED,
        "department": "engineering-2026Q2",
        "reserved_amount": Decimal("50.00"),
        "reason": None,
    }
    base.update(overrides)
    return PaymentRecord(**base)


# --- DaprStatePaymentRepository --------------------------------------------


async def test_save_writes_record_and_appends_to_index() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStatePaymentRepository(client=client)

    await repo.save(_payment_record(tracking_id="tid-1"))

    assert f"{PAYMENT_KEY_PREFIX}tid-1" in client.store
    assert json.loads(client.store[PAYMENT_INDEX_KEY]) == ["tid-1"]


async def test_get_returns_none_for_unknown_tracking_id() -> None:
    repo = DaprStatePaymentRepository(client=_FakeDaprStateClient())

    assert await repo.get("does-not-exist") is None


async def test_get_returns_saved_payment() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStatePaymentRepository(client=client)
    payment = _payment_record(tracking_id="tid-1")
    await repo.save(payment)

    assert await repo.get("tid-1") == payment


async def test_list_all_returns_empty_when_index_absent() -> None:
    repo = DaprStatePaymentRepository(client=_FakeDaprStateClient())

    assert await repo.list_all() == []


async def test_list_all_returns_saved_items_in_index_order() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStatePaymentRepository(client=client)
    first = _payment_record(tracking_id="tid-1")
    second = _payment_record(tracking_id="tid-2")
    await repo.save(first)
    await repo.save(second)

    assert await repo.list_all() == [first, second]


async def test_save_again_for_existing_tracking_id_does_not_duplicate_index_entry() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStatePaymentRepository(client=client)
    payment = _payment_record(tracking_id="tid-1")
    await repo.save(payment)

    updated = payment.model_copy(update={"status": PaymentStatus.COMPLETED})
    await repo.save(updated)

    assert json.loads(client.store[PAYMENT_INDEX_KEY]) == ["tid-1"]
    assert await repo.get("tid-1") == updated
    assert await repo.list_all() == [updated]


async def test_save_wraps_transaction_failure_as_payment_repository_error() -> None:
    client = _FakeDaprStateClient(fail_transactions=999)
    repo = DaprStatePaymentRepository(client=client)

    with pytest.raises(PaymentRepositoryError):
        await repo.save(_payment_record())


def test_payment_repository_construction_does_not_call_factory_eagerly() -> None:
    built: list[_FakeDaprStateClient] = []

    def factory() -> _FakeDaprStateClient:
        client = _FakeDaprStateClient()
        built.append(client)
        return client

    DaprStatePaymentRepository(factory=factory)

    assert built == []


# --- DaprStateBudgetRepository ----------------------------------------------


async def test_ensure_seeded_writes_budget_when_key_absent() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)

    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))

    assert f"{BUDGET_KEY_PREFIX}marketing-2026Q2" in client.store
    budget = Budget.model_validate_json(client.store[f"{BUDGET_KEY_PREFIX}marketing-2026Q2"])
    assert budget.remaining == Decimal("1000.00")


async def test_ensure_seeded_is_noop_when_key_already_present() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    await repo.reserve("marketing-2026Q2", Decimal("400.00"))

    await repo.ensure_seeded("marketing-2026Q2", Decimal("9999.00"))

    budget = await repo.get("marketing-2026Q2")
    assert budget is not None
    assert budget.remaining == Decimal("600.00")


async def test_ensure_seeded_swallows_conflict_when_another_writer_seeded_concurrently() -> None:
    client = _FakeDaprStateClient(fail_transactions=1)
    repo = DaprStateBudgetRepository(client=client)

    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))  # does not raise

    assert f"{BUDGET_KEY_PREFIX}marketing-2026Q2" not in client.store


async def test_get_returns_none_for_unknown_department() -> None:
    repo = DaprStateBudgetRepository(client=_FakeDaprStateClient())

    assert await repo.get("unknown-dept") is None


async def test_get_returns_seeded_budget() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))

    budget = await repo.get("marketing-2026Q2")

    assert budget == Budget(
        department="marketing-2026Q2", total=Decimal("1000.00"), remaining=Decimal("1000.00")
    )


async def test_reserve_writes_with_the_etag_just_read() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    etag_before = client.etags[f"{BUDGET_KEY_PREFIX}marketing-2026Q2"]

    await repo.reserve("marketing-2026Q2", Decimal("400.00"))

    # the write succeeded, which only happens if the etag we read matched -
    # confirmed indirectly via the etag counter advancing exactly once more.
    etag_after = client.etags[f"{BUDGET_KEY_PREFIX}marketing-2026Q2"]
    assert etag_after != etag_before
    budget = await repo.get("marketing-2026Q2")
    assert budget is not None
    assert budget.remaining == Decimal("600.00")


async def test_reserve_raises_insufficient_budget_error_without_writing() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    transaction_calls_before = client.transaction_calls

    with pytest.raises(InsufficientBudgetError):
        await repo.reserve("marketing-2026Q2", Decimal("1000.01"))

    assert client.transaction_calls == transaction_calls_before


async def test_reserve_raises_budget_not_found_error_for_unseeded_department() -> None:
    repo = DaprStateBudgetRepository(client=_FakeDaprStateClient())

    with pytest.raises(BudgetNotFoundError):
        await repo.reserve("unknown-dept", Decimal("1.00"))


async def test_reserve_retries_on_etag_conflict_and_succeeds_on_second_attempt() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    client._fail_transactions = client.transaction_calls + 1  # next transaction call fails once

    await repo.reserve("marketing-2026Q2", Decimal("400.00"))

    budget = await repo.get("marketing-2026Q2")
    assert budget is not None
    assert budget.remaining == Decimal("600.00")


async def test_reserve_raises_budget_repository_error_after_exhausting_retries() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    client._fail_transactions = client.transaction_calls + 999

    with pytest.raises(BudgetRepositoryError):
        await repo.reserve("marketing-2026Q2", Decimal("400.00"))


async def test_reserve_sleeps_between_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    client._fail_transactions = client.transaction_calls + 1

    await repo.reserve("marketing-2026Q2", Decimal("400.00"))

    assert len(sleep_calls) == 1


async def test_release_adds_back_to_remaining() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    await repo.reserve("marketing-2026Q2", Decimal("400.00"))

    await repo.release("marketing-2026Q2", Decimal("400.00"))

    budget = await repo.get("marketing-2026Q2")
    assert budget is not None
    assert budget.remaining == Decimal("1000.00")


async def test_release_retries_on_conflict_and_succeeds() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    await repo.reserve("marketing-2026Q2", Decimal("400.00"))
    client._fail_transactions = client.transaction_calls + 1

    await repo.release("marketing-2026Q2", Decimal("400.00"))

    budget = await repo.get("marketing-2026Q2")
    assert budget is not None
    assert budget.remaining == Decimal("1000.00")


async def test_release_raises_budget_not_found_error_for_unseeded_department() -> None:
    repo = DaprStateBudgetRepository(client=_FakeDaprStateClient())

    with pytest.raises(BudgetNotFoundError):
        await repo.release("unknown-dept", Decimal("1.00"))


async def test_release_raises_budget_corruption_error_without_writing() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateBudgetRepository(client=client)
    await repo.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    transaction_calls_before = client.transaction_calls

    with pytest.raises(BudgetCorruptionError):
        await repo.release("marketing-2026Q2", Decimal("0.01"))

    assert client.transaction_calls == transaction_calls_before


def test_budget_repository_construction_does_not_call_factory_eagerly() -> None:
    built: list[_FakeDaprStateClient] = []

    def factory() -> _FakeDaprStateClient:
        client = _FakeDaprStateClient()
        built.append(client)
        return client

    DaprStateBudgetRepository(factory=factory)

    assert built == []
