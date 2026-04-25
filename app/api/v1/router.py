"""Aggregate v1 router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, auth, catalog, chat, dev, orders, payments
from app.core.settings import get_settings

router = APIRouter(prefix="/api/v1")
router.include_router(catalog.router)
router.include_router(orders.router)
router.include_router(auth.router)
router.include_router(payments.router)
router.include_router(admin.router)
router.include_router(chat.router)

if get_settings().env != "production":
    router.include_router(dev.router)
