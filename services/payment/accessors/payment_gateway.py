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
    async def charge(self, invoice: Invoice) -> None:
        """Raises PaymentGatewayError on failure. No return value -
        success is 'did not raise'."""
        ...
