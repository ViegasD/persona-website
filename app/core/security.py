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


def sign_guest_order(order_id: int) -> str:
    return _guest_serializer().dumps({"order_id": order_id})


def verify_guest_order(token: str, max_age_seconds: int = 60 * 60 * 24 * 30) -> int | None:
    try:
        data = _guest_serializer().loads(token, max_age=max_age_seconds)
        return int(data["order_id"])
    except (BadSignature, KeyError, ValueError):
        return None
