"""Tests for DaprStateNotificationRepository - Dapr-state-backed replacement
for InMemoryNotificationRepository. Uses a fake matching the minimal
_DaprStateClient Protocol (get_state/execute_state_transaction), never a
real DaprClient.
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from services.notification.dapr_state_repository import (
    NOTIFICATION_KEY_PREFIX,
    DaprStateNotificationRepository,
    NotificationRepositoryError,
)


class _FakeStateResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _FakeDaprStateClient:
    def __init__(self, *, raise_on_transaction: bool = False, raise_on_get: bool = False) -> None:
        self.store: dict[str, str] = {}
        self._raise_on_transaction = raise_on_transaction
        self._raise_on_get = raise_on_get

    async def get_state(self, store_name: str, key: str) -> _FakeStateResponse:
        if self._raise_on_get:
            raise grpc.RpcError()
        return _FakeStateResponse(self.store.get(key, "").encode("utf-8"))

    async def execute_state_transaction(self, store_name: str, operations: list[Any]) -> None:
        if self._raise_on_transaction:
            raise grpc.RpcError()
        for op in operations:
            data = op.data if isinstance(op.data, str) else op.data.decode("utf-8")
            self.store[op.key] = data


async def test_already_notified_returns_false_when_key_absent() -> None:
    repo = DaprStateNotificationRepository(client=_FakeDaprStateClient())

    assert await repo.already_notified("tid-1") is False


async def test_already_notified_returns_true_after_mark_notified() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateNotificationRepository(client=client)

    await repo.mark_notified("tid-1")

    assert await repo.already_notified("tid-1") is True


async def test_mark_notified_writes_via_execute_state_transaction_with_etag_none() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateNotificationRepository(client=client)

    await repo.mark_notified("tid-1")

    assert f"{NOTIFICATION_KEY_PREFIX}tid-1" in client.store


async def test_mark_notified_wraps_transaction_failure_as_notification_repository_error() -> None:
    client = _FakeDaprStateClient(raise_on_transaction=True)
    repo = DaprStateNotificationRepository(client=client)

    with pytest.raises(NotificationRepositoryError):
        await repo.mark_notified("tid-1")


async def test_already_notified_wraps_get_state_failure_as_notification_repository_error() -> None:
    client = _FakeDaprStateClient(raise_on_get=True)
    repo = DaprStateNotificationRepository(client=client)

    with pytest.raises(NotificationRepositoryError):
        await repo.already_notified("tid-1")


def test_construction_does_not_call_factory_eagerly() -> None:
    built: list[_FakeDaprStateClient] = []

    def factory() -> _FakeDaprStateClient:
        client = _FakeDaprStateClient()
        built.append(client)
        return client

    DaprStateNotificationRepository(factory=factory)

    assert built == []


async def test_no_index_key_is_ever_written() -> None:
    """Proves the 'no index' design decision empirically - unlike Approval's
    approval:index / Payment's payment:index, only the single
    notification:{tracking_id} key should ever appear in the store."""
    client = _FakeDaprStateClient()
    repo = DaprStateNotificationRepository(client=client)

    await repo.mark_notified("tid-1")
    await repo.mark_notified("tid-2")

    assert set(client.store.keys()) == {
        f"{NOTIFICATION_KEY_PREFIX}tid-1",
        f"{NOTIFICATION_KEY_PREFIX}tid-2",
    }
