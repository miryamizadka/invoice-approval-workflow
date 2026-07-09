"""Unit tests for IntakeService's two-phase pub/sub orchestration: process()
publishes invoice.submitted and returns without waiting; complete() is called
separately (by the decision.completed subscription handler, once the event
arrives) to finish the submission. See tests/integration/test_intake_service.py
for full HTTP + subscription-route wiring coverage.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from services.intake.decision_completed_publisher import DecisionCompletedPublisherError
from services.intake.decision_publisher import DecisionPublisherError
from services.intake.models import SubmissionStatus
from services.intake.repository import InMemoryInvoiceRepository
from services.intake.service import IntakeService
from shared.contracts.models import Decision, DecisionCompletedEvent, Invoice, Route
from tests.support.decision_fixtures import clean_invoice


class _StubPublisher:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def publish(self, invoice: Invoice, *, correlation_id: str) -> None:
        self.calls.append({"invoice": invoice, "correlation_id": correlation_id})
        if self._error is not None:
            raise self._error


class _StubDecisionCompletedPublisher:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[DecisionCompletedEvent] = []

    async def publish(self, event: DecisionCompletedEvent) -> None:
        self.calls.append(event)
        if self._error is not None:
            raise self._error


def _decision(route: Route = Route.AUTO_APPROVE, correlation_id: str = "cid") -> Decision:
    return Decision(route=route, reason="test", triggered_rules=[], correlation_id=correlation_id)


# --- process() publishes and returns without a decision -----------------------


async def test_process_publishes_invoice_and_marks_processing() -> None:
    publisher = _StubPublisher()
    repository = InMemoryInvoiceRepository()
    service = IntakeService(repository, publisher, _StubDecisionCompletedPublisher())
    tracking_id = await service.submit(clean_invoice())

    await service.process(tracking_id)

    assert len(publisher.calls) == 1
    assert publisher.calls[0]["correlation_id"] == tracking_id
    submission = await repository.get(tracking_id)
    assert submission is not None
    assert submission.status == SubmissionStatus.PROCESSING


async def test_process_marks_processing_before_publishing() -> None:
    """Save-then-publish ordering matters (race window with an inbound
    decision.completed event) - a publisher that inspects repository state
    from inside publish() confirms PROCESSING is already saved by then."""
    repository = InMemoryInvoiceRepository()
    seen_status_at_publish_time: list[SubmissionStatus] = []

    class _OrderCheckingPublisher:
        async def publish(self, invoice: Invoice, *, correlation_id: str) -> None:
            submission = await repository.get(correlation_id)
            assert submission is not None
            seen_status_at_publish_time.append(submission.status)

    service = IntakeService(
        repository, _OrderCheckingPublisher(), _StubDecisionCompletedPublisher()
    )
    tracking_id = await service.submit(clean_invoice())

    await service.process(tracking_id)

    assert seen_status_at_publish_time == [SubmissionStatus.PROCESSING]


async def test_process_does_not_publish_invoice_submitted_for_known_duplicate() -> None:
    publisher = _StubPublisher()
    repository = InMemoryInvoiceRepository()
    service = IntakeService(repository, publisher, _StubDecisionCompletedPublisher())
    invoice = clean_invoice()
    first_id = await service.submit(invoice)
    await service.process(first_id)
    second_id = await service.submit(invoice)  # same vendor/invoice_number/total -> duplicate

    await service.process(second_id)

    assert len(publisher.calls) == 1  # only the first (non-duplicate) publish
    submission = await repository.get(second_id)
    assert submission is not None
    assert submission.status == SubmissionStatus.COMPLETED
    assert submission.decision is not None
    assert submission.decision.route == Route.DUPLICATE


async def test_process_publishes_decision_completed_for_known_duplicate() -> None:
    """Closes the real gap found during Notification's live verification:
    a known duplicate never reached Decision, so decision.completed never
    fired and Notification could never react to it."""
    repository = InMemoryInvoiceRepository()
    decision_completed_publisher = _StubDecisionCompletedPublisher()
    invoice = clean_invoice()
    service = IntakeService(repository, _StubPublisher(), decision_completed_publisher)
    first_id = await service.submit(invoice)
    await service.process(first_id)
    second_id = await service.submit(invoice)  # duplicate

    await service.process(second_id)

    assert len(decision_completed_publisher.calls) == 1
    event = decision_completed_publisher.calls[0]
    assert event.decision.route == Route.DUPLICATE
    assert event.decision.correlation_id == second_id
    assert event.recommendation is None
    assert event.invoice == invoice


async def test_process_does_not_double_publish_decision_completed_for_duplicate() -> None:
    repository = InMemoryInvoiceRepository()
    decision_completed_publisher = _StubDecisionCompletedPublisher()
    invoice = clean_invoice()
    service = IntakeService(repository, _StubPublisher(), decision_completed_publisher)
    first_id = await service.submit(invoice)
    await service.process(first_id)
    second_id = await service.submit(invoice)  # duplicate
    await service.process(second_id)

    await service.process(second_id)  # hypothetical re-invocation of process() itself

    assert len(decision_completed_publisher.calls) == 1


async def test_process_logs_and_does_not_revert_status_when_decision_completed_publish_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = InMemoryInvoiceRepository()
    failing_publisher = _StubDecisionCompletedPublisher(
        error=DecisionCompletedPublisherError("sidecar unreachable")
    )
    invoice = clean_invoice()
    service = IntakeService(repository, _StubPublisher(), failing_publisher)
    first_id = await service.submit(invoice)
    await service.process(first_id)
    second_id = await service.submit(invoice)  # duplicate

    with caplog.at_level(logging.ERROR):
        await service.process(second_id)

    submission = await repository.get(second_id)
    assert submission is not None
    assert submission.status == SubmissionStatus.COMPLETED
    assert submission.decision is not None
    assert submission.decision.route == Route.DUPLICATE
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].correlation_id == second_id  # type: ignore[attr-defined]
    assert error_records[0].route == Route.DUPLICATE.value  # type: ignore[attr-defined]


async def test_process_publish_failure_marks_failed() -> None:
    publisher = _StubPublisher(error=DecisionPublisherError("sidecar unreachable"))
    repository = InMemoryInvoiceRepository()
    service = IntakeService(repository, publisher, _StubDecisionCompletedPublisher())
    tracking_id = await service.submit(clean_invoice())

    await service.process(tracking_id)

    submission = await repository.get(tracking_id)
    assert submission is not None
    assert submission.status == SubmissionStatus.FAILED


# --- complete() finishes a submission when the decision event arrives ---------


async def test_complete_marks_completed_with_decision() -> None:
    repository = InMemoryInvoiceRepository()
    service = IntakeService(repository, _StubPublisher(), _StubDecisionCompletedPublisher())
    tracking_id = await service.submit(clean_invoice())
    await service.process(tracking_id)
    decision = _decision(route=Route.HUMAN_REVIEW, correlation_id=tracking_id)

    await service.complete(tracking_id, decision)

    submission = await repository.get(tracking_id)
    assert submission is not None
    assert submission.status == SubmissionStatus.COMPLETED
    assert submission.decision == decision


async def test_complete_with_unknown_tracking_id_does_not_raise() -> None:
    repository = InMemoryInvoiceRepository()
    service = IntakeService(repository, _StubPublisher(), _StubDecisionCompletedPublisher())

    await service.complete("does-not-exist", _decision(correlation_id="does-not-exist"))


async def test_complete_called_twice_is_idempotent() -> None:
    repository = InMemoryInvoiceRepository()
    service = IntakeService(repository, _StubPublisher(), _StubDecisionCompletedPublisher())
    tracking_id = await service.submit(clean_invoice())
    await service.process(tracking_id)
    decision = _decision(route=Route.AUTO_APPROVE, correlation_id=tracking_id)

    await service.complete(tracking_id, decision)
    await service.complete(tracking_id, decision)  # redelivery

    submission = await repository.get(tracking_id)
    assert submission is not None
    assert submission.status == SubmissionStatus.COMPLETED
    assert submission.decision == decision


async def test_complete_called_with_different_decision_after_completed_keeps_first(
    ) -> None:
    """First-write-wins: a redelivered event with a (should-never-happen)
    different decision for the same correlation_id doesn't overwrite."""
    repository = InMemoryInvoiceRepository()
    service = IntakeService(repository, _StubPublisher(), _StubDecisionCompletedPublisher())
    tracking_id = await service.submit(clean_invoice())
    await service.process(tracking_id)
    first_decision = _decision(route=Route.AUTO_APPROVE, correlation_id=tracking_id)
    other_decision = _decision(route=Route.HUMAN_REVIEW, correlation_id=tracking_id)

    await service.complete(tracking_id, first_decision)
    await service.complete(tracking_id, other_decision)

    submission = await repository.get(tracking_id)
    assert submission is not None
    assert submission.decision == first_decision


async def test_complete_is_idempotent_when_intakes_own_published_event_loops_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Intake also subscribes to decision.completed (services/intake/app.py),
    so publishing a duplicate's own decision.completed causes Dapr to deliver
    it right back to Intake's own subscription handler. complete()'s existing
    idempotency (status already COMPLETED + value-equal decision) must treat
    this as a silent no-op, not log the "different decision" warning."""
    repository = InMemoryInvoiceRepository()
    decision_completed_publisher = _StubDecisionCompletedPublisher()
    invoice = clean_invoice()
    service = IntakeService(repository, _StubPublisher(), decision_completed_publisher)
    first_id = await service.submit(invoice)
    await service.process(first_id)
    second_id = await service.submit(invoice)  # duplicate
    await service.process(second_id)
    published_decision = decision_completed_publisher.calls[0].decision

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await service.complete(second_id, published_decision)  # simulated self-loopback

    submission = await repository.get(second_id)
    assert submission is not None
    assert submission.status == SubmissionStatus.COMPLETED
    assert submission.decision == published_decision
    assert caplog.records == []
