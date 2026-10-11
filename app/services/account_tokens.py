"""Hashed, expiring, one-time account credentials."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import (
    AccountToken,
    OutboxJob,
    User,
)
from app.services.account_mail import mail_is_configured


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def welcome_identity(email: str) -> str:
    local, domain = email.strip().lower().rsplit("@", 1)
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return token_digest(local + "@" + domain)


def token_is_usable(token: AccountToken, now: datetime) -> bool:
    return token.used_at is None and token.expires_at > now


async def issue_account_token(session: AsyncSession, user: User, purpose: str) -> None:
    if not mail_is_configured():
        raise HTTPException(503, detail="account email delivery unavailable")
    now = datetime.now(UTC)
    latest = (await session.execute(select(AccountToken).where(
        AccountToken.user_id == user.id, AccountToken.purpose == purpose,
    ).order_by(AccountToken.created_at.desc()).limit(1))).scalar_one_or_none()
    if latest and latest.created_at > now - timedelta(seconds=60):
        return
    await session.execute(update(AccountToken).where(
        AccountToken.user_id == user.id, AccountToken.purpose == purpose,
        AccountToken.used_at.is_(None),
    ).values(used_at=now))
    raw = secrets.token_urlsafe(32)
    token = AccountToken(
        user_id=user.id, token_hash=token_digest(raw), purpose=purpose,
        expires_at=now + (timedelta(hours=24) if purpose == "verify" else timedelta(minutes=30)),
    )
    session.add(token)
    await session.flush()
    link = get_settings().account_page_url.split("#", 1)[0] + "#" + purpose + "=" + quote(raw)
    subject = "Confirme seu e-mail" if purpose == "verify" else "Redefina sua senha"
    session.add(OutboxJob(
        dedup_key=f"account_email:{token.id}", kind="account_email", user_id=user.id,
        payload={"to": user.email, "subject": subject,
                 "body": f"Recado Animado\n\n{subject}:\n{link}\n\nSe voce nao solicitou, ignore esta mensagem."},
    ))


async def consume_account_token(session: AsyncSession, raw: str, purpose: str) -> User:
    # Resolve the user first, then lock user -> token, matching issuance order.
    token = (await session.execute(select(AccountToken).where(
        AccountToken.token_hash == token_digest(raw), AccountToken.purpose == purpose,
    ))).scalar_one_or_none()
    if token is None:
        raise HTTPException(400, detail="invalid or expired account link")
    user = (await session.execute(select(User).where(User.id == token.user_id).with_for_update())).scalar_one_or_none()
    token = (await session.execute(select(AccountToken).where(AccountToken.id == token.id)
                                  .with_for_update().execution_options(populate_existing=True))).scalar_one()
    now = datetime.now(UTC)
    if user is None or not token_is_usable(token, now):
        raise HTTPException(400, detail="invalid or expired account link")
    token.used_at = now
    return user


async def verify_account(session: AsyncSession, raw: str) -> User:
    user = await consume_account_token(session, raw, "verify")
    user.email_verified_at = datetime.now(UTC)
    await grant_welcome_credit(session, user)
    return user


async def grant_welcome_credit(session: AsyncSession, user: User) -> None:
    """Retired reward hook, including Google login: verification grants no credits."""
    return
