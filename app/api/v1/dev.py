"""Dev-only endpoints — not registered in production."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderItem, OrderStatus, Payment, PaymentStatus
from app.db.session import get_session
from app.workers.queue import enqueue_process_item

router = APIRouter(prefix="/dev", tags=["dev"])


@router.post("/payments/approve-by-phone", status_code=status.HTTP_200_OK)
async def simulate_approve_by_phone(
    phone: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Approve the most recent AWAITING_PAYMENT order for a guest phone."""
    order = (
        await session.execute(
            select(Order)
            .where(
                Order.guest_phone == phone,
                Order.status == OrderStatus.AWAITING_PAYMENT,
            )
            .order_by(Order.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No AWAITING_PAYMENT order found for phone {phone!r}",
        )

    payment = (
        await session.execute(
            select(Payment)
            .where(Payment.order_id == order.id, Payment.status == PaymentStatus.PENDING)
            .order_by(Payment.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)

    if payment is not None:
        payment.status = PaymentStatus.APPROVED
        payment.paid_at = now

    order.status = OrderStatus.PAID
    order.paid_at = now

    await session.flush()

    item_ids = (
        await session.execute(
            select(OrderItem.id).where(OrderItem.order_id == order.id)
        )
    ).scalars().all()

    await session.commit()

    for item_id in item_ids:
        await enqueue_process_item(item_id)

    return {"order_id": order.id, "items_enqueued": len(item_ids)}
