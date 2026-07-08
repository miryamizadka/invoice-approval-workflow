"""The real, only production PaymentGateway - there is no real payment
processor in this project, so this IS production, not a stub.

Deterministic, not random: fails only for the configured invoice ids, so
INV-1012 is reliably reproducible over docker compose and regression-
testable, never flaky. Reads PAYMENT_SIMULATE_FAILURE_IDS (comma-separated
invoice ids) once at construction - same fail-fast-at-construction posture
as GroqProvider reading GROQ_API_KEY (there is no equivalent hard failure
case here, though - an empty/unset env var is a perfectly valid state,
meaning "no simulated failures")."""

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

    async def charge(self, invoice: Invoice) -> None:
        if invoice.id in self._failure_ids:
            raise PaymentGatewayError(
                f"Simulated gateway decline for invoice {invoice.id} "
                f"(PAYMENT_SIMULATE_FAILURE_IDS)."
            )
