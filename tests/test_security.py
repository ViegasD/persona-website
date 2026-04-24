"""Tests for the JWT and guest-order signed cookie helpers."""

from __future__ import annotations

from app.core import security


def test_user_token_roundtrip() -> None:
    token = security.create_user_token(42)
    assert security.decode_user_token(token) == 42


def test_user_token_rejects_garbage() -> None:
    assert security.decode_user_token("not-a-jwt") is None


def test_guest_order_signature_roundtrip() -> None:
    sig = security.sign_guest_order(7)
    assert security.verify_guest_order(sig) == 7
    assert security.verify_guest_order("garbage") is None


def test_password_hash_verifies() -> None:
    hashed = security.hash_password("hunter22")
    assert security.verify_password("hunter22", hashed)
    assert not security.verify_password("wrong", hashed)
