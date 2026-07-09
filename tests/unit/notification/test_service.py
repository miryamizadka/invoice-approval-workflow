"""Unit tests for NotificationService - the terminal, pure consumer of the
choreography chain (M8 push notification, M10 idempotency).

Uses InMemoryNotificationRepository and FakeNotificationChannel - no Dapr.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from services.notification.accessors.fake_channel import FakeNotificationChannel
from services.notification.accessors.notification_channel import NotificationChannelError
from services.notification.repository import InMemoryNotificationRepository
from services.notification.service import NotificationService, build_notification_service
from shared.contracts.models import ApprovalResolution, PaymentResolution, Route
from tests.support.event_fixtures import (
    approval_completed_event,
    decision_completed_event,
    payment_completed_event,
)


def _service(
    **overrides: Any,
) -> tuple[NotificationService, InMemoryNotificationRepository, FakeNotificationChannel]:
    repository = overrides.get("repository") or InMemoryNotificationRepository()
    channel = overrides.get("channel") or FakeNotificationChannel()
    service = build_notification_service(repository, channel)
    return service, repository, channel


# --- decision.completed filtering -----------------------------------------


async def test_decision_completed_reject_route_sends_notification() -> None:
    service, _, channel = _service()

    await service.handle_decision_completed(decision_completed_event(Route.REJECT))

    assert len(channel.sent) == 1
    assert channel.sent[0][3] == "decision.completed"


async def test_decision_completed_duplicate_route_sends_notification() -> None:
    service, _, channel = _service()

    await service.handle_decision_completed(decision_completed_event(Route.DUPLICATE))

    assert len(channel.sent) == 1
    assert "duplicate" in channel.sent[0][2].lower()


async def test_decision_completed_auto_approve_route_ignored() -> None:
    service, _, channel = _service()

    await service.handle_decision_completed(decision_completed_event(Route.AUTO_APPROVE))

    assert channel.sent == []


async def test_escalation_to_human_review_does_not_notify_submitter() -> None:
    """Business invariant, named explicitly - human_review is not Notification's
    concern; Approval owns it, and the eventual resolution reaches Notification
    via approval.completed/payment.completed instead."""
    service, _, channel = _service()

    await service.handle_decision_completed(decision_completed_event(Route.HUMAN_REVIEW))

    assert channel.sent == []


# --- approval.completed filtering -----------------------------------------


async def test_approval_completed_rejected_sends_notification() -> None:
    service, _, channel = _service()

    await service.handle_approval_completed(approval_completed_event(ApprovalResolution.REJECTED))

    assert len(channel.sent) == 1
    assert channel.sent[0][3] == "approval.completed"


async def test_approval_approved_does_not_notify_because_payment_will() -> None:
    """Business invariant, named explicitly - APPROVED items go on to Payment,
    which will send the eventual notification once the saga finishes."""
    service, _, channel = _service()

    await service.handle_approval_completed(approval_completed_event(ApprovalResolution.APPROVED))

    assert channel.sent == []


# --- payment.completed - unconditional -------------------------------------


async def test_payment_completed_completed_sends_notification() -> None:
    service, _, channel = _service()

    await service.handle_payment_completed(payment_completed_event(PaymentResolution.COMPLETED))

    assert len(channel.sent) == 1
    assert channel.sent[0][3] == "payment.completed"


async def test_payment_completed_failed_sends_notification_with_reason() -> None:
    service, _, channel = _service()

    await service.handle_payment_completed(payment_completed_event(PaymentResolution.FAILED))

    assert len(channel.sent) == 1
    assert "test reason" in channel.sent[0][2]


# --- idempotency -----------------------------------------------------------


async def test_idempotency_guard_redelivery_of_same_event_does_not_call_send_twice() -> None:
    service, _, channel = _service()
    event = payment_completed_event(PaymentResolution.COMPLETED)

    await service.handle_payment_completed(event)
    await service.handle_payment_completed(event)

    assert len(channel.sent) == 1


async def test_send_failure_propagates_uncaught_and_mark_notified_never_called() -> None:
    channel = FakeNotificationChannel(fail_for={"TEST-0000"})
    service, repository, _ = _service(channel=channel)

    event = payment_completed_event(PaymentResolution.COMPLETED)
    with pytest.raises(NotificationChannelError):
        await service.handle_payment_completed(event)

    assert await repository.already_notified("corr-1") is False


async def test_redelivery_after_failed_send_succeeds_and_marks_notified() -> None:
    """The most critical test for the full idempotency+retry story - proves
    the retry actually recovers, not just that a failure doesn't corrupt state."""
    channel = FakeNotificationChannel(fail_first_n_calls=1)
    service, repository, _ = _service(channel=channel)
    event = payment_completed_event(PaymentResolution.COMPLETED)

    with pytest.raises(NotificationChannelError):
        await service.handle_payment_completed(event)
    assert await repository.already_notified("corr-1") is False

    await service.handle_payment_completed(event)  # redelivery

    assert await repository.already_notified("corr-1") is True
    assert len(channel.sent) == 1


class _MarkNotifiedFailsOnceRepository:
    """Local test double proving the other half of the documented
    already_notified/send/mark_notified race: send() succeeds but
    mark_notified() fails to persist. A plain RuntimeError, not
    NotificationRepositoryError (services/notification/dapr_state_repository.py)
    - this file is deliberately Dapr-free, and _notify() propagates whatever
    mark_notified() raises uncaught regardless of type."""

    def __init__(self) -> None:
        self._notified: set[str] = set()
        self._fail_next = True

    async def already_notified(self, tracking_id: str) -> bool:
        return tracking_id in self._notified

    async def mark_notified(self, tracking_id: str) -> None:
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("simulated mark_notified failure")
        self._notified.add(tracking_id)


async def test_mark_notified_failure_causes_resend_on_redelivery() -> None:
    """Covers the other branch of the race documented in service.py's
    docstring: send() already succeeded but the mark never persisted - a
    redelivery must resend (a safe, harmless duplicate), not silently skip
    because already_notified is still False."""
    repository = _MarkNotifiedFailsOnceRepository()
    channel = FakeNotificationChannel()
    service = build_notification_service(repository, channel)
    event = payment_completed_event(PaymentResolution.COMPLETED)

    with pytest.raises(RuntimeError):
        await service.handle_payment_completed(event)
    assert len(channel.sent) == 1  # send() already happened before the mark failed
    assert await repository.already_notified("corr-1") is False

    await service.handle_payment_completed(event)  # redelivery

    assert await repository.already_notified("corr-1") is True
    assert len(channel.sent) == 2  # harmless duplicate - documented, expected


# --- logging: event_source must be embedded in the message text ------------


async def test_notification_sent_log_includes_event_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression-guard for the JsonFormatter Finding (extra= silently drops
    every key except correlation_id) - source must be embedded in the
    message text itself to be visible in production JSON logs."""
    service, _, _ = _service()
    event = payment_completed_event(PaymentResolution.COMPLETED)

    with caplog.at_level(logging.INFO, logger="services.notification.service"):
        await service.handle_payment_completed(event)

    sent_record = next(r for r in caplog.records if r.getMessage().startswith("notification_sent"))
    assert "source=payment.completed" in sent_record.getMessage()


async def test_notification_already_sent_skipping_log_includes_event_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _, _ = _service()
    event = payment_completed_event(PaymentResolution.COMPLETED)
    await service.handle_payment_completed(event)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="services.notification.service"):
        await service.handle_payment_completed(event)  # redelivery, already notified

    skip_record = next(
        r for r in caplog.records if r.getMessage().startswith("notification_already_sent_skipping")
    )
    assert "source=payment.completed" in skip_record.getMessage()


# --- read API ---------------------------------------------------------------


async def test_already_notified_helper_reflects_repository_state() -> None:
    service, _, _ = _service()

    assert await service.already_notified("corr-1") is False

    await service.handle_payment_completed(payment_completed_event(PaymentResolution.COMPLETED))

    assert await service.already_notified("corr-1") is True
