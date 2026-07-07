"""Shared, non-fixture test helpers for Decision-service tests.

Plain functions, not pytest fixtures: pytest.mark.parametrize needs concrete
values at collection time, before any fixture would be resolved.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from shared.contracts.models import (
    Category,
    Invoice,
    LineItem,
    Recommendation,
    RecommendationType,
)

FIXTURES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "sample-invoices.json"


def _load_raw_fixtures() -> list[dict[str, Any]]:
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    return list(data["fixtures"])


RAW_FIXTURES: list[dict[str, Any]] = _load_raw_fixtures()


def invoice_from_fixture(raw: dict[str, Any], **overrides: Any) -> Invoice:
    """Build an Invoice from a raw fixture dict, stripping test-only keys."""
    clean = {k: v for k, v in raw.items() if k not in ("scenario", "expected")}
    clean.update(overrides)
    return Invoice.model_validate(clean)


def build_recommendation(raw: dict[str, Any]) -> Recommendation | None:
    """A plausible, non-adversarial agent recommendation for a fixture.

    Confidence is fixed at 0.9 (above threshold) so any escalation seen in
    the fixture-driven suite is proven to come from the deterministic gates,
    never from an artificially low confidence score.
    """
    if raw["id"] == "INV-1007":
        return None  # duplicate short-circuits before any agent call
    if raw["expected"]["route"] == "reject":
        return Recommendation(
            recommendation=RecommendationType.REJECT,
            confidence=0.9,
            cited_rules=raw["expected"]["violations"],
            reasoning=raw["expected"]["reason"],
        )
    return Recommendation(
        recommendation=RecommendationType.APPROVE,
        confidence=0.9,
        cited_rules=[],
        reasoning="stub: optimistic non-adversarial recommendation",
    )


def clean_invoice(**overrides: Any) -> Invoice:
    """A minimal, fully policy-clean Invoice for isolating a single gate.

    Base case: known-vendor USD meal, $50 total, 1 attendee, receipt
    present, math reconciles exactly - passes every gate on its own.
    Passing `total=` alone regenerates a single matching line item with
    zero tax so callers don't have to keep the math reconciled by hand;
    pass `line_items`/`tax_amount` explicitly to exercise GLOBAL-MATH itself.
    """
    base: dict[str, Any] = {
        "id": "TEST-0000",
        "submitter": "test@example.com",
        "department": "engineering-2026Q2",
        "vendor": "Test Vendor",
        "vendor_known": True,
        "invoice_number": "TEST-0001",
        "currency": "USD",
        "category": Category.MEALS,
        "attendees": 1,
        "line_items": [
            LineItem(description="Test item", quantity=Decimal("1"), unit_price=Decimal("50.00"))
        ],
        "tax_amount": Decimal("0.00"),
        "total": Decimal("50.00"),
        "receipt_present": True,
        "date": "2026-05-12",
        "notes": None,
    }
    if "total" in overrides and "line_items" not in overrides and "tax_amount" not in overrides:
        total = overrides["total"]
        overrides = dict(overrides)
        overrides["line_items"] = [
            LineItem(description="Test item", quantity=Decimal("1"), unit_price=total)
        ]
        overrides["tax_amount"] = Decimal("0.00")
    base.update(overrides)
    return Invoice(**base)
