"""Dapr-state-backed UserRepository (N1) - the same interim-persistence
pattern already used for Invoice/Payment/Budget data (ARCHITECTURE.md
§8/§9), not a third persistence mechanism. Migrating to PostgreSQL later
would follow the exact same path already documented there.

Key layout: user:{email} -> User, JSON.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import grpc
from dapr.clients.grpc._request import TransactionalStateOperation

from services.auth.models import User
from shared.dapr_client import LazyDaprClient

USER_KEY_PREFIX = "user:"

_STORE_NAME = "statestore"


class UserRepositoryError(Exception):
    """Raised on any Dapr state failure - never caught silently."""


class _StateResponse(Protocol):
    @property
    def data(self) -> bytes | str: ...

    @property
    def etag(self) -> str: ...


class _DaprStateClient(Protocol):
    async def get_state(self, store_name: str, key: str) -> _StateResponse: ...

    async def execute_state_transaction(
        self, store_name: str, operations: list[object]
    ) -> object: ...


class DaprStateUserRepository:
    def __init__(
        self,
        *,
        client: _DaprStateClient | None = None,
        factory: Callable[[], _DaprStateClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprStateClient](client, factory=factory)

    async def get_by_email(self, email: str) -> User | None:
        try:
            response = await self._dapr.get().get_state(_STORE_NAME, f"{USER_KEY_PREFIX}{email}")
        except grpc.RpcError as exc:
            raise UserRepositoryError(f"Failed to read user: {exc}") from exc
        if not response.data:
            return None
        return User.model_validate_json(response.data)

    async def save(self, user: User) -> None:
        operation = TransactionalStateOperation(
            key=f"{USER_KEY_PREFIX}{user.email}", data=user.model_dump_json()
        )
        try:
            await self._dapr.get().execute_state_transaction(_STORE_NAME, [operation])
        except grpc.RpcError as exc:
            raise UserRepositoryError(f"Failed to save user: {exc}") from exc

    async def ensure_seeded(self, user: User) -> None:
        """Writes user ONLY if the key is currently absent, so a service
        restart never overwrites an existing (possibly already-modified)
        account - same semantics as BudgetRepository.ensure_seeded."""
        key = f"{USER_KEY_PREFIX}{user.email}"
        try:
            response = await self._dapr.get().get_state(_STORE_NAME, key)
        except grpc.RpcError as exc:
            raise UserRepositoryError(f"Failed to read user for seeding: {exc}") from exc
        if response.data:
            return
        operation = TransactionalStateOperation(
            key=key, data=user.model_dump_json(), etag=response.etag
        )
        try:
            await self._dapr.get().execute_state_transaction(_STORE_NAME, [operation])
        except grpc.RpcError:
            return  # another writer seeded first between our read and write - fine
