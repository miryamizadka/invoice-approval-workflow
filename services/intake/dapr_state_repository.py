"""Dapr-state-backed InvoiceRepository - an interim durable store until a
real PostgreSQL repository exists (see PLAN.md's migration path:
InMemoryInvoiceRepository -> DaprStateInvoiceRepository -> PostgresInvoiceRepository).

Key layout (see PLAN.md for the diagram):
    submission:{tracking_id} -> full Submission, JSON
    dedup:{dedup_key}        -> tracking_id only, a pointer, NEVER a Submission

Both keys are written together in one atomic transaction (execute_state_transaction),
never as two separate save_state calls - this is the invariant the rest of this
class relies on:
1. Atomicity: if the transaction fails, neither key is written.
2. Pointer/record consistency: if dedup:{key} exists, submission:{tracking_id}
   it points to MUST also exist (there is no delete() in this Protocol, so
   nothing else can break this once written). If this is ever violated, it's
   corruption, not a valid state - find_by_dedup_key() raises rather than
   returning None (None here would wrongly mean "not a duplicate").
3. tracking_id never changes once assigned (IntakeService.submit() generates
   it once) - it is now also this repository's primary key.

Known, accepted gap (documented, not fixed here): the "does this dedup_key
exist" check and the save() that follows are no longer atomic with each other
the way two in-memory dict lookups were (see InMemoryInvoiceRepository) - Dapr
state is real network I/O, so a window exists between them. Two near-
simultaneous submits of the exact same invoice could both pass the check
before either saves. This is not a "missing database" problem in general -
it is specifically a "key-value store, not a relational one" problem: a
Postgres unique constraint would reject the second insert atomically; Dapr's
ETag mechanism is built for update-vs-update optimistic concurrency (exactly
what Payment's budget reservation needs per ARCHITECTURE.md's INV-1014), not
"insert only if absent". The eventual fix is a PostgreSQL unique constraint,
not extending Dapr state - deferred until Approval/Payment need real
persistent business data anyway (PLAN.md).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import grpc
from dapr.clients.grpc._request import TransactionalStateOperation

from services.intake.models import Submission
from shared.dapr_client import LazyDaprClient

SUBMISSION_KEY_PREFIX = "submission:"
DEDUP_KEY_PREFIX = "dedup:"

_STORE_NAME = "statestore"


class InvoiceRepositoryError(Exception):
    """Raised on any Dapr state failure - never caught silently (same
    fail-clean pattern as DecisionPublisherError/DecisionClientError)."""


class _StateResponse(Protocol):
    @property
    def data(self) -> bytes | str: ...


class _DaprStateClient(Protocol):
    async def get_state(self, store_name: str, key: str) -> _StateResponse: ...

    async def execute_state_transaction(
        self, store_name: str, operations: list[Any]
    ) -> object: ...


class DaprStateInvoiceRepository:
    """Lazy client construction, same reasoning as DaprDecisionPublisher: a
    real DaprClient() blocks for up to 60s retrying a sidecar health check
    if none is reachable - confirmed empirically. LazyDaprClient
    (shared/dapr_client.py) builds it once, on first actual use, and reuses
    it after that."""

    def __init__(
        self,
        *,
        client: _DaprStateClient | None = None,
        factory: Callable[[], _DaprStateClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprStateClient](client, factory=factory)

    async def save(self, submission: Submission) -> None:
        operations = [
            TransactionalStateOperation(
                key=f"{SUBMISSION_KEY_PREFIX}{submission.tracking_id}",
                data=submission.model_dump_json(),
            ),
            TransactionalStateOperation(
                key=f"{DEDUP_KEY_PREFIX}{submission.dedup_key}",
                data=submission.tracking_id,
            ),
        ]
        try:
            await self._dapr.get().execute_state_transaction(_STORE_NAME, operations)
        except grpc.RpcError as exc:
            raise InvoiceRepositoryError(f"Failed to save submission: {exc}") from exc

    async def get(self, tracking_id: str) -> Submission | None:
        try:
            response = await self._dapr.get().get_state(
                _STORE_NAME, f"{SUBMISSION_KEY_PREFIX}{tracking_id}"
            )
        except grpc.RpcError as exc:
            raise InvoiceRepositoryError(f"Failed to read submission: {exc}") from exc
        if not response.data:
            return None
        return Submission.model_validate_json(response.data)

    async def find_by_dedup_key(self, dedup_key: str) -> Submission | None:
        try:
            response = await self._dapr.get().get_state(
                _STORE_NAME, f"{DEDUP_KEY_PREFIX}{dedup_key}"
            )
        except grpc.RpcError as exc:
            raise InvoiceRepositoryError(f"Failed to read dedup pointer: {exc}") from exc
        if not response.data:
            return None
        tracking_id = (
            response.data if isinstance(response.data, str) else response.data.decode("utf-8")
        )
        submission = await self.get(tracking_id)
        if submission is None:
            raise InvoiceRepositoryError(
                f"dedup pointer for {dedup_key!r} references missing submission "
                f"{tracking_id!r} - invariant violated (pointer without a record)."
            )
        return submission
