"""UTMify API client — reports approved sales for attribution tracking."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.core.settings import get_settings

logger = logging.getLogger(__name__)

UTMIFY_ENDPOINT = "https://api.utmify.com.br/api-credentials/orders"


async def report_sale(
    *,
    order_id: int,
    amount_cents: int,
    paid_at: datetime,
    created_at: datetime,
    payment_method: str,
    customer_name: str | None,
    customer_email: str | None,
    customer_phone: str | None,
    plan_slug: str,
    plan_name: str,
    utm_source: str | None,
    utm_medium: str | None,
    utm_campaign: str | None,
    utm_content: str | None,
    utm_term: str | None,
    utm_sck: str | None,
    utm_src: str | None,
) -> None:
    """Fire-and-forget: send a paid order to UTMify for attribution."""
    settings = get_settings()
    token = settings.utmify_api_token
    if not token:
        logger.debug("UTMIFY_API_TOKEN not set — skipping UTMify report")
        return

    def _iso(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = {
        "isTest": False,
        "status": "paid",
        "orderId": str(order_id),
        "platform": "Mercado Pago",
        "paymentMethod": payment_method,
        "createdAt": _iso(created_at),
        "approvedDate": _iso(paid_at),
        "refundedAt": None,
        "customer": {
            "name": customer_name or "",
            "email": customer_email or "",
            "phone": customer_phone or "",
            "country": "BR",
            "document": None,
        },
        "products": [
            {
                "id": plan_slug,
                "name": plan_name,
                "planId": plan_slug,
                "planName": plan_name,
                "quantity": 1,
                "priceInCents": amount_cents,
            }
        ],
        "commission": {
            "gatewayFeeInCents": 0,
            "totalPriceInCents": amount_cents,
            "userCommissionInCents": amount_cents,
        },
        "trackingParameters": {
            "sck": utm_sck,
            "src": utm_src,
            "utm_source": utm_source,
            "utm_medium": utm_medium,
            "utm_campaign": utm_campaign,
            "utm_content": utm_content,
            "utm_term": utm_term,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                UTMIFY_ENDPOINT,
                json=payload,
                headers={"Content-Type": "application/json", "x-api-token": token},
            )
        if resp.status_code >= 400:
            logger.warning(
                "UTMify report failed for order %s: %s %s",
                order_id, resp.status_code, resp.text[:200],
            )
        else:
            logger.info("UTMify sale reported for order %s", order_id)
    except Exception:  # noqa: BLE001
        logger.exception("UTMify report error for order %s", order_id)
