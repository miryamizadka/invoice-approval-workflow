"""Tests for shared cross-service contracts that don't belong to one service.

build_duplicate_decision: the single canonical "what does a DUPLICATE
decision look like" source, used by both the router's gate 1 and Intake's
pre-publish short-circuit (see IntakeService.process()) - must never drift.

InvoiceSubmittedEvent: the pub/sub envelope Intake publishes and Decision
subscribes to - carries transport concerns (correlation_id) that Invoice
itself deliberately doesn't.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.contracts.models import Decision, InvoiceSubmittedEvent, Route, build_duplicate_decision
from tests.support.decision_fixtures import clean_invoice


def test_build_duplicate_decision_returns_duplicate_route() -> None:
    decision = build_duplicate_decision("corr-123")

    assert decision.route == Route.DUPLICATE
    assert decision.triggered_rules == ["GLOBAL-DUP"]
    assert decision.correlation_id == "corr-123"
    assert "duplicate" in decision.reason.lower()


def test_build_duplicate_decision_is_a_decision() -> None:
    assert isinstance(build_duplicate_decision("corr-123"), Decision)


def test_invoice_submitted_event_carries_invoice_and_correlation_id() -> None:
    invoice = clean_invoice()

    event = InvoiceSubmittedEvent(invoice=invoice, correlation_id="corr-456")

    assert event.invoice == invoice
    assert event.correlation_id == "corr-456"


def test_invoice_submitted_event_is_frozen() -> None:
    event = InvoiceSubmittedEvent(invoice=clean_invoice(), correlation_id="corr-456")

    with pytest.raises(ValidationError):
        event.correlation_id = "other"  # type: ignore[misc]


def test_invoice_submitted_event_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        InvoiceSubmittedEvent(
            invoice=clean_invoice(), correlation_id="corr-456", is_duplicate=False
        )
