"""Commit delivery intent with business data; retry network delivery separately."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import String, and_, cast, false, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import AccountToken, Order, OrderItem, OrderItemStatus, OrderStatus, OutboxJob
from app.db.session import session_scope
from app.services.account_mail import send_account_email
from app.services.processing_policy import processing_allowed, processing_enabled
from app.workers.queue import get_pool


async def record_video_jobs(session: AsyncSession, order: Order, item_ids: list[int], kind: str = "process_item", generation: str = "") -> None:
    for item_id in item_ids:
        await session.execute(insert(OutboxJob).values(
            dedup_key=f"{kind}:{item_id}" + (f":{generation}" if generation else ""), kind=kind, order_id=order.id,
            payload={"item_id": item_id},
        ).on_conflict_do_nothing(index_elements=[OutboxJob.dedup_key]))


async def record_delivery_job(session: AsyncSession, order: Order) -> None:
    await session.execute(insert(OutboxJob).values(
        dedup_key=f"deliver_order:{order.id}", kind="deliver_order", order_id=order.id,
        payload={},
    ).on_conflict_do_nothing(index_elements=[OutboxJob.dedup_key]))


async def deliver_outbox_job(session: AsyncSession, job: OutboxJob) -> None:
    if job.kind != "account_email":
        if not processing_enabled():
            return
        order = await session.get(Order, job.order_id)
        if order is None or not processing_allowed(order):
            return
    if job.kind == "account_email":
        await send_account_email(job.payload)
        job.payload = {}  # Remove the raw email link after successful delivery.
    elif job.kind == "deliver_order":
        from app.services.delivery import deliver_order
        records = await deliver_order(session, job.order_id)
        if not records:
            raise RuntimeError("order is not ready for website delivery")
    elif job.kind in {"process_item", "generate_video"}:
        item = (await session.execute(select(OrderItem).join(Order).where(
            OrderItem.id == job.payload["item_id"],
            Order.status.in_([OrderStatus.PAID, OrderStatus.QUEUED, OrderStatus.GENERATING]),
            OrderItem.status.in_([OrderItemStatus.PENDING] if job.kind == "process_item" else [
                OrderItemStatus.COMPOSITING, OrderItemStatus.AWAITING_APPROVAL, OrderItemStatus.APPROVED,
            ]),
        ))).scalar_one_or_none()
        if item is not None:
            pool = await get_pool()
            function = "process_item_phase1" if job.kind == "process_item" else "generate_video_for_approved_item"
            await pool.enqueue_job(function, item.id, _job_id=f"outbox:{job.id}")
    else:
        raise ValueError("unknown outbox job kind")
    job.delivered_at = datetime.now(UTC)
    job.last_error = None


def active_account_mail(now: datetime):
    # Leave obsolete mail untouched instead of sending expired or replaced links.
    return and_(OutboxJob.kind == "account_email", select(AccountToken.id).where(
        OutboxJob.dedup_key == "account_email:" + cast(AccountToken.id, String),
        AccountToken.user_id == OutboxJob.user_id,
        AccountToken.used_at.is_(None), AccountToken.expires_at > now,
    ).exists())


async def dispatch_outbox(ctx: dict | None = None) -> None:
    eligible = false()
    if processing_enabled():
        eligible = and_(OutboxJob.kind.in_(["process_item", "generate_video", "deliver_order"]),
                        select(Order.id).where(Order.id == OutboxJob.order_id,
                            Order.created_at >= get_settings().processing_start_at).exists())
    for _ in range(50):
        async with session_scope() as session:
            job = (await session.execute(select(OutboxJob).where(
                OutboxJob.delivered_at.is_(None), OutboxJob.available_at <= datetime.now(UTC),
                or_(active_account_mail(datetime.now(UTC)), eligible),
            ).order_by(OutboxJob.id).limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
            if job is None:
                break
            job.attempts += 1
            try:
                await deliver_outbox_job(session, job)
            except Exception as exc:
                # Do not persist exception text which could include email credentials.
                job.last_error = type(exc).__name__
                job.available_at = datetime.now(UTC) + timedelta(seconds=min(600, 2 ** min(job.attempts, 9)))
            await session.commit()
