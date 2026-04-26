"""Public configuration endpoint — safe to expose without auth."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.settings import get_settings

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
async def get_public_config() -> JSONResponse:
    """Return client-safe configuration values (public keys only)."""
    s = get_settings()
    return JSONResponse({"mp_public_key": s.mercadopago_public_key or ""})
