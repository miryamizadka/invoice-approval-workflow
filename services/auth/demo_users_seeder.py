"""Loads services/auth/demo_users.json (Approver + Admin demo accounts) at
startup - mirrors services/payment/budgets_loader.py's exact shape. Only
Approver/Admin need seeding: Submitter accounts are created via the normal
POST /auth/register self-service flow (N1 design - Approver/Admin are not
self-registerable, since Approver actions trigger real payments).
"""

from __future__ import annotations

import json
from pathlib import Path

from services.auth.hashing import hash_password
from services.auth.models import User
from services.auth.repository import UserRepository
from shared.auth import Role

DEFAULT_DEMO_USERS_PATH = Path(__file__).resolve().parent / "demo_users.json"


async def seed_demo_users(
    repository: UserRepository, path: Path = DEFAULT_DEMO_USERS_PATH
) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data["users"]:
        password_hash, salt = hash_password(entry["password"])
        user = User(
            email=entry["email"],
            password_hash=password_hash,
            salt=salt,
            role=Role[entry["role"].upper()],
        )
        await repository.ensure_seeded(user)
