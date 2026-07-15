"""Shared JWT issuance/verification + FastAPI dependencies for role-gated
routes (N1). First Depends()-based DI in this codebase - existing routes
keep pulling services off request.app.state directly (unrelated to auth,
unchanged); this is purely additive.

Role is an IntEnum, not a StrEnum: rank IS the value (Admin > Approver >
Submitter), so a plain `>=` comparison alone implements the hierarchy, with
no separate rank-lookup table. require_role() takes a single minimum role,
not a variadic set - the real role matrix (see project/MASTER_CHECKLIST.md
N1 / the PR description) is entirely hierarchical; if a future requirement
ever needs a non-hierarchical check (e.g. "Submitter or Admin but not
Approver"), this would need to become require_roles(*roles) with a
membership test instead of >= - not built now since nothing today needs it.

decode_token pins algorithms=["HS256"] explicitly rather than letting PyJWT
infer the algorithm from the token's own header - this is a deliberate
algorithm-confusion guard (a token signed "none" or with a different
algorithm must never be accepted just because an attacker controls the alg
header).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Coroutine
from enum import IntEnum
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

ALGORITHM = "HS256"
DEFAULT_EXPIRY_HOURS = 8


class Role(IntEnum):
    SUBMITTER = 1
    APPROVER = 2
    ADMIN = 3


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    sub: str
    role: Role


class TokenError(Exception):
    """Raised on any invalid, expired, malformed, or algorithm-mismatched JWT."""


def create_access_token(
    *, subject: str, role: Role, secret: str, expires_hours: int = DEFAULT_EXPIRY_HOURS
) -> str:
    now = int(time.time())
    claims = {
        "sub": subject,
        "role": role.name.lower(),
        "iat": now,
        "exp": now + expires_hours * 3600,
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def decode_token(token: str, *, secret: str) -> AuthenticatedUser:
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError as exc:
        raise TokenError(str(exc)) from exc

    sub = claims.get("sub")
    role_raw = claims.get("role")
    if not sub or not role_raw:
        raise TokenError("token missing required sub/role claim")
    try:
        role = Role[str(role_raw).upper()]
    except KeyError as exc:
        raise TokenError(f"unknown role: {role_raw!r}") from exc

    return AuthenticatedUser(sub=sub, role=role)


def _get_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET not configured")
    return secret


async def get_current_user(request: Request) -> AuthenticatedUser:
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = header.removeprefix("Bearer ")
    try:
        return decode_token(token, secret=_get_secret())
    except TokenError as exc:
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc


def require_role(minimum: Role) -> Callable[..., Coroutine[Any, Any, AuthenticatedUser]]:
    async def _check(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if user.role < minimum:
            raise HTTPException(status_code=403, detail="insufficient role")
        return user

    return _check
