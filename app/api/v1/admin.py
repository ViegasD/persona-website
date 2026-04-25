"""Admin endpoints (gated by ``X-Api-Key``)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_admin_key
from app.db.models import (
    Batch,
    BatchStatus,
    BatchTrigger,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
)
from app.db.session import get_session
from app.schemas.v1 import OrderOut, PlanCreate, PlanOut, PlanUpdate
from app.services import batch_collector

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_key)])


# ── Plans CRUD ─────────────────────────────────────────────────────────────


@router.get("/plans", response_model=list[PlanOut])
async def list_all_plans(session: AsyncSession = Depends(get_session)) -> list[Plan]:
    return list(
        (await session.execute(select(Plan).order_by(Plan.sort_order, Plan.id))).scalars().all()
    )


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(payload: PlanCreate, session: AsyncSession = Depends(get_session)) -> Plan:
    plan = Plan(**payload.model_dump())
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


@router.put("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: int, payload: PlanUpdate, session: AsyncSession = Depends(get_session)
) -> Plan:
    plan = (await session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(plan, k, v)
    await session.commit()
    await session.refresh(plan)
    return plan


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_plan(plan_id: int, session: AsyncSession = Depends(get_session)) -> None:
    plan = (await session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    plan.is_active = False
    await session.commit()


# ── Orders ─────────────────────────────────────────────────────────────────


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    status_filter: OrderStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[OrderOut]:
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.plan))
        .order_by(desc(Order.created_at))
        .limit(limit)
        .offset(offset)
    )
    if status_filter is not None:
        stmt = stmt.where(Order.status == status_filter)
    orders = (await session.execute(stmt)).scalars().all()
    from app.api.v1.orders import _to_out  # local import to avoid cycle

    return [_to_out(o) for o in orders]


@router.post("/orders/{order_id}/refund", status_code=status.HTTP_204_NO_CONTENT)
async def refund_order(order_id: int, session: AsyncSession = Depends(get_session)) -> None:
    order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    order.status = OrderStatus.REFUNDED
    await session.commit()


@router.post("/orders/{order_id}/approve", status_code=status.HTTP_200_OK)
async def approve_order(order_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Force-approve a payment for testing (bypasses MercadoPago)."""
    order = (
        await session.execute(
            select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    payment = (
        await session.execute(
            select(Payment).where(Payment.order_id == order_id).order_by(Payment.id.desc())
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if payment is not None and payment.status != PaymentStatus.APPROVED:
        payment.status = PaymentStatus.APPROVED
        payment.paid_at = now

    if order.status not in {OrderStatus.PAID, OrderStatus.QUEUED, OrderStatus.RENDERING, OrderStatus.READY, OrderStatus.DELIVERED}:
        order.status = OrderStatus.PAID
        order.paid_at = now
        await session.flush()
        await batch_collector.attach_paid_order(session, order.id)

    await session.commit()
    return {"order_id": order_id, "status": order.status.value}


# ── Batches ────────────────────────────────────────────────────────────────


@router.get("/batches")
async def list_batches(
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (
        await session.execute(select(Batch).order_by(desc(Batch.id)).limit(limit))
    ).scalars().all()
    return [
        {
            "id": b.id,
            "status": b.status.value,
            "trigger": b.trigger.value if b.trigger else None,
            "pod_id": b.pod_id,
            "pod_endpoint": b.pod_endpoint,
            "order_count": b.order_count,
            "started_at": b.started_at,
            "finished_at": b.finished_at,
            "error": b.error,
        }
        for b in rows
    ]


@router.post("/batches/{batch_id}/run")
async def run_batch(batch_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    batch = (await session.execute(select(Batch).where(Batch.id == batch_id))).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if batch.status != BatchStatus.COLLECTING:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"batch is {batch.status.value}")
    await batch_collector.mark_batch_starting(session, batch, BatchTrigger.MANUAL)
    await session.commit()
    # Enqueue the worker (arq) — done here so admin doesn't need to wait.
    from app.workers.queue import enqueue_run_batch

    await enqueue_run_batch(batch.id)
    return {"id": batch.id, "status": batch.status.value}


@router.post("/batches/{batch_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_batch(batch_id: int, session: AsyncSession = Depends(get_session)) -> None:
    batch = (await session.execute(select(Batch).where(Batch.id == batch_id))).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    batch.status = BatchStatus.FAILED
    batch.error = "cancelled by admin"
    batch.finished_at = datetime.now(UTC)
    await session.commit()
