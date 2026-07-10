"""PaymentGateway - the accessor Payment's saga calls to actually execute a
charge. A Protocol, same pattern as LLMProvider: isolates PaymentService
from any particular backend.

There is no real payment processor in this project - PaymentGatewayError
and the simulated behavior below are intentional, not a stand-in for
something real that's temporarily skipped."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.contracts.models import Invoice


class PaymentGatewayError(Exception):
    """Raised when the (simulated) gateway declines a charge - never caught
    silently; the saga's compensation step is exactly what catches this."""


@runtime_checkable
class PaymentGateway(Protocol):
    async def charge(self, invoice: Invoice, *, idempotency_key: str) -> None:
        """Raises PaymentGatewayError on failure. No return value -
        success is 'did not raise'.

        idempotency_key must be stable across retries of the same logical
        charge (PaymentService passes tracking_id, models.py) - calling
        charge() twice with the same key must replay the original outcome
        without re-executing the underlying charge logic (M10). This is what
        makes PaymentRecord's documented crash-recovery resume (a redelivery
        that resumes at the charge step, not just the reserve step) safe
        against a real, stateful payment gateway - not just this project's
        pure simulated ones, which would be idempotent by accident."""
        ...
