"""Test double for PaymentGateway - never wired through production
composition, only ever constructed directly by tests.

Idempotency (M10): mirrors SimulatedPaymentGateway's _results cache/
execution_count - a repeated charge() call with a previously-seen
idempotency_key replays the cached outcome instead of re-running the
fail_for check or appending to `charged` again."""

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
        self._results: dict[str, PaymentGatewayError | None] = {}
        self.execution_count = 0

    async def charge(self, invoice: Invoice, *, idempotency_key: str) -> None:
        if idempotency_key in self._results:
            cached_error = self._results[idempotency_key]
            if cached_error is not None:
                raise cached_error
            return
        self.execution_count += 1
        if invoice.id in self._fail_for:
            error = PaymentGatewayError(self._error_message)
            self._results[idempotency_key] = error
            raise error
        self._results[idempotency_key] = None
        self.charged.append(invoice.id)
