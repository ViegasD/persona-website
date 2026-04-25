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


@router.get("/orders-by-phone")
async def orders_by_phone(
    phone: str,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List all orders for a guest phone (debug). Uses LIKE for partial match."""
    orders = (
        await session.execute(
            select(Order)
            .where(Order.guest_phone.like(f"%{phone}%"))
            .order_by(Order.id.desc())
            .limit(20)
        )
    ).scalars().all()
    return [{"id": o.id, "status": o.status, "guest_phone": o.guest_phone, "created_at": str(o.created_at)} for o in orders]


@router.get("/orders-recent")
async def orders_recent(
    limit: int = 10,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List most recent orders (debug)."""
    orders = (
        await session.execute(
            select(Order).order_by(Order.id.desc()).limit(limit)
        )
    ).scalars().all()
    return [{"id": o.id, "status": o.status, "guest_phone": o.guest_phone, "created_at": str(o.created_at)} for o in orders]


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
                Order.guest_phone.like(f"%{phone}%"),
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

    return await _approve_order(order.id, session)


async def _approve_order(order_id: int, session: AsyncSession) -> dict:
    """Shared logic to approve an order by ID."""
    order = (
        await session.execute(select(Order).where(Order.id == order_id))
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Order {order_id} not found")

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
        await session.execute(select(OrderItem.id).where(OrderItem.order_id == order.id))
    ).scalars().all()
    await session.commit()

    errors = []
    for item_id in item_ids:
        try:
            await enqueue_process_item(item_id)
        except Exception as exc:
            errors.append({"item_id": item_id, "error": str(exc)})

    return {"order_id": order.id, "items_enqueued": len(item_ids) - len(errors), "enqueue_errors": errors}


@router.post("/payments/approve-by-order/{order_id}", status_code=status.HTTP_200_OK)
async def simulate_approve_by_order(
    order_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Approve a specific order by ID."""
    return await _approve_order(order_id, session)
