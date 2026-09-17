"""Auth service domain + request/response models (N1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_serializer

from shared.auth import Role


class User(BaseModel):
    """Persisted domain record - password_hash/salt are hex-encoded strings
    from services.auth.hashing, never a plaintext password."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    email: str
    password_hash: str
    salt: str
    role: Role


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str


class RegisterResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str
    role: Role

    @field_serializer("role")
    def _serialize_role(self, role: Role) -> str:
        # Role is an IntEnum (rank-comparable, see shared/auth.py) - without
        # this, it would serialize as a bare int (e.g. 1) instead of a
        # human-readable string a client can actually read ("submitter").
        return role.name.lower()


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str


class LoginResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    token_type: str = "bearer"
