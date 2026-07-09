"""Unit tests for InMemoryNotificationRepository."""

from __future__ import annotations

import pytest

from services.notification.repository import InMemoryNotificationRepository


@pytest.mark.asyncio
async def test_already_notified_returns_false_for_unknown_tracking_id() -> None:
    repository = InMemoryNotificationRepository()

    assert await repository.already_notified("tid-1") is False


@pytest.mark.asyncio
async def test_already_notified_returns_true_after_mark_notified() -> None:
    repository = InMemoryNotificationRepository()

    await repository.mark_notified("tid-1")

    assert await repository.already_notified("tid-1") is True


@pytest.mark.asyncio
async def test_mark_notified_is_idempotent_calling_twice_does_not_raise() -> None:
    repository = InMemoryNotificationRepository()

    await repository.mark_notified("tid-1")
    await repository.mark_notified("tid-1")

    assert await repository.already_notified("tid-1") is True
