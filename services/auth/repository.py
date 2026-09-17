"""UserRepository Protocol - isolates AuthService from the storage
mechanism (DIP), same pattern as every other service's repository.py.

InMemoryUserRepository is the only implementation here, used in tests;
DaprStateUserRepository (dapr_state_repository.py) is the real one."""

from __future__ import annotations

from typing import Protocol

from services.auth.models import User


class UserAlreadyExistsError(Exception):
    """Raised by register() when the email is already taken."""


class InvalidCredentialsError(Exception):
    """Raised by login() for either an unknown email or a wrong password -
    deliberately the same exception for both, to avoid a user-enumeration
    signal in the error path."""


class UserRepository(Protocol):
    async def get_by_email(self, email: str) -> User | None: ...

    async def save(self, user: User) -> None: ...

    async def ensure_seeded(self, user: User) -> None:
        """Writes user only if the key is currently absent, so a service
        restart never resets/overwrites an existing account - same
        semantics as BudgetRepository.ensure_seeded."""
        ...


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._by_email: dict[str, User] = {}

    async def get_by_email(self, email: str) -> User | None:
        return self._by_email.get(email)

    async def save(self, user: User) -> None:
        self._by_email[user.email] = user

    async def ensure_seeded(self, user: User) -> None:
        self._by_email.setdefault(user.email, user)
