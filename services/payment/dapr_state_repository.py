"""Dapr-state-backed PaymentRepository and BudgetRepository - the durable
store behind Payment's saga (M9) and idempotency (M10).

Key layout:
    payment:{tracking_id} -> full PaymentRecord, JSON
    payment:index         -> JSON array of tracking_ids, append-only
    budget:{department}   -> Budget, JSON (no index - departments are a
                              small, fixed, config-known set from
                              policy/budgets.json; nothing needs to
                              *discover* them at runtime, unlike tracking_ids)

Both PaymentRecord keys are written together in one atomic transaction
(execute_state_transaction), same invariant as DaprStateApprovalRepository.
payment:index writes carry the etag read alongside the index, with the same
retry-on-conflict loop as Budget's below (_MAX_RETRIES/_RETRY_DELAY_SECONDS)
- same fix as DaprStateApprovalRepository.save() for the identical
documented read-then-write race on approval:index.

Budget writes go through execute_state_transaction with a per-operation
etag for optimistic concurrency (ARCHITECTURE.md's INV-1014 guarantee).
This is deliberate, not incidental: get_state() returns a real .etag, and
TransactionalStateOperation accepts etag= directly - an ETag conflict here
surfaces as DaprGrpcError, a grpc.RpcError subclass (confirmed by reading
the installed dapr==1.18.1 source), matching every existing `except
grpc.RpcError` in this codebase. save_state() is deliberately never used
for the conditional write - its ETag-conflict path raises DaprInternalError,
a plain Exception, NOT a grpc.RpcError subclass, which would silently break
that idiom.

Business failures (InsufficientBudgetError, BudgetNotFoundError) are a
different category from infra failures (BudgetRepositoryError, raised only
after retries are exhausted on a genuine ETag/RPC problem) - the former are
caught by PaymentService and become a terminal FAILED PaymentRecord; the
latter are deliberately NOT caught there, so they propagate uncaught out of
the subscription handler, causing Dapr to redeliver the event later once
the underlying infra issue clears.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol

import grpc
from dapr.clients.grpc._request import TransactionalStateOperation

from services.payment.models import Budget, PaymentRecord
from services.payment.repository import (
    BudgetCorruptionError,
    BudgetNotFoundError,
    InsufficientBudgetError,
)
from shared.dapr_client import LazyDaprClient

PAYMENT_KEY_PREFIX = "payment:"
PAYMENT_INDEX_KEY = "payment:index"
BUDGET_KEY_PREFIX = "budget:"

_STORE_NAME = "statestore"
_MAX_RETRIES = 5
_RETRY_DELAY_SECONDS = 0.01  # small, fixed - avoids a retry storm under real contention (INV-1014)


class PaymentRepositoryError(Exception):
    """Raised on any Dapr state failure saving/reading a PaymentRecord -
    never caught silently (same fail-clean pattern as ApprovalRepositoryError)."""


class BudgetRepositoryError(Exception):
    """Raised on a genuine Dapr state failure (or exhausted ETag-conflict
    retries) reading/writing a Budget - distinct from the business
    exceptions in repository.py (InsufficientBudgetError/BudgetNotFoundError),
    which PaymentService catches; this one is deliberately not caught there."""


class _StateResponse(Protocol):
    @property
    def data(self) -> bytes | str: ...
    @property
    def etag(self) -> str: ...


class _DaprStateClient(Protocol):
    async def get_state(self, store_name: str, key: str) -> _StateResponse: ...

    async def execute_state_transaction(
        self, store_name: str, operations: list[Any]
    ) -> object: ...


class DaprStatePaymentRepository:
    """Identical shape/invariants to DaprStateApprovalRepository, including
    the ETag-protected index write (see module docstring above). The
    remaining accepted gap is also identical: payment:index grows without
    bound - append-only, no delete/compaction (same non-goal as Approval's,
    see that file's module docstring)."""

    def __init__(
        self,
        *,
        client: _DaprStateClient | None = None,
        factory: Callable[[], _DaprStateClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprStateClient](client, factory=factory)

    async def _read_index(self) -> tuple[list[str], str]:
        try:
            response = await self._dapr.get().get_state(_STORE_NAME, PAYMENT_INDEX_KEY)
        except grpc.RpcError as exc:
            raise PaymentRepositoryError(f"Failed to read payment index: {exc}") from exc
        if not response.data:
            return [], response.etag
        raw = response.data if isinstance(response.data, str) else response.data.decode("utf-8")
        return list(json.loads(raw)), response.etag

    async def save(self, payment: PaymentRecord) -> None:
        for _ in range(_MAX_RETRIES):
            index, etag = await self._read_index()
            if payment.tracking_id not in index:
                index = [*index, payment.tracking_id]
            operations = [
                TransactionalStateOperation(
                    key=f"{PAYMENT_KEY_PREFIX}{payment.tracking_id}",
                    data=payment.model_dump_json(),
                ),
                TransactionalStateOperation(
                    key=PAYMENT_INDEX_KEY, data=json.dumps(index), etag=etag
                ),
            ]
            try:
                await self._dapr.get().execute_state_transaction(_STORE_NAME, operations)
                return
            except grpc.RpcError:
                # Stale index etag from a concurrent save() (or a genuine
                # transport failure - not distinguished, same assumption as
                # Budget's reserve()/release()) - re-read and retry.
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
        raise PaymentRepositoryError(
            f"Exceeded {_MAX_RETRIES} retries saving payment {payment.tracking_id!r}"
        )

    async def get(self, tracking_id: str) -> PaymentRecord | None:
        try:
            response = await self._dapr.get().get_state(
                _STORE_NAME, f"{PAYMENT_KEY_PREFIX}{tracking_id}"
            )
        except grpc.RpcError as exc:
            raise PaymentRepositoryError(f"Failed to read payment: {exc}") from exc
        if not response.data:
            return None
        return PaymentRecord.model_validate_json(response.data)

    async def list_all(self) -> list[PaymentRecord]:
        index, _ = await self._read_index()
        result: list[PaymentRecord] = []
        for tracking_id in index:
            payment = await self.get(tracking_id)
            if payment is not None:
                result.append(payment)
        return result


class DaprStateBudgetRepository:
    """No index (see module docstring) - departments are always known
    upfront from policy/budgets.json, unlike PaymentRecord's dynamically-
    created tracking_ids."""

    def __init__(
        self,
        *,
        client: _DaprStateClient | None = None,
        factory: Callable[[], _DaprStateClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprStateClient](client, factory=factory)

    async def ensure_seeded(self, department: str, total: Decimal) -> None:
        """Writes the seed Budget ONLY if the key is currently absent, so a
        service restart never resets an in-progress budget. If a concurrent
        replica seeds first between our read and write, our conditional
        write loses on a stale ETag; that failure is swallowed (not
        retried, not raised) since the desired end state - "this department
        has some prior seed" - is already true either way."""
        try:
            response = await self._dapr.get().get_state(
                _STORE_NAME, f"{BUDGET_KEY_PREFIX}{department}"
            )
        except grpc.RpcError as exc:
            raise BudgetRepositoryError(f"Failed to read budget for seeding: {exc}") from exc
        if response.data:
            return
        budget = Budget(department=department, total=total, remaining=total)
        operation = TransactionalStateOperation(
            key=f"{BUDGET_KEY_PREFIX}{department}",
            data=budget.model_dump_json(),
            etag=response.etag,
        )
        try:
            await self._dapr.get().execute_state_transaction(_STORE_NAME, [operation])
        except grpc.RpcError:
            return  # another writer seeded first between our read and write - fine

    async def get(self, department: str) -> Budget | None:
        try:
            response = await self._dapr.get().get_state(
                _STORE_NAME, f"{BUDGET_KEY_PREFIX}{department}"
            )
        except grpc.RpcError as exc:
            raise BudgetRepositoryError(f"Failed to read budget: {exc}") from exc
        if not response.data:
            return None
        return Budget.model_validate_json(response.data)

    async def reserve(self, department: str, amount: Decimal) -> None:
        for _ in range(_MAX_RETRIES):
            try:
                response = await self._dapr.get().get_state(
                    _STORE_NAME, f"{BUDGET_KEY_PREFIX}{department}"
                )
            except grpc.RpcError as exc:
                raise BudgetRepositoryError(f"Failed to read budget: {exc}") from exc
            if not response.data:
                raise BudgetNotFoundError(department)
            budget = Budget.model_validate_json(response.data)
            if budget.remaining < amount:
                raise InsufficientBudgetError(department)
            updated = budget.model_copy(update={"remaining": budget.remaining - amount})
            operation = TransactionalStateOperation(
                key=f"{BUDGET_KEY_PREFIX}{department}",
                data=updated.model_dump_json(),
                etag=response.etag,
            )
            try:
                await self._dapr.get().execute_state_transaction(_STORE_NAME, [operation])
                return
            except grpc.RpcError:
                # etag was stale (concurrent writer, exactly INV-1014) - re-read
                # and re-check on the next iteration, after a small fixed delay
                # to avoid a retry storm under real contention.
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
        raise BudgetRepositoryError(
            f"Exceeded {_MAX_RETRIES} retries reserving budget for {department!r}"
        )

    async def release(self, department: str, amount: Decimal) -> None:
        for _ in range(_MAX_RETRIES):
            try:
                response = await self._dapr.get().get_state(
                    _STORE_NAME, f"{BUDGET_KEY_PREFIX}{department}"
                )
            except grpc.RpcError as exc:
                raise BudgetRepositoryError(f"Failed to read budget: {exc}") from exc
            if not response.data:
                raise BudgetNotFoundError(department)
            budget = Budget.model_validate_json(response.data)
            new_remaining = budget.remaining + amount
            if new_remaining > budget.total:
                raise BudgetCorruptionError(
                    f"release() for {department!r} would push remaining "
                    f"({new_remaining}) above total ({budget.total})."
                )
            updated = budget.model_copy(update={"remaining": new_remaining})
            operation = TransactionalStateOperation(
                key=f"{BUDGET_KEY_PREFIX}{department}",
                data=updated.model_dump_json(),
                etag=response.etag,
            )
            try:
                await self._dapr.get().execute_state_transaction(_STORE_NAME, [operation])
                return
            except grpc.RpcError:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
        raise BudgetRepositoryError(
            f"Exceeded {_MAX_RETRIES} retries releasing budget for {department!r}"
        )
