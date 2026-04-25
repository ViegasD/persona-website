"""Authentication helpers: password hashing, JWTs, signed guest-order cookies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core.settings import get_settings

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False


def create_user_token(user_id: int) -> str:
    settings = get_settings()
    payload = {
        "sub": str(user_id),
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(hours=settings.jwt_expiry_hours),
        "kind": "user",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_user_token(token: str) -> int | None:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        if payload.get("kind") != "user":
            return None
        return int(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        return None


def _guest_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().guest_cookie_secret, salt="guest-order")


def sign_guest_orders(order_ids: list[int]) -> str:
    """Sign a list of order IDs into a single cookie value."""
    return _guest_serializer().dumps({"order_ids": order_ids})


def verify_guest_orders(token: str, max_age_seconds: int = 60 * 60 * 24 * 30) -> list[int]:
    """Verify and decode the guest cookie. Supports both old single-id and new list format."""
    try:
        data = _guest_serializer().loads(token, max_age=max_age_seconds)
        # New format: {"order_ids": [1, 2, ...]}
        if "order_ids" in data:
            return [int(i) for i in data["order_ids"]]
        # Legacy format: {"order_id": N}
        if "order_id" in data:
            return [int(data["order_id"])]
        return []
    except (BadSignature, KeyError, ValueError):
        return []


# Legacy aliases kept for any callers outside orders.py
def sign_guest_order(order_id: int) -> str:
    return sign_guest_orders([order_id])


def verify_guest_order(token: str, max_age_seconds: int = 60 * 60 * 24 * 30) -> int | None:
    ids = verify_guest_orders(token, max_age_seconds)
    return ids[0] if ids else None
