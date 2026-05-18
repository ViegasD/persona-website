"""Admin endpoints (gated by ``X-Api-Key``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_admin_key
from app.core.security import sign_media_token
from app.db.models import (
    ApiCostLog,
    Batch,
    BatchStatus,
    BatchTrigger,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
)
from app.db.session import get_session
from app.schemas.v1 import OrderOut, PlanCreate, PlanOut, PlanUpdate
from app.services import batch_collector, storage

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


@router.post("/orders/{order_id}/reprocess", status_code=status.HTTP_200_OK)
async def reprocess_order(order_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Re-enqueue stuck items OR advance order status if all items are already READY."""
    order = (
        await session.execute(
            select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if order.status not in {OrderStatus.PAID, OrderStatus.QUEUED}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"order is {order.status.value}, expected PAID or QUEUED",
        )

    # If all items are already READY, the order just needs its status advanced.
    if order.items and all(i.status == OrderItemStatus.READY for i in order.items):
        order.status = OrderStatus.READY
        await session.commit()
        return {"order_id": order_id, "action": "marked_ready", "enqueued": [], "errors": []}

    from app.workers.queue import enqueue_process_item

    enqueued = []
    errors = []
    for item in order.items:
        if item.status in {OrderItemStatus.PENDING, OrderItemStatus.FAILED}:
            item.status = OrderItemStatus.PENDING
            item.error = None
            try:
                await session.flush()
                await enqueue_process_item(item.id)
                enqueued.append(item.id)
            except Exception as exc:
                errors.append({"item_id": item.id, "error": str(exc)})

    await session.commit()
    return {"order_id": order_id, "action": "requeued", "enqueued": enqueued, "errors": errors}


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

    if order.status not in {OrderStatus.PAID, OrderStatus.GENERATING, OrderStatus.READY, OrderStatus.DELIVERED}:
        order.status = OrderStatus.PAID
        order.paid_at = now
        await session.flush()
        # Enqueue per-item phase-1 processing immediately.
        from app.workers.queue import enqueue_process_item
        item_ids = [it.id for it in order.items]
        await session.commit()
        for item_id in item_ids:
            await enqueue_process_item(item_id)
        return {"order_id": order_id, "status": order.status.value}

    await session.commit()
    return {"order_id": order_id, "status": order.status.value}


# ── Item approval queue ────────────────────────────────────────────────────


@router.get("/items/pending-approval")
async def list_pending_approval(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List all order items currently awaiting admin approval."""
    items = (
        await session.execute(
            select(OrderItem)
            .options(selectinload(OrderItem.order))
            .where(OrderItem.status == OrderItemStatus.AWAITING_APPROVAL)
            .order_by(OrderItem.updated_at)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    from app.core.settings import get_settings
    api_base = get_settings().api_base_url.rstrip("/")

    result = []
    for item in items:
        order = item.order
        is_multi = len(item.character_ids) > 1
        has_preview = bool(item.video_s3_key or item.composite_image_s3_key)
        if has_preview:
            token = sign_media_token(item.id)
            preview_url = f"{api_base}/api/v1/media/{token}"
        else:
            preview_url = None

        result.append({
            "item_id": item.id,
            "order_id": item.order_id,
            "sequence": item.sequence,
            "character_ids": item.character_ids,
            "recipient_name": order.recipient_name,
            "recipient_age": order.recipient_age,
            "occasion_slug": order.occasion_slug,
            "is_multi_character": is_multi,
            "preview_type": "image" if is_multi else "video",
            "preview_url": preview_url,
            "updated_at": item.updated_at,
        })
    return result


@router.get("/items/{item_id}/preview")
async def preview_item(
    item_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Proxy the composite image or video from S3 so the browser never hits MinIO directly."""
    item = (
        await session.execute(select(OrderItem).where(OrderItem.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    is_multi = len(item.character_ids) > 1
    s3_key = item.composite_image_s3_key if is_multi else item.video_s3_key
    if not s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no preview available yet")

    data = storage.download_bytes(s3_key)
    content_type = "image/png" if is_multi else "video/mp4"
    return Response(content=data, media_type=content_type)


@router.post("/items/{item_id}/approve", status_code=status.HTTP_200_OK)
async def approve_item(item_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Approve a pending item: triggers video generation (multi-char) or delivery (single-char)."""
    item = (
        await session.execute(select(OrderItem).where(OrderItem.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if item.status != OrderItemStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"item is {item.status.value}, expected AWAITING_APPROVAL",
        )
    item.status = OrderItemStatus.APPROVED
    await session.commit()

    from app.workers.queue import enqueue_generate_video
    await enqueue_generate_video(item_id)
    return {"item_id": item_id, "status": item.status.value}


@router.post("/items/{item_id}/reject", status_code=status.HTTP_200_OK)
async def reject_item(item_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Reject a pending item (marks it FAILED so it can be retried or investigated)."""
    item = (
        await session.execute(select(OrderItem).where(OrderItem.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if item.status != OrderItemStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"item is {item.status.value}, expected AWAITING_APPROVAL",
        )
    item.status = OrderItemStatus.FAILED
    item.error = "rejected by admin"
    await session.commit()
    return {"item_id": item_id, "status": item.status.value}


@router.post("/items/{item_id}/retry", status_code=status.HTTP_200_OK)
async def retry_item(item_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    """Re-enqueue a FAILED item for phase-1 processing."""
    item = (
        await session.execute(select(OrderItem).where(OrderItem.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if item.status != OrderItemStatus.FAILED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"item is {item.status.value}, expected FAILED",
        )
    item.status = OrderItemStatus.PENDING
    item.error = None
    await session.commit()

    from app.workers.queue import enqueue_process_item
    await enqueue_process_item(item_id)
    return {"item_id": item_id, "status": item.status.value}


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


# ── Dashboard summary ──────────────────────────────────────────────────────


@router.get("/dashboard/summary")
async def dashboard_summary(session: AsyncSession = Depends(get_session)) -> dict:
    """Aggregate KPIs for the admin dashboard:
    - Revenue (total paid, today, this week, this month)
    - Order counts by status
    - API cost totals (all-time, today, this week, this month)
    - Conversion funnel (drafts vs awaiting_payment vs paid)
    """
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)

    # ── Revenue ────────────────────────────────────────────────────────────
    paid_statuses = {
        OrderStatus.PAID, OrderStatus.QUEUED, OrderStatus.GENERATING,
        OrderStatus.READY, OrderStatus.DELIVERED,
    }

    async def _revenue(since: datetime | None = None) -> int:
        stmt = select(func.coalesce(func.sum(Order.total_cents), 0)).where(
            Order.status.in_(paid_statuses)
        )
        if since:
            stmt = stmt.where(Order.paid_at >= since)
        return (await session.execute(stmt)).scalar_one()

    revenue_total = await _revenue()
    revenue_today = await _revenue(today_start)
    revenue_week = await _revenue(week_start)
    revenue_month = await _revenue(month_start)

    # ── Order counts ───────────────────────────────────────────────────────
    counts_rows = (
        await session.execute(
            select(Order.status, func.count(Order.id))
            .group_by(Order.status)
        )
    ).all()
    order_counts = {row[0].value: row[1] for row in counts_rows}

    # ── API costs ──────────────────────────────────────────────────────────
    async def _costs(since: datetime | None = None) -> dict[str, int]:
        stmt = select(
            ApiCostLog.provider,
            func.coalesce(func.sum(ApiCostLog.cost_micro_usd), 0),
        ).group_by(ApiCostLog.provider)
        if since:
            stmt = stmt.where(ApiCostLog.created_at >= since)
        rows = (await session.execute(stmt)).all()
        return {row[0]: row[1] for row in rows}

    costs_total = await _costs()
    costs_today = await _costs(today_start)
    costs_week = await _costs(week_start)
    costs_month = await _costs(month_start)

    total_paid_orders = sum(
        order_counts.get(s.value, 0) for s in paid_statuses
    )
    total_orders = sum(order_counts.values())

    return {
        "revenue_cents": {
            "total": revenue_total,
            "today": revenue_today,
            "week": revenue_week,
            "month": revenue_month,
        },
        "order_counts": order_counts,
        "conversion": {
            "total_orders": total_orders,
            "paid_orders": total_paid_orders,
            "rate_pct": round(100 * total_paid_orders / total_orders, 1) if total_orders else 0,
        },
        "api_cost_micro_usd": {
            "total": costs_total,
            "today": costs_today,
            "week": costs_week,
            "month": costs_month,
        },
    }


@router.get("/dashboard/orders")
async def dashboard_orders(
    status_filter: OrderStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Paginated orders list with plan and item info for the purchases page."""
    stmt = (
        select(Order)
        .options(selectinload(Order.plan), selectinload(Order.items))
        .order_by(desc(Order.created_at))
        .limit(limit)
        .offset(offset)
    )
    if status_filter is not None:
        stmt = stmt.where(Order.status == status_filter)

    count_stmt = select(func.count(Order.id))
    if status_filter is not None:
        count_stmt = count_stmt.where(Order.status == status_filter)

    orders, total = await session.execute(stmt), (await session.execute(count_stmt)).scalar_one()
    orders = orders.scalars().all()

    return {
        "total": total,
        "items": [
            {
                "id": o.id,
                "status": o.status.value,
                "plan": o.plan.name if o.plan else None,
                "plan_slug": o.plan.slug if o.plan else None,
                "guest_phone": o.guest_phone,
                "guest_email": o.guest_email,
                "recipient_name": o.recipient_name,
                "total_cents": o.total_cents,
                "quality": o.quality,
                "video_count": len(o.items),
                "created_at": o.created_at,
                "paid_at": o.paid_at,
                "delivered_at": o.delivered_at,
                "error": o.error,
            }
            for o in orders
        ],
    }


@router.get("/dashboard/api-costs")
async def dashboard_api_costs(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Daily API cost breakdown by provider for the last N days."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                func.date_trunc("day", ApiCostLog.created_at).label("day"),
                ApiCostLog.provider,
                func.sum(ApiCostLog.cost_micro_usd).label("cost"),
                func.count(ApiCostLog.id).label("calls"),
            )
            .where(ApiCostLog.created_at >= since)
            .group_by("day", ApiCostLog.provider)
            .order_by("day", ApiCostLog.provider)
        )
    ).all()
    return [
        {
            "day": row.day.date().isoformat(),
            "provider": row.provider,
            "cost_micro_usd": row.cost,
            "calls": row.calls,
        }
        for row in rows
    ]
