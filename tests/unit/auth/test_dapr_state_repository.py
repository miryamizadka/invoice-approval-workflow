"""Tests for DaprStateUserRepository - Dapr-state-backed UserRepository (N1),
the same interim-persistence pattern already used for Invoice/Payment/
Budget data. Fake matches the minimal _DaprStateClient Protocol
(get_state/execute_state_transaction), never a real DaprClient - same
style as tests/unit/payment/test_dapr_state_repository.py.
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from services.auth.dapr_state_repository import (
    USER_KEY_PREFIX,
    DaprStateUserRepository,
    UserRepositoryError,
)
from services.auth.hashing import hash_password
from services.auth.models import User
from shared.auth import Role


class _FakeStateResponse:
    def __init__(self, data: bytes, etag: str = "") -> None:
        self.data = data
        self.etag = etag


class _FakeDaprStateClient:
    def __init__(self, *, fail_transactions: int = 0) -> None:
        self.store: dict[str, str] = {}
        self.etags: dict[str, str] = {}
        self._etag_counter = 0
        self._fail_transactions = fail_transactions
        self.transaction_calls = 0

    async def get_state(self, store_name: str, key: str) -> _FakeStateResponse:
        data = self.store.get(key, "")
        return _FakeStateResponse(data.encode("utf-8"), self.etags.get(key, ""))

    async def execute_state_transaction(self, store_name: str, operations: list[Any]) -> None:
        self.transaction_calls += 1
        if self.transaction_calls <= self._fail_transactions:
            raise grpc.RpcError()
        for op in operations:
            data = op.data if isinstance(op.data, str) else op.data.decode("utf-8")
            self.store[op.key] = data
            self._etag_counter += 1
            self.etags[op.key] = str(self._etag_counter)


def _user(email: str = "alice@example.com", role: Role = Role.SUBMITTER) -> User:
    password_hash, salt = hash_password("hunter2")
    return User(email=email, password_hash=password_hash, salt=salt, role=role)


async def test_save_and_get_by_email_round_trip() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateUserRepository(client=client)
    user = _user()

    await repo.save(user)

    assert await repo.get_by_email("alice@example.com") == user
    assert f"{USER_KEY_PREFIX}alice@example.com" in client.store


async def test_get_by_email_returns_none_for_unknown_email() -> None:
    repo = DaprStateUserRepository(client=_FakeDaprStateClient())

    assert await repo.get_by_email("nobody@example.com") is None


async def test_save_wraps_transaction_failure_as_user_repository_error() -> None:
    client = _FakeDaprStateClient(fail_transactions=999)
    repo = DaprStateUserRepository(client=client)

    with pytest.raises(UserRepositoryError):
        await repo.save(_user())


async def test_ensure_seeded_writes_when_key_absent() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateUserRepository(client=client)
    user = _user(email="approver@example.com", role=Role.APPROVER)

    await repo.ensure_seeded(user)

    assert await repo.get_by_email("approver@example.com") == user


async def test_ensure_seeded_is_noop_when_key_already_present() -> None:
    client = _FakeDaprStateClient()
    repo = DaprStateUserRepository(client=client)
    original = _user(email="approver@example.com", role=Role.APPROVER)
    await repo.save(original)
    different = _user(email="approver@example.com", role=Role.ADMIN)

    await repo.ensure_seeded(different)

    assert await repo.get_by_email("approver@example.com") == original


async def test_ensure_seeded_swallows_conflict_when_another_writer_seeded_concurrently() -> None:
    client = _FakeDaprStateClient(fail_transactions=1)
    repo = DaprStateUserRepository(client=client)

    await repo.ensure_seeded(_user())  # does not raise

    assert f"{USER_KEY_PREFIX}alice@example.com" not in client.store


def test_construction_does_not_call_factory_eagerly() -> None:
    built: list[_FakeDaprStateClient] = []

    def factory() -> _FakeDaprStateClient:
        client = _FakeDaprStateClient()
        built.append(client)
        return client

    DaprStateUserRepository(factory=factory)

    assert built == []
