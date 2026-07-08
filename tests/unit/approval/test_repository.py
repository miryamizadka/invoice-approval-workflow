"""Unit tests for InMemoryApprovalRepository."""

from __future__ import annotations

from typing import Any

import pytest

from services.approval.models import ApprovalStatus, PendingApproval
from services.approval.repository import InMemoryApprovalRepository
from shared.contracts.models import Decision, Route
from tests.support.decision_fixtures import clean_invoice


def _decision() -> Decision:
    return Decision(
        route=Route.HUMAN_REVIEW, reason="test reason", triggered_rules=[], correlation_id="corr-1"
    )


def _pending_approval(**overrides: Any) -> PendingApproval:
    base: dict[str, Any] = {
        "tracking_id": "tid-1",
        "invoice": clean_invoice(),
        "decision": _decision(),
        "recommendation": None,
        "status": ApprovalStatus.PENDING,
    }
    base.update(overrides)
    return PendingApproval(**base)


@pytest.mark.asyncio
async def test_save_and_get_roundtrip() -> None:
    repository = InMemoryApprovalRepository()
    approval = _pending_approval()

    await repository.save(approval)
    result = await repository.get("tid-1")

    assert result == approval


@pytest.mark.asyncio
async def test_get_returns_none_when_not_found() -> None:
    repository = InMemoryApprovalRepository()

    assert await repository.get("missing") is None


@pytest.mark.asyncio
async def test_list_pending_returns_empty_when_none_saved() -> None:
    repository = InMemoryApprovalRepository()

    assert await repository.list_pending() == []


@pytest.mark.asyncio
async def test_list_pending_returns_saved_items_in_insertion_order() -> None:
    repository = InMemoryApprovalRepository()
    first = _pending_approval(tracking_id="tid-1")
    second = _pending_approval(tracking_id="tid-2")

    await repository.save(first)
    await repository.save(second)

    assert await repository.list_pending() == [first, second]


@pytest.mark.asyncio
async def test_save_updates_existing_record_without_duplicating_in_list() -> None:
    """A later save() (status transition) must update in place, not append a
    second entry for the same tracking_id."""
    repository = InMemoryApprovalRepository()
    approval = _pending_approval(tracking_id="tid-1")
    await repository.save(approval)

    updated = approval.model_copy(update={"status": ApprovalStatus.APPROVED})
    await repository.save(updated)

    assert await repository.get("tid-1") == updated
    assert await repository.list_pending() == [updated]
