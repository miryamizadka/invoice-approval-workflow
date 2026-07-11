"""Unit tests for ApprovalService - the Manager orchestrating F4/F5/M11.

Uses InMemoryApprovalRepository and a stub ApprovalOutcomePublisher, no Dapr.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.approval.models import ApprovalStatus
from services.approval.repository import InMemoryApprovalRepository
from services.approval.service import (
    ApprovalAlreadyResolvedError,
    ApprovalNotAwaitingInfoError,
    ApprovalNotFoundError,
    ApprovalService,
    build_approval_service,
)
from shared.contracts.models import (
    ApprovalCompletedEvent,
    ApprovalResolution,
    Decision,
    DecisionCompletedEvent,
    Recommendation,
    RecommendationType,
    Route,
)
from tests.support.decision_fixtures import clean_invoice


class _StubOutcomePublisher:
    def __init__(self) -> None:
        self.published: list[ApprovalCompletedEvent] = []

    async def publish(self, event: ApprovalCompletedEvent) -> None:
        self.published.append(event)


def _decision(route: Route = Route.HUMAN_REVIEW) -> Decision:
    return Decision(route=route, reason="test reason", triggered_rules=[], correlation_id="corr-1")


def _recommendation() -> Recommendation:
    return Recommendation(
        recommendation=RecommendationType.APPROVE,
        confidence=0.9,
        cited_rules=[],
        reasoning="looks fine",
    )


def _decision_completed_event(
    route: Route = Route.HUMAN_REVIEW, recommendation: Recommendation | None = None
) -> DecisionCompletedEvent:
    return DecisionCompletedEvent(
        invoice=clean_invoice(), decision=_decision(route), recommendation=recommendation
    )


def _service(
    **overrides: Any,
) -> tuple[ApprovalService, InMemoryApprovalRepository, _StubOutcomePublisher]:
    repository = overrides.get("repository") or InMemoryApprovalRepository()
    publisher = overrides.get("publisher") or _StubOutcomePublisher()
    return build_approval_service(repository, publisher), repository, publisher


@pytest.mark.asyncio
async def test_handle_decision_completed_ignores_non_human_review_routes() -> None:
    service, repository, _ = _service()
    event = _decision_completed_event(route=Route.AUTO_APPROVE)

    await service.handle_decision_completed(event)

    assert await repository.list_pending() == []


@pytest.mark.asyncio
async def test_handle_decision_completed_saves_pending_for_human_review() -> None:
    service, repository, _ = _service()
    recommendation = _recommendation()
    event = _decision_completed_event(recommendation=recommendation)

    await service.handle_decision_completed(event)

    pending = await repository.list_pending()
    assert len(pending) == 1
    assert pending[0].tracking_id == "corr-1"
    assert pending[0].status == ApprovalStatus.PENDING
    assert pending[0].recommendation == recommendation


@pytest.mark.asyncio
async def test_handle_decision_completed_is_idempotent_after_approval() -> None:
    """The critical redelivery bug fix: a decision.completed redelivered
    after a human already approved must NOT revert the status back to
    PENDING."""
    service, repository, publisher = _service()
    event = _decision_completed_event()
    await service.handle_decision_completed(event)
    await service.approve("corr-1")

    await service.handle_decision_completed(event)
    await service.handle_decision_completed(event)

    result = await repository.get("corr-1")
    assert result is not None
    assert result.status == ApprovalStatus.APPROVED
    assert len(publisher.published) == 1


@pytest.mark.asyncio
async def test_list_pending_returns_escalated_items() -> None:
    service, _, _ = _service()
    await service.handle_decision_completed(_decision_completed_event())

    result = await service.list_pending()

    assert len(result) == 1
    assert result[0].tracking_id == "corr-1"


@pytest.mark.asyncio
async def test_get_returns_pending_approval() -> None:
    service, _, _ = _service()
    await service.handle_decision_completed(_decision_completed_event())

    result = await service.get("corr-1")

    assert result.tracking_id == "corr-1"


@pytest.mark.asyncio
async def test_get_raises_not_found_for_unknown_tracking_id() -> None:
    service, _, _ = _service()

    with pytest.raises(ApprovalNotFoundError):
        await service.get("missing")


@pytest.mark.asyncio
async def test_approve_updates_status_and_publishes_approved_event() -> None:
    service, repository, publisher = _service()
    await service.handle_decision_completed(_decision_completed_event())

    result = await service.approve("corr-1")

    assert result.status == ApprovalStatus.APPROVED
    stored = await repository.get("corr-1")
    assert stored is not None
    assert stored.status == ApprovalStatus.APPROVED
    assert len(publisher.published) == 1
    assert publisher.published[0].resolution == ApprovalResolution.APPROVED


@pytest.mark.asyncio
async def test_reject_updates_status_and_publishes_rejected_event() -> None:
    service, repository, publisher = _service()
    await service.handle_decision_completed(_decision_completed_event())

    result = await service.reject("corr-1")

    assert result.status == ApprovalStatus.REJECTED
    stored = await repository.get("corr-1")
    assert stored is not None
    assert stored.status == ApprovalStatus.REJECTED
    assert len(publisher.published) == 1
    assert publisher.published[0].resolution == ApprovalResolution.REJECTED


@pytest.mark.asyncio
async def test_request_info_updates_status_without_publishing() -> None:
    service, repository, publisher = _service()
    await service.handle_decision_completed(_decision_completed_event())

    result = await service.request_info("corr-1")

    assert result.status == ApprovalStatus.WAITING_INFO
    stored = await repository.get("corr-1")
    assert stored is not None
    assert stored.status == ApprovalStatus.WAITING_INFO
    assert publisher.published == []


@pytest.mark.asyncio
async def test_approve_after_request_info_succeeds() -> None:
    service, _, publisher = _service()
    await service.handle_decision_completed(_decision_completed_event())
    await service.request_info("corr-1")

    result = await service.approve("corr-1")

    assert result.status == ApprovalStatus.APPROVED
    assert len(publisher.published) == 1


@pytest.mark.asyncio
async def test_approve_raises_not_found_for_unknown_tracking_id() -> None:
    service, _, _ = _service()

    with pytest.raises(ApprovalNotFoundError):
        await service.approve("missing")


@pytest.mark.asyncio
async def test_approve_raises_already_resolved_when_already_approved() -> None:
    service, _, publisher = _service()
    await service.handle_decision_completed(_decision_completed_event())
    await service.approve("corr-1")

    with pytest.raises(ApprovalAlreadyResolvedError):
        await service.approve("corr-1")

    assert len(publisher.published) == 1


@pytest.mark.asyncio
async def test_reject_raises_already_resolved_when_already_rejected() -> None:
    service, _, publisher = _service()
    await service.handle_decision_completed(_decision_completed_event())
    await service.reject("corr-1")

    with pytest.raises(ApprovalAlreadyResolvedError):
        await service.reject("corr-1")

    assert len(publisher.published) == 1


@pytest.mark.asyncio
async def test_request_info_raises_already_resolved_when_terminal() -> None:
    service, _, _ = _service()
    await service.handle_decision_completed(_decision_completed_event())
    await service.approve("corr-1")

    with pytest.raises(ApprovalAlreadyResolvedError):
        await service.request_info("corr-1")


# --- add_additional_info (F5 - submitter responds to a request-info) -------


@pytest.mark.asyncio
async def test_add_additional_info_succeeds_when_waiting_info() -> None:
    service, repository, _ = _service()
    await service.handle_decision_completed(_decision_completed_event())
    await service.request_info("corr-1")

    result = await service.add_additional_info("corr-1", "Client name: Acme Corp")

    assert result.status == ApprovalStatus.PENDING  # (new) signal to approver, resume point
    assert result.additional_info == "Client name: Acme Corp"
    stored = await repository.get("corr-1")
    assert stored is not None
    assert stored.status == ApprovalStatus.PENDING
    assert stored.additional_info == "Client name: Acme Corp"


@pytest.mark.asyncio
async def test_add_additional_info_raises_when_not_waiting_info() -> None:
    """Only valid from WAITING_INFO - this endpoint's purpose is responding
    to a request, not general note-taking on a still-fresh PENDING item."""
    service, _, _ = _service()
    await service.handle_decision_completed(_decision_completed_event())

    with pytest.raises(ApprovalNotAwaitingInfoError):
        await service.add_additional_info("corr-1", "unsolicited info")


@pytest.mark.asyncio
async def test_add_additional_info_raises_not_found_for_unknown_tracking_id() -> None:
    service, _, _ = _service()

    with pytest.raises(ApprovalNotFoundError):
        await service.add_additional_info("missing", "info")


@pytest.mark.asyncio
async def test_add_additional_info_raises_when_already_terminal() -> None:
    service, _, _ = _service()
    await service.handle_decision_completed(_decision_completed_event())
    await service.request_info("corr-1")
    await service.add_additional_info("corr-1", "first response")
    await service.approve("corr-1")

    with pytest.raises(ApprovalNotAwaitingInfoError):
        await service.add_additional_info("corr-1", "too late")


@pytest.mark.asyncio
async def test_additional_info_remains_visible_after_later_approve() -> None:
    """additional_info is not cleared by a later approve/reject - the
    approver must still be able to see what the submitter said."""
    service, repository, _ = _service()
    await service.handle_decision_completed(_decision_completed_event())
    await service.request_info("corr-1")
    await service.add_additional_info("corr-1", "Client name: Acme Corp")

    await service.approve("corr-1")

    stored = await repository.get("corr-1")
    assert stored is not None
    assert stored.status == ApprovalStatus.APPROVED
    assert stored.additional_info == "Client name: Acme Corp"
