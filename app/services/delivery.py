"""Delivery: account download links + WhatsApp send via Evolution API."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.settings import get_settings
from app.db.models import (
    Delivery,
    DeliveryChannel,
    DeliveryStatus,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
)
from app.services.processing_policy import processing_allowed, processing_enabled
from app.services.storage import presigned_get_url


async def deliver_order(session: AsyncSession, order_id: int) -> list[Delivery]:
    """Idempotently produce the deliveries for a READY/DELIVERED order.

    - Complete orders get one website delivery; links are generated on demand.
    - WhatsApp is an optional, explicitly enabled secondary channel.
    """
    if not processing_enabled():
        return []
    order = (await session.execute(select(Order).options(selectinload(Order.plan)).where(Order.id == order_id).with_for_update()
                                   .execution_options(populate_existing=True))).scalar_one()
    if not processing_allowed(order) or order.status not in {OrderStatus.READY, OrderStatus.DELIVERED}:
        return []

    items = (
        await session.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id
            )
        )
    ).scalars().all()

    if (order.plan.video_count <= 0 or len(items) != order.plan.video_count
            or any(item.status != OrderItemStatus.READY or not item.video_s3_key for item in items)):
        return []
    existing = (await session.execute(select(Delivery).where(Delivery.order_id == order.id))).scalars().all()
    deliveries: list[Delivery] = list(existing)

    # Account channel — always.
    account = next((d for d in existing if d.channel == DeliveryChannel.ACCOUNT
                    and d.status == DeliveryStatus.SUCCEEDED), None)
    if account is None:
        account = Delivery(
            order_id=order.id,
            channel=DeliveryChannel.ACCOUNT,
            status=DeliveryStatus.SUCCEEDED,
            target=order.guest_email,
            attempted_at=datetime.now(UTC),
            succeeded_at=datetime.now(UTC),
            payload={"video_count": len(items)},
        )
        session.add(account)
        deliveries.append(account)

    # WhatsApp channel.
    phone = order.guest_phone
    settings = get_settings()
    order.status = OrderStatus.DELIVERED
    order.delivered_at = order.delivered_at or datetime.now(UTC)
    if (settings.whatsapp_delivery_enabled and phone and settings.evolution_api_url
            and settings.evolution_api_key and settings.evolution_instance_name
            and not any(d.channel == DeliveryChannel.WHATSAPP for d in existing)):
        wa = Delivery(
            order_id=order.id,
            channel=DeliveryChannel.WHATSAPP,
            status=DeliveryStatus.PENDING,
            target=phone,
            attempted_at=datetime.now(UTC),
        )
        session.add(wa)
        # Persist an at-most-once notification claim before the external send.
        # A crash leaves PENDING for operator review, never an automatic resend.
        await session.commit()
        try:
            await _send_whatsapp_videos(phone, items)
            wa.status = DeliveryStatus.SUCCEEDED
            wa.succeeded_at = datetime.now(UTC)
        except Exception as exc:  # noqa: BLE001 - we want any failure recorded
            wa.status = DeliveryStatus.FAILED
            wa.error = type(exc).__name__
        deliveries.append(wa)

    return deliveries


async def _send_whatsapp_videos(phone: str, items: list[OrderItem]) -> None:
    settings = get_settings()
    base = settings.evolution_api_url.rstrip("/")
    inst = settings.evolution_instance_name
    if not inst:
        raise RuntimeError("EVOLUTION_INSTANCE_NAME not configured")
    url = f"{base}/message/sendMedia/{inst}"
    headers = {"apikey": settings.evolution_api_key or ""}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx, item in enumerate(items, start=1):
            if not item.video_s3_key:
                continue
            video_url = presigned_get_url(item.video_s3_key, expires_in=60 * 60 * 24)
            payload = {
                "number": phone,
                "options": {"delay": 1200},
                "mediaMessage": {
                    "mediatype": "video",
                    "media": video_url,
                    "caption": f"🎬 Vídeo {idx}",
                    "fileName": f"video-{idx}.mp4",
                },
            }
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
