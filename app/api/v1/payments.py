"""Mercado Pago webhook + payment status polling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, get_guest_order_ids
from app.api.v1.orders import _load_order
from app.core.security import verify_guest_orders
from app.core.settings import get_settings
from app.db.models import Order, OrderItem, OrderStatus, Payment, PaymentStatus
from app.db.session import get_session
from app.services import mercadopago_client
from app.services.credits import grant_paid_order_credit, reverse_order_credits
from app.services.meta_capi_client import report_purchase
from app.services.outbox import record_video_jobs

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

    # Use the same order-first lock order as checkout to avoid deadlocks.
    order = (await session.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    )).scalar_one_or_none()
    if order is None:
        return {"received": True}

    payment = (
        await session.execute(
            select(Payment).where(
                Payment.provider == "mercadopago", Payment.provider_id == str(details["id"]),
                Payment.order_id == order_id,
            )
        )
    ).scalar_one_or_none()
    if payment is None:
        # Never apply a provider event to an unrelated payment attempt.
        return {"received": True}

    payment.raw_webhook = details
    if details.get("status") in {"refunded", "charged_back"}:
        refunded = details.get("amount_refunded")
        amount = details.get("amount")
        full_refund = details.get("status") == "charged_back" or refunded is None or (amount is not None and Decimal(str(refunded)) >= Decimal(str(amount)))
        if full_refund:
            payment.status = PaymentStatus.REFUNDED
        if full_refund and payment.funds_order and order.status != OrderStatus.REFUNDED:
            order.status = OrderStatus.REFUNDED
            await reverse_order_credits(session, order)
        await session.commit()
        return {"received": True}
    if details.get("status") == "approved" and payment.status not in {PaymentStatus.APPROVED, PaymentStatus.REFUNDED}:
        payment.status = PaymentStatus.APPROVED
        payment.paid_at = datetime.now(UTC)

        if order is not None and order.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.DRAFT}:
            payment.funds_order = True
            order.status = OrderStatus.PAID
            order.paid_at = datetime.now(UTC)
            await grant_paid_order_credit(session, order)
            await session.flush()
            # Enqueue per-item phase-1 processing immediately (no batch waiting).
            item_ids = (
                await session.execute(
                    select(OrderItem.id).where(OrderItem.order_id == order.id)
                )
            ).scalars().all()
            await record_video_jobs(session, order, list(item_ids))
            await session.commit()

            # Report Purchase to Meta via Facebook CAPI (server-side)
            asyncio.create_task(report_purchase(
                order_id=order.id,
                amount_cents=payment.amount_cents,
                email=order.guest_email,
                phone=order.guest_phone,
                event_time=int(order.paid_at.timestamp()),
            ))

            return {"received": True}

    elif details.get("status") in {"rejected", "cancelled"} and payment.status == PaymentStatus.PENDING:
        payment.status = PaymentStatus.REJECTED

    await session.commit()
    return {"received": True}


@router.get("/{payment_id}")
async def get_payment(
    payment_id: int,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    x_guest_token: str | None = Header(default=None, alias="X-Guest-Token"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if user_id is None and not guest_order_ids and not verify_guest_orders(x_guest_token or ""):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="login or guest token required")
    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id))
    ).scalar_one_or_none()
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="payment not found")
    await _load_order(session, payment.order_id, user_id=user_id,
                      guest_order_ids=guest_order_ids, guest_token=x_guest_token)
    return {
        "id": payment.id,
        "order_id": payment.order_id,
        "status": payment.status.value,
        "amount_cents": payment.amount_cents,
        "expires_at": payment.expires_at,
        "paid_at": payment.paid_at,
    }
