"""Meta Conversions API client — sends server-side events via Stape CAPIG."""

from __future__ import annotations

import hashlib
import logging
import re
import time

import httpx

from app.core.settings import get_settings

logger = logging.getLogger(__name__)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _hash_email(email: str | None) -> str | None:
    if not email:
        return None
    return _sha256(email.strip().lower())


def _hash_phone(phone: str | None) -> str | None:
    """Normalize to digits only then SHA-256."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    # Remove leading country code 55 if present and number is long enough
    if digits.startswith("55") and len(digits) > 11:
        digits = digits[2:]
    return _sha256(digits)


async def report_purchase(
    *,
    order_id: int,
    amount_cents: int,
    email: str | None,
    phone: str | None,
    event_time: int | None = None,
) -> None:
    """Send a Purchase event to Meta via Stape CAPIG (server-side CAPI)."""
    await _send_event(
        event_name="Purchase",
        event_id=f"purchase_{order_id}",
        email=email,
        phone=phone,
        event_time=event_time,
        custom_data={
            "currency": "BRL",
            "value": round(amount_cents / 100, 2),
        },
    )
    logger.info("Meta CAPI Purchase reported for order %s", order_id)


async def report_initiate_checkout(
    *,
    order_id: int,
    amount_cents: int,
    email: str | None,
    phone: str | None,
) -> None:
    """Send an InitiateCheckout event to Meta via Stape CAPIG."""
    await _send_event(
        event_name="InitiateCheckout",
        event_id=f"initiate_checkout_{order_id}",
        email=email,
        phone=phone,
        custom_data={
            "currency": "BRL",
            "value": round(amount_cents / 100, 2),
        },
    )
    logger.info("Meta CAPI InitiateCheckout reported for order %s", order_id)


async def _send_event(
    *,
    event_name: str,
    event_id: str,
    email: str | None,
    phone: str | None,
    event_time: int | None = None,
    custom_data: dict | None = None,
) -> None:
    settings = get_settings()

    url = settings.stape_capig_url
    identifier = settings.stape_capig_identifier
    api_key = settings.stape_capig_api_key

    if not all([url, identifier, api_key]):
        logger.debug("Stape CAPIG not configured — skipping %s", event_name)
        return

    payload = {
        "data": [
            {
                "event_name": event_name,
                "event_time": event_time or int(time.time()),
                "event_id": event_id,
                "action_source": "website",
                "user_data": {
                    **({"em": [_hash_email(email)]} if email else {}),
                    **({"ph": [_hash_phone(phone)]} if phone else {}),
                    "country": ["br"],
                },
                **({"custom_data": custom_data} if custom_data else {}),
            }
        ]
    }

    endpoint = f"{url.rstrip('/')}/{identifier}/events"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                endpoint,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
        if resp.status_code >= 400:
            logger.warning(
                "Meta CAPI %s failed (event_id=%s): %s %s",
                event_name, event_id, resp.status_code, resp.text[:300],
            )
    except Exception:  # noqa: BLE001
        logger.exception("Meta CAPI %s error (event_id=%s)", event_name, event_id)
