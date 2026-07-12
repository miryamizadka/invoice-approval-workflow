"""Tests for services/decision/service/dapr_secret_loader.py (M5).

load_groq_api_key() is I/O (fake async Dapr secrets client) - same style as
test_dapr_config_loader.py's _fetch_raw_config()/load_policy_and_thresholds()
tests. resolve_provider_from_secret() is pure (dict/None in, LLMProvider out)
- tested with no Dapr involved at all, mirroring merge_thresholds()'s tests.
"""

from __future__ import annotations

import asyncio
import logging

import grpc
import pytest

from services.decision.accessors.groq_provider import GroqProvider
from services.decision.accessors.llm_provider import LLMProviderError
from services.decision.accessors.mock_provider import MockProvider
from services.decision.service.dapr_secret_loader import (
    load_groq_api_key,
    resolve_provider_from_secret,
)

# --- resolve_provider_from_secret() - pure, no Dapr -------------------------


def test_resolve_provider_from_secret_returns_fallback_when_no_secret() -> None:
    fallback = MockProvider()

    result = resolve_provider_from_secret(None, fallback)

    assert result is fallback


def test_resolve_provider_from_secret_returns_fallback_when_secret_empty_string() -> None:
    fallback = MockProvider()

    result = resolve_provider_from_secret("", fallback)

    assert result is fallback


def test_resolve_provider_from_secret_builds_new_provider_when_valid() -> None:
    fallback = MockProvider()

    result = resolve_provider_from_secret("gsk_valid_key", fallback)

    assert result is not fallback
    assert isinstance(result, GroqProvider)


def test_resolve_provider_from_secret_falls_back_and_logs_when_invalid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fallback = MockProvider()

    def _raise(*args: object, **kwargs: object) -> GroqProvider:
        raise LLMProviderError("boom")

    monkeypatch.setattr(
        "services.decision.service.dapr_secret_loader.GroqProvider", _raise
    )

    with caplog.at_level(logging.WARNING, logger="services.decision.service.dapr_secret_loader"):
        result = resolve_provider_from_secret("some-key", fallback)

    assert result is fallback
    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "boom" in record.getMessage()


# --- load_groq_api_key() - with a fake Dapr secrets client ------------------


class _FakeSecretResponse:
    def __init__(self, secret: dict[str, str]) -> None:
        self.secret = secret


class _FakeSecretClient:
    def __init__(
        self, *, secret: dict[str, str] | None = None, error: Exception | None = None
    ) -> None:
        self._secret = secret or {}
        self._error = error

    async def get_secret(
        self, store_name: str, key: str, secret_metadata: dict[str, str] | None = None
    ) -> _FakeSecretResponse:
        if self._error is not None:
            raise self._error
        return _FakeSecretResponse(self._secret)


async def test_load_groq_api_key_returns_value_when_present() -> None:
    client = _FakeSecretClient(secret={"GROQ_API_KEY": "gsk_from_dapr"})

    result = await load_groq_api_key(client=client)

    assert result == "gsk_from_dapr"


async def test_load_groq_api_key_returns_none_when_store_has_no_value() -> None:
    """Distinct from a grpc error: the call succeeds, but the store has no
    value for this key yet (natural first-run state)."""
    client = _FakeSecretClient(secret={})

    result = await load_groq_api_key(client=client)

    assert result is None


async def test_load_groq_api_key_falls_back_on_grpc_error() -> None:
    client = _FakeSecretClient(error=grpc.RpcError())

    result = await load_groq_api_key(client=client)

    assert result is None


class _HangingSecretClient:
    async def get_secret(
        self, store_name: str, key: str, secret_metadata: dict[str, str] | None = None
    ) -> _FakeSecretResponse:
        await asyncio.sleep(100)  # never resolves within the loader's timeout
        raise AssertionError("unreachable")


async def test_load_groq_api_key_falls_back_on_timeout() -> None:
    client = _HangingSecretClient()

    result = await load_groq_api_key(client=client, timeout=0.05)

    assert result is None


class _ClientConstructionFailsLazily:
    """Same real bug this project already found once for configuration
    (see test_dapr_config_loader.py): LazyDaprClient[...]().get() itself can
    raise (DaprClient()'s constructor does its own blocking wait-for-sidecar
    and raises TimeoutError if the sidecar isn't ready) - must be caught too,
    not just failures from an already-constructed client's get_secret()."""

    def __class_getitem__(cls, item: object) -> type[_ClientConstructionFailsLazily]:
        return cls

    def get(self) -> None:
        raise TimeoutError("Dapr health check timed out, after 60.0.")


async def test_load_groq_api_key_falls_back_when_client_construction_itself_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.decision.service.dapr_secret_loader.LazyDaprClient",
        _ClientConstructionFailsLazily,
    )

    result = await load_groq_api_key(client=None)

    assert result is None
