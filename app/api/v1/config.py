"""Public configuration endpoint — safe to expose without auth."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.settings import get_settings
from app.services.account_mail import mail_is_configured
from app.services.processing_policy import processing_enabled

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
async def get_public_config() -> JSONResponse:
    """Return client-safe configuration values (public keys only)."""
    s = get_settings()
    return JSONResponse({
        "mp_public_key": s.mercadopago_public_key or "",
        "account_email_enabled": mail_is_configured(),
        "video_processing_enabled": processing_enabled(),
    }, headers={"Cache-Control": "no-store"})
