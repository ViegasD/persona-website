"""Auth router — guest claim, register, login, magic link."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_guest_order_ids, require_user
from app.core.security import (
    create_user_token,
    decode_user_claims,
    hash_password,
    verify_guest_orders,
    verify_password,
)
from app.core.settings import get_settings
from app.db.models import Order, User
from app.db.session import get_session
from app.schemas.v1 import (
    AccountEmailIn,
    AccountTokenIn,
    GoogleCredentialIn,
    GuestClaimIn,
    LoginIn,
    PasswordResetIn,
    RegisterIn,
    TokenOut,
    UserOut,
)
from app.services import google_auth
from app.services import storefront_policy
from app.services.account_mail import mail_is_configured
from app.services.account_tokens import consume_account_token, issue_account_token, verify_account

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/google/config")
async def google_config() -> dict:
    audience = get_settings().google_client_id
    return {"enabled": bool(audience)}


@router.post("/google/challenge")
async def google_challenge(response: Response, session: AsyncSession = Depends(get_session)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return await google_auth.issue_challenge(session)


@router.post("/google/login", response_model=TokenOut)
async def google_login(payload: GoogleCredentialIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    user = await google_auth.authenticate(session, payload.credential, payload.nonce)
    return TokenOut(access_token=create_user_token(user.id, user.token_version), user_id=user.id)


@router.post("/google/link", response_model=TokenOut)
async def google_link(payload: GoogleCredentialIn, user_id: int = Depends(require_user),
                      authorization: str | None = Header(default=None, alias="Authorization"),
                      session: AsyncSession = Depends(get_session)) -> TokenOut:
    claims = decode_user_claims((authorization or "").split(" ", 1)[-1])
    if claims is None or claims[0] != user_id:
        raise HTTPException(401, detail="invalid session")
    user = await google_auth.authenticate(session, payload.credential, payload.nonce, link_user_id=user_id,
                                          expected_token_version=claims[1])
    return TokenOut(access_token=create_user_token(user.id, user.token_version), user_id=user.id)

@router.post("/register", response_model=TokenOut)
async def register(payload: RegisterIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    if not (await storefront_policy.read_policy(session)).registration_enabled:
        raise HTTPException(503, detail="registration temporarily unavailable")
    email = str(payload.email).lower()
    existing = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="email already registered")

    user = User(
        email=email,
        name=payload.name,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        credit_balance=0,
        email_verified_at=None,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="email already registered") from exc
    await issue_account_token(session, user, "verify")
    await session.commit()
    return TokenOut(access_token=create_user_token(user.id, user.token_version), user_id=user.id)


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    user = (
        await session.execute(select(User).where(func.lower(User.email) == str(payload.email).lower()))
    ).scalar_one_or_none()
    if user is None or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    return TokenOut(access_token=create_user_token(user.id, user.token_version), user_id=user.id)


@router.get("/me", response_model=UserOut)
async def me(
    user_id: int = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> UserOut:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        phone=user.phone,
        credit_balance=user.credit_balance,
        credit_debt=user.credit_debt,
        email_verified=user.email_verified_at is not None,
        google_linked=user.google_sub is not None,
        created_at=user.created_at,
    )


@router.post("/verify-email", response_model=TokenOut)
async def verify_email(payload: AccountTokenIn, session: AsyncSession = Depends(get_session)) -> TokenOut:
    user = await verify_account(session, payload.token)
    await session.commit()
    return TokenOut(access_token=create_user_token(user.id, user.token_version), user_id=user.id)


@router.post("/resend-verification", status_code=202)
async def resend_verification(user_id: int = Depends(require_user), session: AsyncSession = Depends(get_session)) -> dict:
    user = (await session.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    if user.email_verified_at is None:
        await issue_account_token(session, user, "verify")
        await session.commit()
    return {"message": "verification email requested"}


@router.post("/forgot-password", status_code=202)
async def forgot_password(payload: AccountEmailIn, session: AsyncSession = Depends(get_session)) -> dict:
    if not mail_is_configured():
        raise HTTPException(503, detail="account email delivery unavailable")
    user = (await session.execute(select(User).where(
        func.lower(User.email) == str(payload.email).lower(),
    ).with_for_update())).scalar_one_or_none()
    if user is not None:
        await issue_account_token(session, user, "reset")
        await session.commit()
    return {"message": "if the account exists, a reset link will be sent"}


@router.post("/reset-password")
async def reset_password(payload: PasswordResetIn, session: AsyncSession = Depends(get_session)) -> dict:
    user = await consume_account_token(session, payload.token, "reset")
    user.password_hash = hash_password(payload.password)
    user.token_version += 1
    await session.commit()
    return {"message": "password updated; sign in again"}


@router.post("/guest-claim", response_model=TokenOut)
async def claim_guest_order(
    payload: GuestClaimIn,
    user_id: int = Depends(require_user),
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    x_guest_token: str | None = Header(default=None, alias="X-Guest-Token"),
    session: AsyncSession = Depends(get_session),
) -> TokenOut:
    """Attach signed guest orders to the authenticated account."""
    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.email or user.email.lower() != str(payload.email).lower():
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="account email mismatch")
    owned_ids = set(guest_order_ids)
    if x_guest_token:
        owned_ids.update(verify_guest_orders(x_guest_token))
    if not owned_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="guest order ownership required")
    orders = (
        await session.execute(select(Order).where(
            Order.id.in_(owned_ids), Order.user_id.is_(None),
            func.lower(Order.guest_email) == str(payload.email).lower(),
        ).with_for_update())
    ).scalars().all()
    for order in orders:
        if order.user_id is None:
            order.user_id = user.id

    await session.commit()
    return TokenOut(access_token=create_user_token(user.id, user.token_version), user_id=user.id)
