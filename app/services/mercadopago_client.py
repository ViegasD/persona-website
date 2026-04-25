"""Mercado Pago Pix integration. Mirrors the Node backend's behaviour but
prefixes every ``external_reference`` with ``web_`` so webhooks routed to this
service don't collide with the WhatsApp bot's ``persona_*`` orders."""

from __future__ import annotations

import asyncio
import hmac
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import mercadopago
import qrcode
from qrcode.constants import ERROR_CORRECT_H

from app.core.settings import get_settings

EXTERNAL_REF_PREFIX = "web_"

_sdk_instance: mercadopago.SDK | None = None


def _sdk() -> mercadopago.SDK:
    global _sdk_instance
    if _sdk_instance is None:
        _sdk_instance = mercadopago.SDK(get_settings().mercadopago_access_token)
    return _sdk_instance


def make_external_reference(order_id: int) -> str:
    return f"{EXTERNAL_REF_PREFIX}{order_id}"


def parse_external_reference(ref: str | None) -> int | None:
    if not ref or not ref.startswith(EXTERNAL_REF_PREFIX):
        return None
    try:
        return int(ref[len(EXTERNAL_REF_PREFIX) :])
    except ValueError:
        return None


async def create_pix_payment(
    *,
    order_id: int,
    amount_cents: int,
    description: str,
    notification_url: str,
    payer_email: str | None = None,
    expires_in_minutes: int = 30,
) -> dict[str, Any]:
    """Create a Pix payment and return ``{paymentId, qrCode, ticketUrl, expiresAt}``."""
    expires_at = datetime.now(UTC) + timedelta(minutes=expires_in_minutes)
    body = {
        "transaction_amount": round(amount_cents / 100, 2),
        "payment_method_id": "pix",
        "description": description,
        "external_reference": make_external_reference(order_id),
        "notification_url": notification_url,
        "payer": {"email": payer_email or "cliente@persona.com.br"},
        "date_of_expiration": expires_at.strftime("%Y-%m-%dT%H:%M:%S.000-03:00"),
    }

    def _create() -> dict[str, Any]:
        return _sdk().payment().create(body)

    response = await asyncio.to_thread(_create)
    http_status = response.get("status")
    payment = response.get("response", {})
    payment_id = payment.get("id")
    if not payment_id or (isinstance(http_status, int) and http_status >= 400):
        error_msg = payment.get("message") or payment.get("error") or f"MercadoPago error (HTTP {http_status})"
        raise RuntimeError(f"MercadoPago payment creation failed: {error_msg}")
    transaction_data = (payment.get("point_of_interaction") or {}).get("transaction_data") or {}
    return {
        "payment_id": str(payment_id),
        "qr_code": transaction_data.get("qr_code", ""),
        "qr_code_base64": transaction_data.get("qr_code_base64", ""),
        "ticket_url": transaction_data.get("ticket_url", ""),
        "expires_at": expires_at,
    }


async def get_payment_details(payment_id: str) -> dict[str, Any]:
    def _get() -> dict[str, Any]:
        return _sdk().payment().get(payment_id)

    response = await asyncio.to_thread(_get)
    payment = response.get("response", {})
    return {
        "id": str(payment.get("id")),
        "status": payment.get("status"),
        "external_reference": payment.get("external_reference"),
        "payment_method": payment.get("payment_method_id"),
        "amount": payment.get("transaction_amount"),
        "paid_at": payment.get("date_approved"),
    }


def render_qr_png(payload: str) -> bytes:
    """Render a Pix copy-and-paste payload as a high-error-correction PNG."""
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=10, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def verify_webhook_signature(
    *,
    secret: str | None,
    x_signature: str | None,
    x_request_id: str | None,
    data_id: str | None,
) -> bool:
    """Implement the MP HMAC-SHA256 verification with timing-safe compare.

    Manifest format: ``id:<data_id_lower>;request-id:<req_id>;ts:<ts>;``
    Returns True if no secret is configured (mirrors Node behaviour).
    """
    if not secret:
        return True
    if not x_signature:
        return False

    ts: str | None = None
    v1: str | None = None
    for part in x_signature.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == "ts":
            ts = v.strip()
        elif k.strip() == "v1":
            v1 = v.strip()
    if not ts or not v1:
        return False

    parts: list[str] = []
    if data_id:
        parts.append(f"id:{data_id.lower()}")
    if x_request_id:
        parts.append(f"request-id:{x_request_id}")
    parts.append(f"ts:{ts}")
    manifest = ";".join(parts) + ";"

    computed = hmac.new(secret.encode(), manifest.encode(), sha256).hexdigest()
    try:
        return hmac.compare_digest(bytes.fromhex(computed), bytes.fromhex(v1))
    except ValueError:
        return False
