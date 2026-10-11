"""Auth dependencies — guest cookie + bearer JWT."""

from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_user_claims, verify_guest_orders
from app.core.settings import get_settings
from app.db.models import User
from app.db.session import get_session

GUEST_COOKIE_NAME = "persona_guest_order"


async def get_current_user_id(
    authorization: str | None = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_session),
) -> int | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_user_claims(token)
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid session")
    user = (await session.execute(select(User.id, User.token_version).where(User.id == claims[0]))).one_or_none()
    if user is None or user.token_version != claims[1]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="session expired")
    return user.id


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
