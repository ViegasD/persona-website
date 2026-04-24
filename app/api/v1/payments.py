"""Mercado Pago webhook + payment status polling."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import Order, OrderStatus, Payment, PaymentStatus
from app.db.session import get_session
from app.services import batch_collector, mercadopago_client

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/mercadopago/webhook", status_code=status.HTTP_200_OK)
async def mercadopago_webhook(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, bool]:
    settings = get_settings()
    headers = request.headers
    body = await request.json()
    qs = dict(request.query_params)
    data_id = qs.get("data.id") or (body.get("data") or {}).get("id")

    if not mercadopago_client.verify_webhook_signature(
        secret=settings.mercadopago_webhook_secret,
        x_signature=headers.get("x-signature"),
        x_request_id=headers.get("x-request-id"),
        data_id=str(data_id) if data_id is not None else None,
    ):
        # Always 200 to avoid leaking info.
        return {"received": True}

    if body.get("type") != "payment" or not data_id:
        return {"received": True}

    details = await mercadopago_client.get_payment_details(str(data_id))
    order_id = mercadopago_client.parse_external_reference(details.get("external_reference"))
    if order_id is None:
        # Not one of ours; ignore.
        return {"received": True}

    payment = (
        await session.execute(
            select(Payment).where(
                Payment.provider == "mercadopago", Payment.provider_id == details["id"]
            )
        )
    ).scalar_one_or_none()
    if payment is None:
        # Could happen if the webhook arrives before our DB write commits;
        # fall back to looking up by order id.
        payment = (
            await session.execute(
                select(Payment).where(Payment.order_id == order_id).order_by(Payment.id.desc())
            )
        ).scalar_one_or_none()
    if payment is None:
        return {"received": True}

    payment.raw_webhook = details
    if details.get("status") == "approved" and payment.status != PaymentStatus.APPROVED:
        payment.status = PaymentStatus.APPROVED
        payment.paid_at = datetime.now(UTC)

        order = (
            await session.execute(select(Order).where(Order.id == order_id))
        ).scalar_one_or_none()
        if order is not None and order.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.DRAFT}:
            order.status = OrderStatus.PAID
            order.paid_at = datetime.now(UTC)
            # Attach to the open batch immediately.
            await batch_collector.attach_paid_order(session, order.id)

    elif details.get("status") in {"rejected", "cancelled"}:
        payment.status = PaymentStatus.REJECTED

    await session.commit()
    return {"received": True}


@router.get("/{payment_id}")
async def get_payment(payment_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id))
    ).scalar_one_or_none()
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="payment not found")
    return {
        "id": payment.id,
        "order_id": payment.order_id,
        "status": payment.status.value,
        "amount_cents": payment.amount_cents,
        "expires_at": payment.expires_at,
        "paid_at": payment.paid_at,
    }
