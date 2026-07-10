"""Externally-configurable policy + thresholds via Dapr's Configuration API
(F7/M13) - the one thing ARCHITECTURE.md §9 already commits Decision to
("The policy and autonomy thresholds live in a Dapr configuration store,
never hard-coded").

Fetch-once at startup, not live hot-reload (subscribe_configuration): a
changed value takes effect on the next `docker compose restart decision`
(no image rebuild, no code change) - not before this call is a Dapr build
here or elsewhere; every other config mechanism in this project (LLM_PROVIDER,
policy.md, budgets.json) is also read once at startup. Live hot-reload would
mean turning Decider's thresholds into mutable shared state read concurrently
by every in-flight request - real complexity for a config that changes rarely.

Dapr's Configuration API is read/subscribe only from the app's side (no
`save_configuration` in the client SDK) - operators change a value by writing
directly to the backing store (Redis here: `redis-cli SET ceiling 500`), not
through Dapr.

Split into two layers on purpose (I/O vs pure logic):
- _fetch_raw_config(): the only thing that touches Dapr. Never raises -
  collapses any failure (grpc error, timeout) to an empty dict, treated
  identically to "nothing configured yet" by merge_thresholds().
- merge_thresholds(): pure (dict in, AutonomyThresholds out) - fully unit
  tested without any Dapr client at all. Per-field: a missing key falls
  back silently; a present-but-unparseable value falls back for that field
  only (never invalidates the rest) and logs a warning - the config store
  is external input now, not trusted Python source.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import grpc

from services.decision.router.config import AutonomyThresholds
from shared.dapr_client import LazyDaprClient

_STORE_NAME = "configstore"
_DEFAULT_TIMEOUT_SECONDS = 5.0

_POLICY_TEXT_KEY = "policy_text"
_CONFIG_KEYS = {
    "ceiling": "ceiling",
    "min_confidence": "min_confidence",
    "receipt_required_above": "receipt_required_above",
    "meal_per_attendee_cap": "meal_per_attendee_cap",
    "saas_monthly_cap": "saas_monthly_cap",
    "hardware_cap": "hardware_cap",
    "travel_manager_approval_threshold": "travel_manager_approval_threshold",
    "severe_violation_rules": "severe_violation_rules",
}
_ALL_KEYS = [_POLICY_TEXT_KEY, *_CONFIG_KEYS.values()]

_logger = logging.getLogger(__name__)


class _ConfigResponse(Protocol):
    @property
    def items(self) -> dict[str, Any]: ...


class _ConfigClient(Protocol):
    async def get_configuration(
        self, store_name: str, keys: list[str], config_metadata: dict[str, str] | None = None
    ) -> _ConfigResponse: ...


async def _fetch_raw_config(
    client: _ConfigClient | None, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> dict[str, str]:
    try:
        # Client resolution happens *inside* the try, not before it - a real
        # bug found live: LazyDaprClient's DaprClient() constructor does its
        # own blocking wait-for-sidecar (up to 60s) and eventually raises
        # TimeoutError if the sidecar isn't ready. Outside this try, that
        # exception propagated uncaught out of the FastAPI lifespan hook and
        # crashed the whole service's startup - exactly the failure mode
        # this function exists to prevent.
        resolved_client = client or LazyDaprClient[_ConfigClient]().get()
        response = await asyncio.wait_for(
            resolved_client.get_configuration(_STORE_NAME, _ALL_KEYS), timeout=timeout
        )
    except (grpc.RpcError, TimeoutError) as exc:
        _logger.warning(f"dapr_config_unreachable_using_defaults error={exc}")
        return {}
    return {key: item.value for key, item in response.items.items()}


def _parse_decimal(field: str, raw: dict[str, str], fallback: Decimal) -> Decimal:
    if field not in raw:
        return fallback
    try:
        value = Decimal(raw[field])
    except InvalidOperation:
        _logger.warning(
            f"dapr_config_invalid_value_using_default field={field} raw_value={raw[field]!r}"
        )
        return fallback
    if value != fallback:
        _logger.info(f"dapr_config_override field={field} old={fallback} new={value}")
    return value


def merge_thresholds(raw: dict[str, str], fallback: AutonomyThresholds) -> AutonomyThresholds:
    ceiling = _parse_decimal("ceiling", raw, fallback.ceiling)
    receipt_required_above = _parse_decimal(
        "receipt_required_above", raw, fallback.receipt_required_above
    )
    meal_per_attendee_cap = _parse_decimal(
        "meal_per_attendee_cap", raw, fallback.meal_per_attendee_cap
    )
    saas_monthly_cap = _parse_decimal("saas_monthly_cap", raw, fallback.saas_monthly_cap)
    hardware_cap = _parse_decimal("hardware_cap", raw, fallback.hardware_cap)
    travel_manager_approval_threshold = _parse_decimal(
        "travel_manager_approval_threshold", raw, fallback.travel_manager_approval_threshold
    )

    min_confidence = fallback.min_confidence
    if "min_confidence" in raw:
        try:
            min_confidence = float(raw["min_confidence"])
        except ValueError:
            _logger.warning(
                f"dapr_config_invalid_value_using_default field=min_confidence "
                f"raw_value={raw['min_confidence']!r}"
            )
            min_confidence = fallback.min_confidence
        else:
            if min_confidence != fallback.min_confidence:
                _logger.info(
                    f"dapr_config_override field=min_confidence "
                    f"old={fallback.min_confidence} new={min_confidence}"
                )

    severe_violation_rules = fallback.severe_violation_rules
    if "severe_violation_rules" in raw:
        parsed = frozenset(v.strip() for v in raw["severe_violation_rules"].split(",") if v.strip())
        if parsed != fallback.severe_violation_rules:
            _logger.info(
                f"dapr_config_override field=severe_violation_rules "
                f"old={fallback.severe_violation_rules} new={parsed}"
            )
        severe_violation_rules = parsed

    return AutonomyThresholds(
        ceiling=ceiling,
        min_confidence=min_confidence,
        receipt_required_above=receipt_required_above,
        meal_per_attendee_cap=meal_per_attendee_cap,
        saas_monthly_cap=saas_monthly_cap,
        hardware_cap=hardware_cap,
        travel_manager_approval_threshold=travel_manager_approval_threshold,
        severe_violation_rules=severe_violation_rules,
    )


async def load_policy_and_thresholds(
    policy_fallback: str,
    thresholds_fallback: AutonomyThresholds,
    *,
    client: _ConfigClient | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, AutonomyThresholds]:
    """Never raises - the service must start and function correctly even if
    the config store was never seeded or is completely unreachable."""
    raw = await _fetch_raw_config(client, timeout=timeout)
    policy = raw.get(_POLICY_TEXT_KEY, policy_fallback)
    if _POLICY_TEXT_KEY in raw and policy != policy_fallback:
        _logger.info(f"dapr_config_override field={_POLICY_TEXT_KEY}")
    thresholds = merge_thresholds(raw, thresholds_fallback)
    return policy, thresholds
