"""Tests for shared/jwt_secret_loader.py (M5/N1) - the same fetch-once,
never-raise, resilient-fallback posture as
services/decision/service/dapr_secret_loader.py (GROQ_API_KEY), applied to
JWT_SECRET.

resolve_jwt_secret() is pure (str | None, str | None in -> str | None out) -
tested with no Dapr involved at all, mirroring resolve_provider_from_secret()'s
tests. load_jwt_secret_from_dapr() is I/O (fake async Dapr secrets client) -
same style as test_dapr_secret_loader.py's load_groq_api_key() tests.
"""

from __future__ import annotations

import asyncio

import grpc
import pytest

from shared.jwt_secret_loader import load_jwt_secret_from_dapr, resolve_jwt_secret

# --- resolve_jwt_secret() - pure, no Dapr -----------------------------------


def test_resolve_jwt_secret_prefers_dapr_value_when_present() -> None:
    result = resolve_jwt_secret("dapr-secret", "env-secret")

    assert result == "dapr-secret"


def test_resolve_jwt_secret_falls_back_to_env_when_dapr_value_is_none() -> None:
    result = resolve_jwt_secret(None, "env-secret")

    assert result == "env-secret"


def test_resolve_jwt_secret_falls_back_to_env_when_dapr_value_is_empty_string() -> None:
    result = resolve_jwt_secret("", "env-secret")

    assert result == "env-secret"


def test_resolve_jwt_secret_returns_none_when_neither_source_has_a_value() -> None:
    result = resolve_jwt_secret(None, None)

    assert result is None


# --- load_jwt_secret_from_dapr() - with a fake Dapr secrets client ---------


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


async def test_load_jwt_secret_from_dapr_returns_value_when_present() -> None:
    client = _FakeSecretClient(secret={"JWT_SECRET": "secret-from-dapr"})

    result = await load_jwt_secret_from_dapr(client=client)

    assert result == "secret-from-dapr"


async def test_load_jwt_secret_from_dapr_returns_none_when_store_has_no_value() -> None:
    """Distinct from a grpc error: the call succeeds, but the store has no
    value for this key yet (natural first-run state)."""
    client = _FakeSecretClient(secret={})

    result = await load_jwt_secret_from_dapr(client=client)

    assert result is None


async def test_load_jwt_secret_from_dapr_falls_back_on_grpc_error() -> None:
    client = _FakeSecretClient(error=grpc.RpcError())

    result = await load_jwt_secret_from_dapr(client=client)

    assert result is None


class _HangingSecretClient:
    async def get_secret(
        self, store_name: str, key: str, secret_metadata: dict[str, str] | None = None
    ) -> _FakeSecretResponse:
        await asyncio.sleep(100)  # never resolves within the loader's timeout
        raise AssertionError("unreachable")


async def test_load_jwt_secret_from_dapr_falls_back_on_timeout() -> None:
    client = _HangingSecretClient()

    result = await load_jwt_secret_from_dapr(client=client, timeout=0.05)

    assert result is None


class _ClientConstructionFailsLazily:
    """Same real bug this project already found once for configuration/GROQ
    (see test_dapr_secret_loader.py): LazyDaprClient[...]().get() itself can
    raise (DaprClient()'s constructor does its own blocking wait-for-sidecar
    and raises TimeoutError if the sidecar isn't ready) - must be caught too,
    not just failures from an already-constructed client's get_secret()."""

    def __class_getitem__(cls, item: object) -> type[_ClientConstructionFailsLazily]:
        return cls

    def get(self) -> None:
        raise TimeoutError("Dapr health check timed out, after 60.0.")


async def test_load_jwt_secret_from_dapr_falls_back_when_client_construction_itself_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "shared.jwt_secret_loader.LazyDaprClient",
        _ClientConstructionFailsLazily,
    )

    result = await load_jwt_secret_from_dapr(client=None)

    assert result is None
