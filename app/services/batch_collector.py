"""Batch collector — promotes PAID orders into the open ``COLLECTING`` batch
and triggers a run when policy thresholds are met.

The trigger logic is intentionally idempotent so it can be invoked from many
places (payment webhook, periodic cron, admin button) without risking
double-runs. Postgres advisory locks guard the critical section.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import (
    Batch,
    BatchItem,
    BatchStatus,
    BatchTrigger,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
)

ADVISORY_LOCK_KEY = 0xC0FFEE_BA7C  # arbitrary, scoped to storefront


async def _open_batch(session: AsyncSession) -> Batch:
    stmt = select(Batch).where(Batch.status == BatchStatus.COLLECTING).order_by(Batch.id.desc())
    batch = (await session.execute(stmt)).scalar_one_or_none()
    if batch is None:
        batch = Batch(status=BatchStatus.COLLECTING, order_count=0)
        session.add(batch)
        await session.flush()
    return batch


async def attach_paid_order(session: AsyncSession, order_id: int) -> Batch:
    """Add every PENDING item of the given paid order to the open batch."""
    await session.execute(select(func.pg_advisory_xact_lock(ADVISORY_LOCK_KEY)))

    order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    if order.status not in {OrderStatus.PAID, OrderStatus.QUEUED}:
        # Only attach paid (or already-queued) orders.
        return await _open_batch(session)

    batch = await _open_batch(session)

    items = (
        await session.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id,
                OrderItem.status == OrderItemStatus.PENDING,
            )
        )
    ).scalars().all()

    for item in items:
        existing = (
            await session.execute(
                select(BatchItem).where(
                    BatchItem.batch_id == batch.id, BatchItem.order_item_id == item.id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(BatchItem(batch_id=batch.id, order_item_id=item.id))

    batch.order_count = (
        await session.execute(
            select(func.count(BatchItem.id)).where(BatchItem.batch_id == batch.id)
        )
    ).scalar_one()

    if order.status == OrderStatus.PAID:
        order.status = OrderStatus.QUEUED
    return batch


async def should_trigger(session: AsyncSession, batch: Batch) -> BatchTrigger | None:
    """Return the trigger reason if this batch should start running, else None."""
    settings = get_settings()
    if batch.status != BatchStatus.COLLECTING:
        return None
    if batch.order_count >= settings.batch_auto_threshold:
        return BatchTrigger.AUTO_THRESHOLD

    oldest = (
        await session.execute(
            select(func.min(Order.paid_at))
            .join(OrderItem, OrderItem.order_id == Order.id)
            .join(BatchItem, BatchItem.order_item_id == OrderItem.id)
            .where(BatchItem.batch_id == batch.id)
        )
    ).scalar_one_or_none()
    if oldest and oldest <= datetime.now(UTC) - timedelta(minutes=settings.batch_max_age_minutes):
        return BatchTrigger.AUTO_AGE
    return None


async def mark_batch_starting(
    session: AsyncSession, batch: Batch, trigger: BatchTrigger
) -> Batch:
    batch.status = BatchStatus.STARTING_POD
    batch.trigger = trigger
    batch.started_at = datetime.now(UTC)
    return batch
