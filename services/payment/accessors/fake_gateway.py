"""Test double for PaymentGateway - never wired through production
composition, only ever constructed directly by tests."""

from __future__ import annotations

from services.payment.accessors.payment_gateway import PaymentGatewayError
from shared.contracts.models import Invoice


class FakePaymentGateway:
    def __init__(
        self, *, fail_for: set[str] | None = None, error_message: str = "simulated gateway failure"
    ) -> None:
        self._fail_for = fail_for or set()
        self._error_message = error_message
        self.charged: list[str] = []

    async def charge(self, invoice: Invoice) -> None:
        if invoice.id in self._fail_for:
            raise PaymentGatewayError(self._error_message)
        self.charged.append(invoice.id)
