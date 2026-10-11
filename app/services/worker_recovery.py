"""Recover interrupted jobs after the worker's one-hour execution deadline."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.db.models import Order, OrderItem, OrderItemStatus, OrderStatus
from app.db.session import session_scope
from app.services.outbox import record_delivery_job, record_video_jobs


async def recover_interrupted_jobs(ctx: dict | None = None) -> None:
    cutoff = datetime.now(UTC) - timedelta(minutes=75)
    async with session_scope() as session:
        orders = (await session.execute(select(Order).where(
            Order.status.in_([OrderStatus.PAID, OrderStatus.QUEUED, OrderStatus.GENERATING]),
            select(OrderItem.id).where(OrderItem.order_id == Order.id, OrderItem.updated_at < cutoff,
                OrderItem.status.in_([OrderItemStatus.PENDING, OrderItemStatus.COMPOSITING,
                    OrderItemStatus.RENDERING, OrderItemStatus.APPROVED, OrderItemStatus.READY])).exists(),
        ).order_by(Order.id).limit(200).with_for_update(skip_locked=True))).scalars().all()
        for order in orders:
            all_items = (await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
            if all_items and all(item.status == OrderItemStatus.READY for item in all_items):
                order.status = OrderStatus.READY
                order.generated_at = datetime.now(UTC)
                await record_delivery_job(session, order)
                continue
            items = (await session.execute(select(OrderItem).where(
                OrderItem.order_id == order.id, OrderItem.updated_at < cutoff,
                OrderItem.status.in_([OrderItemStatus.PENDING, OrderItemStatus.COMPOSITING,
                                     OrderItemStatus.RENDERING, OrderItemStatus.APPROVED]),
            ).with_for_update(skip_locked=True))).scalars().all()
            for item in items:
                if item.attempts >= 5:
                    item.status = OrderItemStatus.FAILED
                    item.error = "automatic recovery limit reached; admin retry required"
                    continue
                has_output = bool(item.video_s3_key or item.composite_image_s3_key)
                item.status = OrderItemStatus.APPROVED if has_output else OrderItemStatus.PENDING
                item.updated_at = datetime.now(UTC)
                await record_video_jobs(session, order, [item.id],
                                        kind="generate_video" if has_output else "process_item",
                                        generation=f"recovery:{uuid4().hex}")
        await session.commit()
