"""Fail closed for paused processing and orders outside the approved window."""

from app.core.settings import get_settings
from app.db.models import Order


def processing_enabled() -> bool:
    settings = get_settings()
    return settings.worker_enabled and settings.processing_start_at is not None


def processing_allowed(order: Order) -> bool:
    return bool(processing_enabled() and order.created_at is not None
                and order.created_at >= get_settings().processing_start_at)
