"""Password hashing via stdlib hashlib.pbkdf2_hmac + a per-user random
salt (N1) - no new dependency (passlib/bcrypt) needed at this project's
scale, consistent with the minimal-dependency philosophy already applied
twice elsewhere (N5: TF-IDF not embeddings; N4: Zipkin not OTLP).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGORITHM = "sha256"
_ITERATIONS = 260_000  # OWASP-recommended minimum for PBKDF2-HMAC-SHA256 (2023 guidance)
_SALT_BYTES = 16


def hash_password(password: str) -> tuple[str, str]:
    """Returns (password_hash, salt), both hex-encoded strings."""
    salt = secrets.token_hex(_SALT_BYTES)
    digest = _derive(password, salt)
    return digest, salt


def verify_password(password: str, *, password_hash: str, salt: str) -> bool:
    candidate = _derive(password, salt)
    return hmac.compare_digest(candidate, password_hash)


def _derive(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        _ALGORITHM, password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    )
    return digest.hex()
