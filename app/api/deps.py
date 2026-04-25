"""Auth dependencies — guest cookie + bearer JWT."""

from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException, status

from app.core.security import decode_user_token, verify_guest_orders
from app.core.settings import get_settings

GUEST_COOKIE_NAME = "persona_guest_order"


def get_current_user_id(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> int | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return decode_user_token(token)


def get_guest_order_ids(
    cookie: str | None = Cookie(default=None, alias=GUEST_COOKIE_NAME),
) -> list[int]:
    if not cookie:
        return []
    return verify_guest_orders(cookie)


def require_admin_key(
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> None:
    expected = get_settings().admin_api_key
    if not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin key")


def require_user(user_id: int | None = Depends(get_current_user_id)) -> int:
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    return user_id
