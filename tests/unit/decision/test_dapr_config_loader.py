"""Tests for services/decision/service/dapr_config_loader.py (F7/M13).

merge_thresholds() is pure (dict in, AutonomyThresholds out) - tested here
with no Dapr involved at all. _fetch_raw_config()/load_policy_and_thresholds()
are tested with a fake async Dapr configuration client.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import grpc
import pytest

from services.decision.router.config import DEFAULT_THRESHOLDS
from services.decision.service.dapr_config_loader import (
    _fetch_raw_config,
    load_policy_and_thresholds,
    merge_thresholds,
)

# --- merge_thresholds() - pure, no Dapr -------------------------------------


def test_merge_thresholds_with_no_overrides_returns_fallback_unchanged() -> None:
    result = merge_thresholds({}, DEFAULT_THRESHOLDS)

    assert result == DEFAULT_THRESHOLDS


def test_merge_thresholds_full_override() -> None:
    raw = {
        "ceiling": "500",
        "min_confidence": "0.9",
        "receipt_required_above": "50",
        "meal_per_attendee_cap": "100",
        "saas_monthly_cap": "300",
        "hardware_cap": "2000",
        "travel_manager_approval_threshold": "3000",
        "severe_violation_rules": "MEAL-03,HW-02",
    }

    result = merge_thresholds(raw, DEFAULT_THRESHOLDS)

    assert result.ceiling == Decimal("500")
    assert result.min_confidence == 0.9
    assert result.receipt_required_above == Decimal("50")
    assert result.meal_per_attendee_cap == Decimal("100")
    assert result.saas_monthly_cap == Decimal("300")
    assert result.hardware_cap == Decimal("2000")
    assert result.travel_manager_approval_threshold == Decimal("3000")
    assert result.severe_violation_rules == frozenset({"MEAL-03", "HW-02"})


def test_merge_thresholds_partial_override_leaves_rest_at_fallback() -> None:
    result = merge_thresholds({"ceiling": "500"}, DEFAULT_THRESHOLDS)

    assert result.ceiling == Decimal("500")
    assert result.min_confidence == DEFAULT_THRESHOLDS.min_confidence
    assert result.hardware_cap == DEFAULT_THRESHOLDS.hardware_cap


def test_merge_thresholds_invalid_value_falls_back_for_that_field_only() -> None:
    result = merge_thresholds(
        {"ceiling": "not-a-number", "min_confidence": "0.9"}, DEFAULT_THRESHOLDS
    )

    assert result.ceiling == DEFAULT_THRESHOLDS.ceiling  # invalid -> fallback
    assert result.min_confidence == 0.9  # valid override still applied


def test_merge_thresholds_does_not_mutate_default_thresholds() -> None:
    """Regression guard - AutonomyThresholds is already frozen=True (structurally
    prevents in-place mutation), but this catches a future refactor that
    swaps a copy for a shared reference."""
    before = DEFAULT_THRESHOLDS.model_copy()

    merge_thresholds({"ceiling": "999999"}, DEFAULT_THRESHOLDS)

    assert DEFAULT_THRESHOLDS == before


def test_merge_thresholds_logs_overridden_fields_with_old_and_new_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="services.decision.service.dapr_config_loader"):
        merge_thresholds({"ceiling": "500"}, DEFAULT_THRESHOLDS)

    record = next(r for r in caplog.records if "ceiling" in r.getMessage())
    assert "250" in record.getMessage()
    assert "500" in record.getMessage()


def test_merge_thresholds_logs_warning_for_invalid_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="services.decision.service.dapr_config_loader"):
        merge_thresholds({"ceiling": "not-a-number"}, DEFAULT_THRESHOLDS)

    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "ceiling" in record.getMessage()


# --- load_policy_and_thresholds() - with a fake Dapr configuration client ---


class _FakeConfigItem:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeConfigResponse:
    def __init__(self, items: dict[str, str]) -> None:
        self.items = {key: _FakeConfigItem(value) for key, value in items.items()}


class _FakeConfigClient:
    def __init__(
        self, *, items: dict[str, str] | None = None, error: Exception | None = None
    ) -> None:
        self._items = items or {}
        self._error = error

    async def get_configuration(
        self, store_name: str, keys: list[str], config_metadata: dict | None = None
    ) -> _FakeConfigResponse:
        if self._error is not None:
            raise self._error
        return _FakeConfigResponse({k: v for k, v in self._items.items() if k in keys})


async def test_load_policy_and_thresholds_falls_back_on_grpc_error() -> None:
    client = _FakeConfigClient(error=grpc.RpcError())

    policy, thresholds = await load_policy_and_thresholds(
        "fallback policy text", DEFAULT_THRESHOLDS, client=client
    )

    assert policy == "fallback policy text"
    assert thresholds == DEFAULT_THRESHOLDS


async def test_load_policy_and_thresholds_falls_back_on_empty_response() -> None:
    """Distinct code path from a grpc error: the call succeeds, but the
    config store has no keys set yet (natural first-run state)."""
    client = _FakeConfigClient(items={})

    policy, thresholds = await load_policy_and_thresholds(
        "fallback policy text", DEFAULT_THRESHOLDS, client=client
    )

    assert policy == "fallback policy text"
    assert thresholds == DEFAULT_THRESHOLDS


async def test_load_policy_and_thresholds_applies_overrides_when_present() -> None:
    client = _FakeConfigClient(items={"ceiling": "500", "policy_text": "new policy"})

    policy, thresholds = await load_policy_and_thresholds(
        "fallback policy text", DEFAULT_THRESHOLDS, client=client
    )

    assert policy == "new policy"
    assert thresholds.ceiling == Decimal("500")
    assert thresholds.min_confidence == DEFAULT_THRESHOLDS.min_confidence


class _HangingConfigClient:
    async def get_configuration(
        self, store_name: str, keys: list[str], config_metadata: dict | None = None
    ) -> _FakeConfigResponse:
        await asyncio.sleep(100)  # never resolves within the loader's timeout
        raise AssertionError("unreachable")


async def test_load_policy_and_thresholds_falls_back_on_timeout() -> None:
    client = _HangingConfigClient()

    policy, thresholds = await load_policy_and_thresholds(
        "fallback policy text", DEFAULT_THRESHOLDS, client=client, timeout=0.05
    )

    assert policy == "fallback policy text"
    assert thresholds == DEFAULT_THRESHOLDS


class _ClientConstructionFailsLazily:
    """Simulates LazyDaprClient[_ConfigClient]().get() itself raising -
    exactly the real bug found live: DaprClient()'s constructor does its own
    blocking wait-for-sidecar and raises TimeoutError if the sidecar isn't
    ready yet. This must be caught too, not just failures from an
    already-constructed client's get_configuration() call."""

    def __class_getitem__(cls, item: object) -> type[_ClientConstructionFailsLazily]:
        return cls

    def get(self) -> None:
        raise TimeoutError("Dapr health check timed out, after 60.0.")


async def test_fetch_raw_config_falls_back_when_client_construction_itself_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.decision.service.dapr_config_loader.LazyDaprClient",
        _ClientConstructionFailsLazily,
    )

    result = await _fetch_raw_config(None)

    assert result == {}
