"""Dapr-state-backed NotificationRepository - the durable idempotency guard
behind Notification's three subscriptions (M10).

Key layout:
    notification:{tracking_id} -> "1"  (a bare marker string, not a
    JSON-serialized record - there is no PendingApproval/PaymentRecord
    equivalent here; see repository.py's docstring). Presence of the key IS
    the fact "already notified" - the value's content is never inspected.

No index key (contrast with DaprStateApprovalRepository's `approval:index`/
DaprStatePaymentRepository's `payment:index`) - nothing here ever needs to
*enumerate* notified tracking_ids.

mark_notified() writes via execute_state_transaction with a single
non-conditional operation (etag=None - already TransactionalStateOperation's
own default; passed explicitly here purely for self-documentation) rather
than save_state(), even though no concurrency/ETag concern exists for this
key (marking "notified" twice is harmless). This is purely for exception-
handling consistency: save_state()'s failure path raises
dapr.clients.exceptions.DaprInternalError - a plain Exception, NOT a
grpc.RpcError subclass (confirmed by reading the installed dapr==1.18.1
source in a prior phase) - which would silently break the `except
grpc.RpcError` idiom used everywhere else in this codebase.
execute_state_transaction's failure path is DaprGrpcError, a grpc.RpcError
subclass, matching that idiom.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import grpc
from dapr.clients.grpc._request import TransactionalStateOperation

from shared.dapr_client import LazyDaprClient

NOTIFICATION_KEY_PREFIX = "notification:"
_NOTIFIED_MARKER = "1"
_STORE_NAME = "statestore"


class NotificationRepositoryError(Exception):
    """Raised on any Dapr state failure - never caught silently (same
    fail-clean pattern as ApprovalRepositoryError/PaymentRepositoryError).
    Deliberately never caught inside NotificationService - propagates
    uncaught out of the subscription handler so Dapr redelivers later."""


class _StateResponse(Protocol):
    @property
    def data(self) -> bytes | str: ...


class _DaprStateClient(Protocol):
    async def get_state(self, store_name: str, key: str) -> _StateResponse: ...

    async def execute_state_transaction(
        self, store_name: str, operations: list[Any]
    ) -> object: ...


class DaprStateNotificationRepository:
    def __init__(
        self,
        *,
        client: _DaprStateClient | None = None,
        factory: Callable[[], _DaprStateClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprStateClient](client, factory=factory)

    async def already_notified(self, tracking_id: str) -> bool:
        try:
            response = await self._dapr.get().get_state(
                _STORE_NAME, f"{NOTIFICATION_KEY_PREFIX}{tracking_id}"
            )
        except grpc.RpcError as exc:
            raise NotificationRepositoryError(
                f"Failed to read notification state: {exc}"
            ) from exc
        return bool(response.data)

    async def mark_notified(self, tracking_id: str) -> None:
        operation = TransactionalStateOperation(
            key=f"{NOTIFICATION_KEY_PREFIX}{tracking_id}",
            data=_NOTIFIED_MARKER,
            etag=None,
        )
        try:
            await self._dapr.get().execute_state_transaction(_STORE_NAME, [operation])
        except grpc.RpcError as exc:
            raise NotificationRepositoryError(f"Failed to mark notified: {exc}") from exc
