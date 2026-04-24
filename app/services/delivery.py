"""Delivery: account download links + WhatsApp send via Evolution API."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.services.storage import presigned_get_url


async def deliver_order(session: AsyncSession, order_id: int) -> list[Delivery]:
    """Idempotently produce the deliveries for a READY/DELIVERED order.

    - Always records an ``account`` delivery with signed URLs (they are
      regenerated on demand from the download endpoint anyway).
    - If a phone is on file, fires a WhatsApp send via Evolution API.
    """
    order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    if order.status not in {OrderStatus.READY, OrderStatus.DELIVERED}:
        return []

    items = (
        await session.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id, OrderItem.status == OrderItemStatus.READY
            )
        )
    ).scalars().all()

    deliveries: list[Delivery] = []

    # Account channel — always.
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
    if phone and settings.evolution_api_url and settings.evolution_api_key:
        wa = Delivery(
            order_id=order.id,
            channel=DeliveryChannel.WHATSAPP,
            status=DeliveryStatus.PENDING,
            target=phone,
            attempted_at=datetime.now(UTC),
        )
        session.add(wa)
        try:
            await _send_whatsapp_videos(phone, items)
            wa.status = DeliveryStatus.SUCCEEDED
            wa.succeeded_at = datetime.now(UTC)
        except Exception as exc:  # noqa: BLE001 - we want any failure recorded
            wa.status = DeliveryStatus.FAILED
            wa.error = str(exc)[:500]
        deliveries.append(wa)

    if any(d.status == DeliveryStatus.SUCCEEDED for d in deliveries):
        order.status = OrderStatus.DELIVERED
        order.delivered_at = datetime.now(UTC)
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
