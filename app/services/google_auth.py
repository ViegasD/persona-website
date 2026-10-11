"""Verify Google credentials, then bind stable subjects to local accounts."""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import GoogleChallenge, User
from app.services.account_tokens import grant_welcome_credit, issue_account_token, token_digest


def client_id() -> str:
    value = get_settings().google_client_id
    if not value:
        raise HTTPException(503, detail="Google sign-in is not configured")
    return value


async def issue_challenge(session: AsyncSession) -> dict:
    audience = client_id()
    now = datetime.now(UTC)
    await session.execute(delete(GoogleChallenge).where(GoogleChallenge.expires_at < now))
    nonce = secrets.token_urlsafe(32)
    session.add(GoogleChallenge(nonce_hash=token_digest(nonce), expires_at=now + timedelta(minutes=10)))
    await session.commit()
    return {"client_id": audience, "nonce": nonce}


async def verify_credential(credential: str, nonce: str) -> dict:
    audience = client_id()
    transport = GoogleRequest()

    def fetch(url, method="GET", **kwargs):
        kwargs["timeout"] = 10
        return transport(url, method=method, **kwargs)

    try:
        claims = await asyncio.to_thread(id_token.verify_oauth2_token, credential, fetch, audience)
        if (not isinstance(claims.get("sub"), str) or not claims["sub"] or len(claims["sub"]) > 255
                or claims.get("email_verified") is not True
                or not isinstance(claims.get("nonce"), str)
                or not secrets.compare_digest(claims["nonce"].encode(), nonce.encode())):
            raise ValueError("invalid claims")
        claims["email"] = str(TypeAdapter(EmailStr).validate_python(claims.get("email"))).lower()
        return claims
    except TransportError as exc:
        raise HTTPException(503, detail="Google verification temporarily unavailable") from exc
    except (TypeError, ValueError, ValidationError, GoogleAuthError) as exc:
        raise HTTPException(401, detail="invalid or expired Google credential") from exc


def email_is_authoritative(claims: dict) -> bool:
    return claims.get("email_verified") is True and (
        claims["email"].endswith("@gmail.com") or bool(claims.get("hd"))
    )


async def authenticate(session: AsyncSession, credential: str, nonce: str, link_user_id: int | None = None,
                       expected_token_version: int | None = None) -> User:
    # End the request's auth read before network verification and nonce handling.
    await session.rollback()
    claims = await verify_credential(credential, nonce)
    now = datetime.now(UTC)
    # Commit consumption separately from binding, without a nested connection.
    consumed = (await session.execute(update(GoogleChallenge).where(
        GoogleChallenge.nonce_hash == token_digest(nonce), GoogleChallenge.used_at.is_(None),
        GoogleChallenge.expires_at > now,
    ).values(used_at=now).returning(GoogleChallenge.nonce_hash))).scalar_one_or_none()
    if consumed is None:
        raise HTTPException(401, detail="Google sign-in expired; try again")
    await session.commit()
    # Serialize subject/email signup races before taking account locks.
    for key in ["google:" + claims["sub"], "email:" + claims["email"]]:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
    user = (await session.execute(select(User).where(User.google_sub == claims["sub"])
                                  .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if link_user_id is not None:
        if user is not None and user.id != link_user_id:
            raise HTTPException(409, detail="Esta conta Google ja esta vinculada a outra conta.")
        if user is None:
            user = (await session.execute(select(User).where(User.id == link_user_id).with_for_update()
                                          .execution_options(populate_existing=True))).scalar_one()
            if user.google_sub and user.google_sub != claims["sub"]:
                raise HTTPException(409, detail="Sua conta ja tem outro Google vinculado.")
        if expected_token_version is None or user.token_version != expected_token_version:
            raise HTTPException(401, detail="session expired; sign in again before linking Google")
        if not user.email or user.email.lower() != claims["email"]:
            raise HTTPException(409, detail="Use a conta Google com o mesmo e-mail da sua conta.")
        user.google_sub = claims["sub"]
    elif user is None:
        existing = (await session.execute(select(User.id).where(func.lower(User.email) == claims["email"]))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(409, detail="Entre com sua senha e vincule o Google em Minha conta.")
        user = User(email=claims["email"], name=str(claims.get("name") or "")[:255] or None,
                    google_sub=claims["sub"], credit_balance=0)
        session.add(user)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(409, detail="Conta ja existe; entre e vincule o Google.") from exc
        if not email_is_authoritative(claims):
            await issue_account_token(session, user, "verify")
    # Never rebind stored email or award another credit if Google changes email.
    if user.email and user.email.lower() == claims["email"] and email_is_authoritative(claims):
        user.email_verified_at = user.email_verified_at or now
        await grant_welcome_credit(session, user)
    await session.commit()
    return user
