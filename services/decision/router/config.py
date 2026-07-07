"""Externally-configurable autonomy thresholds (M13).

Holds one default instance matching policy.md sections 1-6 today. In
production these values would be loaded from Dapr configuration at runtime
instead of imported as a Python constant - the router only depends on the
AutonomyThresholds shape, so swapping the source later requires no change
to router.py.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class AutonomyThresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ceiling: Decimal
    min_confidence: float
    receipt_required_above: Decimal
    meal_per_attendee_cap: Decimal
    saas_monthly_cap: Decimal
    hardware_cap: Decimal
    travel_manager_approval_threshold: Decimal
    severe_violation_rules: frozenset[str]


DEFAULT_THRESHOLDS = AutonomyThresholds(
    ceiling=Decimal("250"),
    min_confidence=0.80,
    receipt_required_above=Decimal("25"),
    meal_per_attendee_cap=Decimal("75"),
    saas_monthly_cap=Decimal("200"),
    hardware_cap=Decimal("1000"),
    travel_manager_approval_threshold=Decimal("1500"),
    severe_violation_rules=frozenset({"MEAL-03"}),
)
