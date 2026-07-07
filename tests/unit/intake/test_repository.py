"""Unit tests for InMemoryInvoiceRepository."""

from __future__ import annotations

from typing import Any

import pytest

from services.intake.models import Submission, SubmissionStatus
from services.intake.repository import InMemoryInvoiceRepository
from tests.support.decision_fixtures import clean_invoice


def _submission(**overrides: Any) -> Submission:
    base: dict[str, Any] = {
        "tracking_id": "tid-1",
        "invoice": clean_invoice(),
        "dedup_key": "Test Vendor|TEST-0001|50.00",
        "is_duplicate": False,
        "status": SubmissionStatus.RECEIVED,
    }
    base.update(overrides)
    return Submission(**base)


@pytest.mark.asyncio
async def test_save_and_get_roundtrip() -> None:
    repository = InMemoryInvoiceRepository()
    submission = _submission()

    await repository.save(submission)
    result = await repository.get("tid-1")

    assert result == submission


@pytest.mark.asyncio
async def test_get_returns_none_when_not_found() -> None:
    repository = InMemoryInvoiceRepository()

    assert await repository.get("missing") is None


@pytest.mark.asyncio
async def test_find_by_dedup_key_finds_existing() -> None:
    repository = InMemoryInvoiceRepository()
    submission = _submission(dedup_key="unique-key")
    await repository.save(submission)

    result = await repository.find_by_dedup_key("unique-key")

    assert result == submission


@pytest.mark.asyncio
async def test_find_by_dedup_key_returns_none_when_absent() -> None:
    repository = InMemoryInvoiceRepository()

    assert await repository.find_by_dedup_key("nonexistent") is None


@pytest.mark.asyncio
async def test_save_updates_both_indexes_on_status_change() -> None:
    """A later save() (e.g. status transition) must remain reachable by both
    tracking_id and dedup_key - proves the repository doesn't just insert."""
    repository = InMemoryInvoiceRepository()
    submission = _submission(dedup_key="unique-key-2")
    await repository.save(submission)

    updated = submission.model_copy(update={"status": SubmissionStatus.COMPLETED})
    await repository.save(updated)

    assert await repository.get("tid-1") == updated
    assert await repository.find_by_dedup_key("unique-key-2") == updated
