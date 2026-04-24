"""Auth router — guest claim, register, login, magic link."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_user_token, hash_password, verify_password
from app.db.models import Order, User
from app.db.session import get_session
from app.schemas.v1 import GuestClaimIn, LoginIn, RegisterIn, TokenOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenOut)
async def register(payload: RegisterIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    existing = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="email already registered")

    user = User(
        email=payload.email,
        name=payload.name,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        email_verified_at=datetime.now(UTC),  # in v1 we trust at-signup; magic link in v1.1
    )
    session.add(user)
    await session.flush()
    await session.commit()
    return TokenOut(access_token=create_user_token(user.id), user_id=user.id)


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user is None or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    return TokenOut(access_token=create_user_token(user.id), user_id=user.id)


@router.post("/guest-claim", response_model=TokenOut)
async def claim_guest_order(
    payload: GuestClaimIn, session: AsyncSession = Depends(get_session)
) -> TokenOut:
    """Attach all DRAFT/PAID orders that match the guest email to a new (or
    existing) account. The ``code`` in v1 is a placeholder for the email
    confirmation flow — accept any non-empty value for now and tighten in
    v1.1 with a real one-time code."""
    if not payload.code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="code required")

    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email=payload.email,
            password_hash=hash_password(secrets.token_urlsafe(24)),
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()

    orders = (
        await session.execute(select(Order).where(Order.guest_email == payload.email))
    ).scalars().all()
    for order in orders:
        if order.user_id is None:
            order.user_id = user.id

    await session.commit()
    return TokenOut(access_token=create_user_token(user.id), user_id=user.id)
