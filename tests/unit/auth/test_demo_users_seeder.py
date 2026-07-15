"""Tests for services/auth/demo_users_seeder.py - loads the demo
Approver/Admin accounts at startup, mirroring
services/payment/budgets_loader.py's exact shape."""

from __future__ import annotations

from services.auth.demo_users_seeder import DEFAULT_DEMO_USERS_PATH, seed_demo_users
from services.auth.hashing import verify_password
from services.auth.repository import InMemoryUserRepository
from shared.auth import Role


def test_default_demo_users_path_exists_and_is_readable() -> None:
    assert DEFAULT_DEMO_USERS_PATH.is_file()


async def test_seed_demo_users_creates_approver_and_admin_with_hashed_passwords() -> None:
    repository = InMemoryUserRepository()

    await seed_demo_users(repository)

    approver = await repository.get_by_email("approver@example.com")
    admin = await repository.get_by_email("admin@example.com")
    assert approver is not None
    assert admin is not None
    assert approver.role == Role.APPROVER
    assert admin.role == Role.ADMIN
    assert verify_password(
        "ApproverDemo123!", password_hash=approver.password_hash, salt=approver.salt
    )
    assert approver.password_hash != "ApproverDemo123!"
