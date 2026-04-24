"""Aggregate v1 router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, auth, catalog, chat, orders, payments

router = APIRouter(prefix="/api/v1")
router.include_router(catalog.router)
router.include_router(orders.router)
router.include_router(auth.router)
router.include_router(payments.router)
router.include_router(admin.router)
router.include_router(chat.router)
