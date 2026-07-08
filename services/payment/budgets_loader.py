"""Loads department budget seeds Payment's startup uses to populate
BudgetRepository. A local file today; the loading mechanism is the seam
M13's external config would replace later - callers only depend on getting
a dict back, not on how it's sourced. Mirrors
services/decision/service/policy_loader.py's exact shape."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

DEFAULT_BUDGETS_PATH = Path(__file__).resolve().parents[2] / "policy" / "budgets.json"


def load_budgets(path: Path = DEFAULT_BUDGETS_PATH) -> dict[str, Decimal]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {department: Decimal(str(total)) for department, total in data["budgets"].items()}
