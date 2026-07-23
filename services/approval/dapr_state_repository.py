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

Index writes use ETag-based optimistic concurrency with retry (same pattern
as services/payment/dapr_state_repository.py's reserve()/release(), INV-1014):
save() reads the index together with its ETag, and the transactional write
carries that ETag on the index operation only (never the per-record
operation). A stale ETag - another save() won the race - surfaces as
execute_state_transaction failing, which is retried with a fresh read up to
_MAX_RETRIES times before giving up as ApprovalRepositoryError.

Known, accepted gap (documented, not fixed here):
The index grows without bound - append-only, no delete/compaction. Not a
bug; out of scope until a relational store replaces this entirely, at
which point the index itself is expected to disappear, not be optimized.

Every tracking_id appears in the index at most once - save() checks before
appending, never blindly appends on every call.
"""

from __future__ import annotations

import asyncio
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
_MAX_RETRIES = 5
_RETRY_DELAY_SECONDS = 0.01  # small, fixed - avoids a retry storm under real contention


class ApprovalRepositoryError(Exception):
    """Raised on any Dapr state failure - never caught silently (same
    fail-clean pattern as InvoiceRepositoryError)."""


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

    async def _read_index(self) -> tuple[list[str], str]:
        try:
            response = await self._dapr.get().get_state(_STORE_NAME, APPROVAL_INDEX_KEY)
        except grpc.RpcError as exc:
            raise ApprovalRepositoryError(f"Failed to read approval index: {exc}") from exc
        if not response.data:
            return [], response.etag
        raw = response.data if isinstance(response.data, str) else response.data.decode("utf-8")
        return list(json.loads(raw)), response.etag

    async def save(self, approval: PendingApproval) -> None:
        for _ in range(_MAX_RETRIES):
            index, etag = await self._read_index()
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
                    etag=etag,
                ),
            ]
            try:
                await self._dapr.get().execute_state_transaction(_STORE_NAME, operations)
                return
            except grpc.RpcError:
                # Stale index etag from a concurrent save() (or a genuine
                # transport failure - not distinguished, same assumption as
                # Payment's reserve()/release()) - re-read and retry.
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
        raise ApprovalRepositoryError(
            f"Exceeded {_MAX_RETRIES} retries saving approval {approval.tracking_id!r}"
        )

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
        index, _ = await self._read_index()
        result: list[PendingApproval] = []
        for tracking_id in index:
            approval = await self.get(tracking_id)
            if approval is not None:
                result.append(approval)
        return result
