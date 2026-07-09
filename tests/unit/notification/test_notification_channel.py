"""Tests for NotificationChannel implementations - FakeNotificationChannel
(test double) and LoggingNotificationChannel (the real, only production
implementation - there is no real email/SMS/webhook backend in this
project)."""

from __future__ import annotations

import logging

import pytest

from services.notification.accessors.fake_channel import FakeNotificationChannel
from services.notification.accessors.logging_channel import LoggingNotificationChannel
from services.notification.accessors.notification_channel import (
    NotificationChannel,
    NotificationChannelError,
)
from tests.support.decision_fixtures import clean_invoice

# --- LoggingNotificationChannel -------------------------------------------


async def test_logging_channel_send_logs_expected_fields(caplog: pytest.LogCaptureFixture) -> None:
    """Regression test for the Finding: JsonFormatter only ever promotes
    record.correlation_id out of extra= - everything else must be embedded
    in the primary log message string itself, not solely passed via extra."""
    channel = LoggingNotificationChannel()
    invoice = clean_invoice(id="INV-9999")

    with caplog.at_level(logging.INFO, logger="notification.channel"):
        await channel.send(invoice, "tid-1", "Payment completed: ok", source="payment.completed")

    assert len(caplog.records) == 1
    formatted_message = caplog.records[0].getMessage()
    assert "tid-1" in formatted_message
    assert "INV-9999" in formatted_message
    assert invoice.submitter in formatted_message
    assert "payment.completed" in formatted_message
    assert "Payment completed: ok" in formatted_message


async def test_logging_channel_never_raises() -> None:
    channel = LoggingNotificationChannel()
    invoice = clean_invoice()

    await channel.send(  # does not raise
        invoice, "tid-1", "some message", source="decision.completed"
    )


# --- FakeNotificationChannel -----------------------------------------------


async def test_fake_channel_records_sent_calls() -> None:
    channel = FakeNotificationChannel()
    invoice = clean_invoice(id="INV-1")

    await channel.send(invoice, "tid-1", "hello", source="payment.completed")

    assert channel.sent == [("INV-1", "tid-1", "hello", "payment.completed")]


async def test_fake_channel_raises_error_for_configured_invoice_id() -> None:
    channel = FakeNotificationChannel(fail_for={"INV-1012"})
    invoice = clean_invoice(id="INV-1012")

    with pytest.raises(NotificationChannelError):
        await channel.send(invoice, "tid-1", "hello", source="payment.completed")


async def test_fake_channel_fail_first_n_calls_then_succeeds() -> None:
    channel = FakeNotificationChannel(fail_first_n_calls=1)
    invoice = clean_invoice(id="INV-1")

    with pytest.raises(NotificationChannelError):
        await channel.send(invoice, "tid-1", "hello", source="payment.completed")
    await channel.send(  # second call succeeds
        invoice, "tid-1", "hello", source="payment.completed"
    )

    assert channel.sent == [("INV-1", "tid-1", "hello", "payment.completed")]


# --- Protocol conformance -------------------------------------------------


@pytest.mark.parametrize("channel", [FakeNotificationChannel(), LoggingNotificationChannel()])
def test_every_channel_satisfies_protocol(channel: NotificationChannel) -> None:
    assert isinstance(channel, NotificationChannel)
