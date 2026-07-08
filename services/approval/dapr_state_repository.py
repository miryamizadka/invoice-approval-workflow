"""Dapr-state-backed ApprovalRepository - the durable store behind M11's
pause/resume (a PENDING/WAITING_INFO item must survive an approval-service
restart, per PLAN.md's verification step 7).

Key layout:
    approval:{tracking_id} -> full PendingApproval, JSON
    approval:index         -> JSON array of tracking_ids, append-only

Both are written together in one atomic transaction (execute_state_transaction),
same invariant as DaprStateInvoiceRepository: if the transaction fails,
neither key changes.

Unlike Intake's dedup pointer, this index carries no business data - it is
the only mechanism to discover which tracking_ids exist at all, since this
key-value store has no Query API (no RediSearch/RedisJSON installed). It is
an interim solution forced by that limitation, not a fixed architectural
choice - a relational store with `WHERE status IN (...)` would not need it.

Known, accepted gaps (documented, not fixed here):
1. Read-then-write race on the index (same category as Intake's dedup-key
   race): two near-simultaneous saves for a brand-new tracking_id could both
   read the index before either appends. Low-risk here - one escalation
   event per invoice, not concurrent submits.
2. The index grows without bound - append-only, no delete/compaction. Not a
   bug; out of scope until a relational store replaces this entirely, at
   which point the index itself is expected to disappear, not be optimized.

Every tracking_id appears in the index at most once - save() checks before
appending, never blindly appends on every call.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

import grpc
from dapr.clients.grpc._request import TransactionalStateOperation

from services.approval.models import PendingApproval
from shared.dapr_client import LazyDaprClient

APPROVAL_KEY_PREFIX = "approval:"
APPROVAL_INDEX_KEY = "approval:index"

_STORE_NAME = "statestore"


class ApprovalRepositoryError(Exception):
    """Raised on any Dapr state failure - never caught silently (same
    fail-clean pattern as InvoiceRepositoryError)."""


class _StateResponse(Protocol):
    @property
    def data(self) -> bytes | str: ...


class _DaprStateClient(Protocol):
    async def get_state(self, store_name: str, key: str) -> _StateResponse: ...

    async def execute_state_transaction(
        self, store_name: str, operations: list[Any]
    ) -> object: ...


class DaprStateApprovalRepository:
    """Lazy client construction - same reasoning as DaprStateInvoiceRepository:
    a real DaprClient() blocks for up to 60s if no sidecar is reachable."""

    def __init__(
        self,
        *,
        client: _DaprStateClient | None = None,
        factory: Callable[[], _DaprStateClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprStateClient](client, factory=factory)

    async def _read_index(self) -> list[str]:
        try:
            response = await self._dapr.get().get_state(_STORE_NAME, APPROVAL_INDEX_KEY)
        except grpc.RpcError as exc:
            raise ApprovalRepositoryError(f"Failed to read approval index: {exc}") from exc
        if not response.data:
            return []
        raw = response.data if isinstance(response.data, str) else response.data.decode("utf-8")
        return list(json.loads(raw))

    async def save(self, approval: PendingApproval) -> None:
        index = await self._read_index()
        if approval.tracking_id not in index:
            index = [*index, approval.tracking_id]
        operations = [
            TransactionalStateOperation(
                key=f"{APPROVAL_KEY_PREFIX}{approval.tracking_id}",
                data=approval.model_dump_json(),
            ),
            TransactionalStateOperation(
                key=APPROVAL_INDEX_KEY,
                data=json.dumps(index),
            ),
        ]
        try:
            await self._dapr.get().execute_state_transaction(_STORE_NAME, operations)
        except grpc.RpcError as exc:
            raise ApprovalRepositoryError(f"Failed to save approval: {exc}") from exc

    async def get(self, tracking_id: str) -> PendingApproval | None:
        try:
            response = await self._dapr.get().get_state(
                _STORE_NAME, f"{APPROVAL_KEY_PREFIX}{tracking_id}"
            )
        except grpc.RpcError as exc:
            raise ApprovalRepositoryError(f"Failed to read approval: {exc}") from exc
        if not response.data:
            return None
        return PendingApproval.model_validate_json(response.data)

    async def list_pending(self) -> list[PendingApproval]:
        index = await self._read_index()
        result: list[PendingApproval] = []
        for tracking_id in index:
            approval = await self.get(tracking_id)
            if approval is not None:
                result.append(approval)
        return result
