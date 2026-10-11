"""Public configuration endpoint — safe to expose without auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.settings import get_settings
from app.services.account_mail import mail_is_configured
from app.services.processing_policy import processing_enabled
from app.db.session import get_session
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import storefront_policy

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
async def get_public_config(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Return client-safe configuration values (public keys only)."""
    s = get_settings()
    policy = await storefront_policy.read_policy(session)
    return JSONResponse({
        "mp_public_key": s.mercadopago_public_key or "",
        "account_email_enabled": mail_is_configured(),
        "video_processing_enabled": processing_enabled() and policy.new_orders_enabled,
        "registration_enabled": policy.registration_enabled,
        "credit_redemption_enabled": policy.credit_redemption_enabled,
        "require_account_checkout": policy.require_account_checkout,
        "signup_bonus": 0, "purchase_bonus": 0,
    }, headers={"Cache-Control": "no-store"})
