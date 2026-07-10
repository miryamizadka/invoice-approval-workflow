"""The real, only production PaymentGateway - there is no real payment
processor in this project, so this IS production, not a stub.

Deterministic, not random: fails only for the configured invoice ids, so
INV-1012 is reliably reproducible over docker compose and regression-
testable, never flaky. Reads PAYMENT_SIMULATE_FAILURE_IDS (comma-separated
invoice ids) once at construction - same fail-fast-at-construction posture
as GroqProvider reading GROQ_API_KEY (there is no equivalent hard failure
case here, though - an empty/unset env var is a perfectly valid state,
meaning "no simulated failures").

Idempotency (M10): _results caches the outcome (None for success, the raised
PaymentGatewayError for failure) per idempotency_key - a repeated charge()
call with a previously-seen key replays that outcome instead of
re-evaluating _failure_ids. execution_count only increments on a genuinely
new key, proving the underlying decision only ever runs once per key."""

from __future__ import annotations

import os

from services.payment.accessors.payment_gateway import PaymentGatewayError
from shared.contracts.models import Invoice


class SimulatedPaymentGateway:
    def __init__(self, *, failure_ids: str | None = None) -> None:
        raw = failure_ids if failure_ids is not None else os.environ.get(
            "PAYMENT_SIMULATE_FAILURE_IDS", ""
        )
        self._failure_ids = {v.strip() for v in raw.split(",") if v.strip()}
        self._results: dict[str, PaymentGatewayError | None] = {}
        self.execution_count = 0

    async def charge(self, invoice: Invoice, *, idempotency_key: str) -> None:
        if idempotency_key in self._results:
            cached_error = self._results[idempotency_key]
            if cached_error is not None:
                raise cached_error
            return
        self.execution_count += 1
        if invoice.id in self._failure_ids:
            error = PaymentGatewayError(
                f"Simulated gateway decline for invoice {invoice.id} "
                f"(PAYMENT_SIMULATE_FAILURE_IDS)."
            )
            self._results[idempotency_key] = error
            raise error
        self._results[idempotency_key] = None
