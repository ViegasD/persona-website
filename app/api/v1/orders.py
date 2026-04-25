"""Orders router: draft → items → checkout → status polling."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import GUEST_COOKIE_NAME, get_current_user_id, get_guest_order_id
from app.core.security import sign_guest_order
from app.core.settings import get_settings
from app.db.models import (
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
)
from app.db.session import get_session
from app.schemas.v1 import (
    OrderCheckoutOut,
    OrderCreate,
    OrderFillIn,
    OrderItemCreate,
    OrderItemOut,
    OrderItemUpdate,
    OrderOut,
    OrderUpdate,
)
from app.services import mercadopago_client, storage

router = APIRouter(prefix="/orders", tags=["orders"])


# ── Helpers ────────────────────────────────────────────────────────────────


async def _load_order(
    session: AsyncSession,
    order_id: int,
    *,
    user_id: int | None,
    guest_order_id: int | None,
) -> Order:
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items), selectinload(Order.plan))
    )
    order = (await session.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="order not found")
    if order.user_id is not None and order.user_id == user_id:
        return order
    if guest_order_id == order.id:
        return order
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not your order")


def _to_out(order: Order) -> OrderOut:
    return OrderOut(
        id=order.id,
        status=order.status,
        plan_id=order.plan_id,
        recipient_name=order.recipient_name,
        recipient_age=order.recipient_age,
        occasion_slug=order.occasion_slug,
        guest_email=order.guest_email,
        guest_phone=order.guest_phone,
        total_cents=order.total_cents,
        items=[_item_to_out(i) for i in sorted(order.items, key=lambda x: x.sequence)],
        created_at=order.created_at,
        paid_at=order.paid_at,
        delivered_at=order.delivered_at,
    )


def _item_to_out(item: OrderItem) -> OrderItemOut:
    video_url = None
    thumb_url = None
    if item.video_s3_key:
        try:
            video_url = storage.presigned_get_url(item.video_s3_key, expires_in=24 * 3600)
        except Exception:
            video_url = None
    if item.thumbnail_s3_key:
        try:
            thumb_url = storage.presigned_get_url(item.thumbnail_s3_key, expires_in=24 * 3600)
        except Exception:
            thumb_url = None
    return OrderItemOut(
        id=item.id,
        sequence=item.sequence,
        character_ids=item.character_ids,
        custom_message=item.custom_message,
        status=item.status,
        video_url=video_url,
        thumbnail_url=thumb_url,
    )


def _ensure_draft(order: Order) -> None:
    if order.status != OrderStatus.DRAFT:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="order no longer editable")


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    response: Response,
    user_id: int | None = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    plan = (await session.execute(select(Plan).where(Plan.slug == payload.plan_id))).scalar_one_or_none()
    if plan is None or not plan.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="plan not found or inactive")

    order = Order(
        user_id=user_id,
        plan_id=plan.id,
        recipient_name=payload.recipient_name,
        recipient_age=payload.recipient_age,
        occasion_slug=payload.occasion_slug,
        guest_email=payload.guest_email,
        guest_phone=payload.guest_phone,
        total_cents=plan.price_cents,
        status=OrderStatus.DRAFT,
    )
    session.add(order)
    await session.flush()

    # Sign a guest cookie so anonymous browsers can return to /orders/{id}.
    if user_id is None:
        token = sign_guest_order(order.id)
        response.set_cookie(
            GUEST_COOKIE_NAME,
            token,
            httponly=True,
            samesite="none",
            secure=True,
            max_age=60 * 60 * 24 * 30,
        )

    await session.commit()
    await session.refresh(order, attribute_names=["items", "plan"])
    return _to_out(order)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: int,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_id: int | None = Depends(get_guest_order_id),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_id=guest_order_id)
    return _to_out(order)


@router.put("/{order_id}", response_model=OrderOut)
async def update_order(
    order_id: int,
    payload: OrderUpdate,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_id: int | None = Depends(get_guest_order_id),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_id=guest_order_id)
    _ensure_draft(order)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(order, key, value)
    await session.commit()
    await session.refresh(order, attribute_names=["items", "plan"])
    return _to_out(order)


@router.post("/{order_id}/items", response_model=OrderItemOut, status_code=status.HTTP_201_CREATED)
async def add_item(
    order_id: int,
    payload: OrderItemCreate,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_id: int | None = Depends(get_guest_order_id),
    session: AsyncSession = Depends(get_session),
) -> OrderItemOut:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_id=guest_order_id)
    _ensure_draft(order)

    if len(payload.character_ids) > order.plan.max_characters_per_video:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"plan allows at most {order.plan.max_characters_per_video} characters per video",
        )
    if len(order.items) >= order.plan.video_count:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="plan video count reached")

    item = OrderItem(
        order_id=order.id,
        sequence=len(order.items) + 1,
        character_ids=list(payload.character_ids),
        custom_message=payload.custom_message,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _item_to_out(item)


@router.put("/{order_id}/items/{item_id}", response_model=OrderItemOut)
async def update_item(
    order_id: int,
    item_id: int,
    payload: OrderItemUpdate,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_id: int | None = Depends(get_guest_order_id),
    session: AsyncSession = Depends(get_session),
) -> OrderItemOut:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_id=guest_order_id)
    _ensure_draft(order)

    item = next((i for i in order.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="item not found")

    data = payload.model_dump(exclude_unset=True)
    if "character_ids" in data and data["character_ids"]:
        if len(data["character_ids"]) > order.plan.max_characters_per_video:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="too many characters for this plan",
            )
        item.character_ids = data["character_ids"]
    if "custom_message" in data:
        item.custom_message = data["custom_message"]
    await session.commit()
    await session.refresh(item)
    return _item_to_out(item)


@router.delete("/{order_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    order_id: int,
    item_id: int,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_id: int | None = Depends(get_guest_order_id),
    session: AsyncSession = Depends(get_session),
) -> Response:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_id=guest_order_id)
    _ensure_draft(order)
    item = next((i for i in order.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="item not found")
    await session.delete(item)
    # Re-sequence remaining items so positions stay 1..N contiguous.
    remaining = sorted([i for i in order.items if i.id != item_id], key=lambda x: x.sequence)
    for idx, it in enumerate(remaining, start=1):
        it.sequence = idx
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{order_id}/fill", response_model=OrderOut)
async def fill_items(
    order_id: int,
    payload: OrderFillIn,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_id: int | None = Depends(get_guest_order_id),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Replace all items with one item per character slug (up to plan.video_count).
    
    The frontend sends a flat list of character slugs (one per desired video).
    This endpoint clears existing DRAFT items and re-creates them so a single
    call is sufficient regardless of how many videos the plan includes.
    """
    order = await _load_order(session, order_id, user_id=user_id, guest_order_id=guest_order_id)
    _ensure_draft(order)

    slugs = payload.character_slugs[: order.plan.video_count]
    if not slugs:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="at least one character slug required")

    # Clear existing items
    for item in list(order.items):
        await session.delete(item)
    await session.flush()

    for seq, slug in enumerate(slugs, start=1):
        session.add(
            OrderItem(
                order_id=order.id,
                sequence=seq,
                character_ids=[slug],
                custom_message=payload.custom_message,
            )
        )

    await session.commit()
    await session.refresh(order, attribute_names=["items", "plan"])
    return _to_out(order)


@router.post("/{order_id}/checkout", response_model=OrderCheckoutOut)
async def checkout(
    order_id: int,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_id: int | None = Depends(get_guest_order_id),
    session: AsyncSession = Depends(get_session),
) -> OrderCheckoutOut:
    settings = get_settings()
    order = await _load_order(session, order_id, user_id=user_id, guest_order_id=guest_order_id)

    if order.status not in {OrderStatus.DRAFT, OrderStatus.AWAITING_PAYMENT}:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"cannot checkout in status {order.status}")
    if len(order.items) != order.plan.video_count:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"order needs exactly {order.plan.video_count} items (has {len(order.items)})",
        )

    # Reuse the most recent open Pix payment if present.
    existing = (
        await session.execute(
            select(Payment)
            .where(Payment.order_id == order.id, Payment.status == PaymentStatus.PENDING)
            .order_by(Payment.id.desc())
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.qr_code_payload:
            # Valid prior attempt — return it directly.
            return OrderCheckoutOut(
                order_id=order.id,
                payment_id=existing.id,
                qr_code_payload=existing.qr_code_payload,
                qr_code_base64=None,
                qr_code_url=storage.presigned_get_url(cast(str, existing.qr_code_s3_key), 1800)
                if existing.qr_code_s3_key
                else "",
                ticket_url=existing.ticket_url,
                expires_at=existing.expires_at,
                amount_cents=existing.amount_cents,
            )
        else:
            # Failed prior attempt (no QR code) — delete it so we can retry.
            await session.delete(existing)
            await session.flush()

    notification_url = f"{settings.api_base_url.rstrip('/')}/api/v1/payments/mercadopago/webhook"
    pix = await mercadopago_client.create_pix_payment(
        order_id=order.id,
        amount_cents=order.total_cents,
        description=f"Persona — pedido #{order.id}",
        notification_url=notification_url,
        payer_email=order.guest_email,
    )

    qr_png = mercadopago_client.render_qr_png(pix["qr_code"])
    qr_key = storage.storefront_key("orders", str(order.id), "pix.png")
    qr_url = ""
    try:
        storage.upload_bytes(qr_key, qr_png, content_type="image/png")
        qr_url = storage.presigned_get_url(qr_key, 1800)
    except Exception:
        qr_key = None  # type: ignore[assignment]

    payment = Payment(
        order_id=order.id,
        provider="mercadopago",
        provider_id=pix["payment_id"],
        status=PaymentStatus.PENDING,
        amount_cents=order.total_cents,
        qr_code_payload=pix["qr_code"],
        qr_code_s3_key=qr_key,
        ticket_url=pix["ticket_url"] or None,
        expires_at=pix["expires_at"],
    )
    session.add(payment)
    order.status = OrderStatus.AWAITING_PAYMENT
    await session.commit()
    await session.refresh(payment)

    return OrderCheckoutOut(
        order_id=order.id,
        payment_id=payment.id,
        qr_code_payload=pix["qr_code"],
        qr_code_base64=pix["qr_code_base64"] or None,
        qr_code_url=qr_url,
        ticket_url=pix["ticket_url"] or None,
        expires_at=pix["expires_at"],
        amount_cents=order.total_cents,
    )


@router.get("/{order_id}/items/{item_id}/download")
async def download_item(
    order_id: int,
    item_id: int,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_id: int | None = Depends(get_guest_order_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_id=guest_order_id)
    item = next((i for i in order.items if i.id == item_id), None)
    if item is None or not item.video_s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="video not ready")
    return {"url": storage.presigned_get_url(item.video_s3_key, expires_in=3600)}
