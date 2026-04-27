"""Orders router: draft → items → checkout → status polling."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import GUEST_COOKIE_NAME, get_current_user_id, get_guest_order_ids
from app.core.security import sign_guest_orders, sign_media_token, verify_guest_orders
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
    CardCheckoutIn,
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
    guest_order_ids: list[int],
    guest_token: str | None = None,
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
    if order.id in guest_order_ids:
        return order
    # Fallback: verify X-Guest-Token header (avoids third-party cookie blocking)
    if guest_token:
        token_ids = verify_guest_orders(guest_token)
        if order.id in token_ids:
            return order
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not your order")


def _to_out(order: Order, *, guest_token: str | None = None) -> OrderOut:
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
        quality=order.quality,
        items=[_item_to_out(i) for i in sorted(order.items, key=lambda x: x.sequence)],
        created_at=order.created_at,
        paid_at=order.paid_at,
        delivered_at=order.delivered_at,
        guest_token=guest_token,
    )


def _item_to_out(item: OrderItem) -> OrderItemOut:
    settings = get_settings()
    api_base = settings.api_base_url.rstrip("/")
    video_url = None
    thumb_url = None
    if item.video_s3_key:
        token = sign_media_token(item.id)
        video_url = f"{api_base}/api/v1/media/{token}"
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


# ── Public lookup ──────────────────────────────────────────────────────────


@router.get("/lookup", response_model=list[OrderOut])
async def lookup_orders_by_contact(
    phone: str | None = None,
    email: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[OrderOut]:
    """Public endpoint: look up READY/DELIVERED orders by phone or email.

    Returns orders that have at least one READY item so the user can watch
    their videos. No auth required — contact info is the "password".
    """
    if not phone and not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="phone or email required")

    from sqlalchemy import or_, func as safunc
    conditions = []
    if phone:
        # Strip everything except digits for comparison (DB stores various formats)
        digits_only = "".join(c for c in phone if c.isdigit())
        # Match by stripping non-digits from the DB column too
        conditions.append(
            safunc.regexp_replace(Order.guest_phone, r"[^\d]", "", "g") == digits_only
        )
        conditions.append(Order.guest_phone == phone.strip())
    if email:
        conditions.append(Order.guest_email == email.strip().lower())

    orders = (
        await session.execute(
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.plan))
            .where(
                Order.status.in_([OrderStatus.READY, OrderStatus.DELIVERED]),
                or_(*conditions),
            )
            .order_by(Order.created_at.desc())
            .limit(20)
        )
    ).scalars().all()

    return [_to_out(o) for o in orders]


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    response: Response,
    user_id: int | None = Depends(get_current_user_id),
    existing_cookie: str | None = Cookie(default=None, alias=GUEST_COOKIE_NAME),
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
        utm_source=payload.utm_source,
        utm_medium=payload.utm_medium,
        utm_campaign=payload.utm_campaign,
        utm_content=payload.utm_content,
        utm_term=payload.utm_term,
        utm_sck=payload.utm_sck,
        utm_src=payload.utm_src,
    )
    session.add(order)
    await session.flush()

    # Maintain a list of all order IDs the guest has created in this browser.
    guest_token: str | None = None
    if user_id is None:
        prior_ids = verify_guest_orders(existing_cookie) if existing_cookie else []
        merged_ids = prior_ids + [order.id] if order.id not in prior_ids else prior_ids
        guest_token = sign_guest_orders(merged_ids)
        response.set_cookie(
            GUEST_COOKIE_NAME,
            guest_token,
            httponly=True,
            samesite="none",
            secure=True,
            max_age=60 * 60 * 24 * 30,
        )

    await session.commit()
    await session.refresh(order, attribute_names=["items", "plan", "quality"])
    # guest_token is also returned in the response body so the frontend can
    # pass it back as X-Guest-Token on fill/checkout — avoids third-party
    # cookie issues when the storefront HTML is on a different origin.
    return _to_out(order, guest_token=guest_token)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: int,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    x_guest_token: str | None = Header(default=None, alias="X-Guest-Token"),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_ids=guest_order_ids, guest_token=x_guest_token)
    return _to_out(order)


@router.put("/{order_id}", response_model=OrderOut)
async def update_order(
    order_id: int,
    payload: OrderUpdate,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_ids=guest_order_ids)
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
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    session: AsyncSession = Depends(get_session),
) -> OrderItemOut:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_ids=guest_order_ids)
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
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    session: AsyncSession = Depends(get_session),
) -> OrderItemOut:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_ids=guest_order_ids)
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
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    session: AsyncSession = Depends(get_session),
) -> Response:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_ids=guest_order_ids)
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
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    x_guest_token: str | None = Header(default=None, alias="X-Guest-Token"),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Replace all items with one item per character slug (up to plan.video_count).
    
    The frontend sends a flat list of character slugs (one per desired video).
    This endpoint clears existing DRAFT items and re-creates them so a single
    call is sufficient regardless of how many videos the plan includes.
    """
    order = await _load_order(session, order_id, user_id=user_id, guest_order_ids=guest_order_ids, guest_token=x_guest_token)
    _ensure_draft(order)

    slots = payload.video_slots[: order.plan.video_count]
    if len(slots) != order.plan.video_count:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"expected {order.plan.video_count} video slot(s)",
        )
    for slot in slots:
        if len(slot) == 0 or len(slot) > order.plan.max_characters_per_video:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"each slot needs 1–{order.plan.max_characters_per_video} character(s)",
            )

    # Pricing: base + 20% for HD + 10% per extra character (beyond first) per slot
    base = order.plan.price_cents
    total_extra_chars = sum(max(0, len(s) - 1) for s in slots)
    total_cents = round(
        base * (1 + (0.20 if payload.quality == "hd" else 0) + 0.10 * total_extra_chars)
    )
    order.total_cents = total_cents
    order.quality = payload.quality

    # Clear existing items
    for item in list(order.items):
        await session.delete(item)
    await session.flush()

    for seq, slot in enumerate(slots, start=1):
        vi = seq - 1
        msg = None
        if payload.custom_messages and vi < len(payload.custom_messages):
            msg = payload.custom_messages[vi]
        if msg is None:
            msg = payload.custom_message
        # Encode per-video child info as a JSON prefix so the AI worker can use it
        child_name = (payload.recipient_names[vi] if payload.recipient_names and vi < len(payload.recipient_names) else None) or None
        child_age  = (payload.recipient_ages[vi]  if payload.recipient_ages  and vi < len(payload.recipient_ages)  else None) or None
        child_occ  = (payload.occasion_slugs[vi]  if payload.occasion_slugs  and vi < len(payload.occasion_slugs)  else None) or None
        if any([child_name, child_age, child_occ]):
            import json as _json
            prefix = _json.dumps({"_child": {"name": child_name, "age": child_age, "occ": child_occ}}, ensure_ascii=False)
            msg = prefix + "\n" + (msg or "")
        session.add(
            OrderItem(
                order_id=order.id,
                sequence=seq,
                character_ids=list(slot),
                custom_message=msg,
            )
        )

    await session.commit()
    await session.refresh(order, attribute_names=["items", "plan"])
    return _to_out(order)


@router.post("/{order_id}/checkout", response_model=OrderCheckoutOut)
async def checkout(
    order_id: int,
    card: CardCheckoutIn | None = None,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    x_guest_token: str | None = Header(default=None, alias="X-Guest-Token"),
    session: AsyncSession = Depends(get_session),
) -> OrderCheckoutOut:
    settings = get_settings()
    order = await _load_order(session, order_id, user_id=user_id, guest_order_ids=guest_order_ids, guest_token=x_guest_token)

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
                qr_code_url="",
                ticket_url=existing.ticket_url,
                expires_at=existing.expires_at,
                amount_cents=existing.amount_cents,
            )
        else:
            # Failed prior attempt (no QR code) — delete it so we can retry.
            await session.delete(existing)
            await session.flush()

    notification_url = f"{settings.api_base_url.rstrip('/')}/api/v1/payments/mercadopago/webhook"

    # ── Card payment (credit or debit) ──────────────────────────────────────
    if card is not None:
        card_result = await mercadopago_client.create_card_payment(
            order_id=order.id,
            amount_cents=order.total_cents,
            description=f"Persona — pedido #{order.id}",
            notification_url=notification_url,
            card_token=card.card_token,
            installments=card.installments,
            payment_method_id=card.payment_method_id,
            payer_email=order.guest_email,
            payer_doc_type=card.payer_doc_type,
            payer_doc_number=card.payer_doc_number,
        )
        payment_type = "debit_card" if card.payment_method_id.startswith("deb") else "credit_card"
        payment = Payment(
            order_id=order.id,
            provider="mercadopago",
            provider_id=card_result["payment_id"],
            status=PaymentStatus.PENDING,
            amount_cents=order.total_cents,
            qr_code_payload=None,
            qr_code_s3_key=None,
            ticket_url=card_result.get("ticket_url"),
            expires_at=None,
        )
        session.add(payment)
        order.status = OrderStatus.AWAITING_PAYMENT
        # If immediately approved, mark paid
        if card_result["status"] == "approved":
            order.status = OrderStatus.PAID
            order.paid_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(payment)
        return OrderCheckoutOut(
            order_id=order.id,
            payment_id=payment.id,
            payment_method=payment_type,
            ticket_url=card_result.get("ticket_url"),
            expires_at=None,
            amount_cents=order.total_cents,
            card_status=card_result["status"],
            card_status_detail=card_result["status_detail"],
        )

    # ── PIX payment ──────────────────────────────────────────────────────────
    pix = await mercadopago_client.create_pix_payment(
        order_id=order.id,
        amount_cents=order.total_cents,
        description=f"Persona — pedido #{order.id}",
        notification_url=notification_url,
        payer_email=order.guest_email,
    )

    payment = Payment(
        order_id=order.id,
        provider="mercadopago",
        provider_id=pix["payment_id"],
        status=PaymentStatus.PENDING,
        amount_cents=order.total_cents,
        qr_code_payload=pix["qr_code"],
        qr_code_s3_key=None,
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
        payment_method="pix",
        qr_code_payload=pix["qr_code"],
        qr_code_base64=pix["qr_code_base64"] or None,
        qr_code_url="",
        ticket_url=pix["ticket_url"] or None,
        expires_at=pix["expires_at"],
        amount_cents=order.total_cents,
    )


@router.get("/{order_id}/items/{item_id}/download")
async def download_item(
    order_id: int,
    item_id: int,
    user_id: int | None = Depends(get_current_user_id),
    guest_order_ids: list[int] = Depends(get_guest_order_ids),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    order = await _load_order(session, order_id, user_id=user_id, guest_order_ids=guest_order_ids)
    item = next((i for i in order.items if i.id == item_id), None)
    if item is None or not item.video_s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="video not ready")
    return {"url": storage.presigned_get_url(item.video_s3_key, expires_in=3600)}
