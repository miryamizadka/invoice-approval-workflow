"""Decision router: deterministic autonomy-policy enforcement (M12)."""

from services.decision.router.config import DEFAULT_THRESHOLDS, AutonomyThresholds
from services.decision.router.router import compute_dedup_key, route_decision

__all__ = [
    "AutonomyThresholds",
    "DEFAULT_THRESHOLDS",
    "compute_dedup_key",
    "route_decision",
]
