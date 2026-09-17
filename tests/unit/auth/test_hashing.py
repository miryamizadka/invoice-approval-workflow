"""Tests for services/auth/hashing.py - password hashing via stdlib
hashlib.pbkdf2_hmac + a per-user random salt (N1), not passlib/bcrypt (no
new dependency needed at this project's scale)."""

from __future__ import annotations

from services.auth.hashing import hash_password, verify_password


def test_verify_password_true_for_correct_password() -> None:
    password_hash, salt = hash_password("correct horse battery staple")

    assert verify_password(
        "correct horse battery staple", password_hash=password_hash, salt=salt
    )


def test_verify_password_false_for_wrong_password() -> None:
    password_hash, salt = hash_password("correct horse battery staple")

    assert not verify_password("wrong password", password_hash=password_hash, salt=salt)


def test_two_hashes_of_the_identical_password_differ() -> None:
    """Proves a real per-user salt is actually being generated and used -
    not a shared/missing salt, a real correctness risk when hand-rolling
    this instead of using a library that forces it."""
    hash_one, salt_one = hash_password("same password")
    hash_two, salt_two = hash_password("same password")

    assert salt_one != salt_two
    assert hash_one != hash_two


def test_verify_password_false_when_salt_does_not_match() -> None:
    password_hash, _ = hash_password("correct horse battery staple")
    _, other_salt = hash_password("a different password")

    assert not verify_password(
        "correct horse battery staple", password_hash=password_hash, salt=other_salt
    )
