"""Affiliate management endpoints (admin-gated)."""

from __future__ import annotations

import random
import string
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin_key
from app.db.models import Affiliate, Order, OrderStatus
from app.db.session import get_session
from app.schemas.v1 import AffiliateCreate, AffiliateOrderOut, AffiliateOut, AffiliateUpdate

router = APIRouter(
    prefix="/admin/affiliates",
    tags=["affiliates"],
    dependencies=[Depends(require_admin_key)],
)

_ALPHA = string.ascii_uppercase + string.digits
_PAID_STATUSES = {
    OrderStatus.PAID,
    OrderStatus.QUEUED,
    OrderStatus.GENERATING,
    OrderStatus.READY,
    OrderStatus.DELIVERED,
}


def _generate_code(length: int = 8) -> str:
    return "".join(random.choices(_ALPHA, k=length))


def _commission_cents(total_cents: int, pct: Decimal) -> int:
    return round(total_cents * float(pct) / 100)


async def _unique_code(session: AsyncSession) -> str:
    for _ in range(10):
        code = _generate_code()
        exists = (
            await session.execute(select(Affiliate).where(Affiliate.code == code))
        ).scalar_one_or_none()
        if exists is None:
            return code
    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not generate unique code"
    )


def _to_out(affiliate: Affiliate, orders: list[Order]) -> AffiliateOut:
    paid = [o for o in orders if o.status in _PAID_STATUSES]
    pct = Decimal(str(affiliate.commission_pct))
    total_earnings = sum(_commission_cents(o.total_cents, pct) for o in paid)
    unpaid_earnings = sum(
        _commission_cents(o.total_cents, pct) for o in paid if not o.commission_paid_out
    )
    out = AffiliateOut.model_validate(affiliate)
    out.total_orders = len(paid)
    out.total_earnings_cents = total_earnings
    out.unpaid_earnings_cents = unpaid_earnings
    return out


def _order_to_out(order: Order, affiliate: Affiliate) -> AffiliateOrderOut:
    pct = Decimal(str(affiliate.commission_pct))
    return AffiliateOrderOut(
        order_id=order.id,
        recipient_name=order.recipient_name,
        guest_phone=order.guest_phone,
        guest_email=order.guest_email,
        plan_id=order.plan_id,
        total_cents=order.total_cents,
        commission_pct=pct,
        commission_cents=_commission_cents(order.total_cents, pct),
        commission_paid_out=order.commission_paid_out,
        commission_paid_out_at=order.commission_paid_out_at,
        paid_at=order.paid_at,
        created_at=order.created_at,
    )


# ── Create ─────────────────────────────────────────────────────────────────


@router.post("", response_model=AffiliateOut, status_code=status.HTTP_201_CREATED)
async def create_affiliate(
    payload: AffiliateCreate,
    session: AsyncSession = Depends(get_session),
) -> AffiliateOut:
    code = await _unique_code(session)
    affiliate = Affiliate(
        code=code,
        name=payload.name,
        email=payload.email,
        commission_pct=payload.commission_pct,
    )
    session.add(affiliate)
    await session.commit()
    await session.refresh(affiliate)
    return _to_out(affiliate, [])


# ── List ───────────────────────────────────────────────────────────────────


@router.get("", response_model=list[AffiliateOut])
async def list_affiliates(
    session: AsyncSession = Depends(get_session),
) -> list[AffiliateOut]:
    affiliates = (
        await session.execute(select(Affiliate).order_by(Affiliate.created_at.desc()))
    ).scalars().all()
    result = []
    for aff in affiliates:
        orders = (
            await session.execute(select(Order).where(Order.affiliate_id == aff.id))
        ).scalars().all()
        result.append(_to_out(aff, list(orders)))
    return result


# ── Update ─────────────────────────────────────────────────────────────────


@router.patch("/{affiliate_id}", response_model=AffiliateOut)
async def update_affiliate(
    affiliate_id: int,
    payload: AffiliateUpdate,
    session: AsyncSession = Depends(get_session),
) -> AffiliateOut:
    affiliate = (
        await session.execute(select(Affiliate).where(Affiliate.id == affiliate_id))
    ).scalar_one_or_none()
    if affiliate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="affiliate not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(affiliate, k, v)
    await session.commit()
    await session.refresh(affiliate)
    orders = (
        await session.execute(select(Order).where(Order.affiliate_id == affiliate.id))
    ).scalars().all()
    return _to_out(affiliate, list(orders))


# ── Orders for an affiliate ────────────────────────────────────────────────


@router.get("/{affiliate_id}/orders", response_model=list[AffiliateOrderOut])
async def list_affiliate_orders(
    affiliate_id: int,
    session: AsyncSession = Depends(get_session),
) -> list[AffiliateOrderOut]:
    affiliate = (
        await session.execute(select(Affiliate).where(Affiliate.id == affiliate_id))
    ).scalar_one_or_none()
    if affiliate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="affiliate not found")
    orders = (
        await session.execute(
            select(Order)
            .where(
                Order.affiliate_id == affiliate_id,
                Order.status.in_(list(_PAID_STATUSES)),
            )
            .order_by(Order.created_at.desc())
        )
    ).scalars().all()
    return [_order_to_out(o, affiliate) for o in orders]


# ── Mark commission paid ───────────────────────────────────────────────────


@router.post("/{affiliate_id}/orders/{order_id}/mark-paid", response_model=AffiliateOrderOut)
async def mark_commission_paid(
    affiliate_id: int,
    order_id: int,
    session: AsyncSession = Depends(get_session),
) -> AffiliateOrderOut:
    affiliate = (
        await session.execute(select(Affiliate).where(Affiliate.id == affiliate_id))
    ).scalar_one_or_none()
    if affiliate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="affiliate not found")
    order = (
        await session.execute(
            select(Order).where(Order.id == order_id, Order.affiliate_id == affiliate_id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="order not found for this affiliate"
        )
    order.commission_paid_out = True
    order.commission_paid_out_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(order)
    return _order_to_out(order, affiliate)
