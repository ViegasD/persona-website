"""Chat endpoint — stateful AI conversation for the web frontend.

POST /api/v1/chat/message
    Body: { "session_id": "...", "message": "...", "guest_phone": "..." }
    Returns: {
        "session_id": "...",
        "messages": ["bolha 1", "bolha 2"],
        "payment": null | {
            "order_id": 1,
            "payment_id": 1,
            "qr_code_payload": "...",
            "qr_code_base64": "...",
            "ticket_url": "...",
            "expires_at": "...",
            "amount_cents": 1990
        },
        "order_id": null | 1
    }

DELETE /api/v1/chat/session/{session_id}
    Clears conversation history and order state.

Context injection
-----------------
On each request we fetch the live catalog (plans + characters) from the DB
and inject it as a brief context block so the assistant always has up-to-date
pricing and character names without fine-tuning.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan
from app.db.session import get_session
from app.services.chat_service import ChatReply, chat_turn, clear_session

router = APIRouter(prefix="/chat", tags=["chat"])


# ── Schemas ────────────────────────────────────────────────────────────────


class ChatMessageIn(BaseModel):
    session_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Client-generated UUID; auto-created if omitted.",
    )
    message: str = Field(..., min_length=1, max_length=2000)
    guest_phone: str | None = Field(None, max_length=32)


class PaymentOut(BaseModel):
    order_id: int
    payment_id: int
    qr_code_payload: str
    qr_code_base64: str | None = None
    ticket_url: str | None = None
    expires_at: datetime | None = None
    amount_cents: int


class ChatMessageOut(BaseModel):
    session_id: str
    messages: list[str]
    payment: PaymentOut | None = None
    order_id: int | None = None


# ── Context builder ────────────────────────────────────────────────────────


async def _build_context(session: AsyncSession) -> str:
    """Return a compact plain-text block with live plans + character names."""
    lines: list[str] = []

    # Plans
    plans = (
        (await session.execute(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.id)
        )).scalars().all()
    )
    if plans:
        lines.append("<planos>")
        for p in plans:
            price = f"R$ {p.price_cents / 100:.2f}".replace(".", ",")
            lines.append(f"  {p.slug} | {p.name} | {p.video_count} vídeo(s) | {price}")
        lines.append("</planos>")

    # Characters (names only to keep context short)
    try:
        rows = (
            await session.execute(
                text('SELECT name, tags FROM web.character_v ORDER BY name ASC LIMIT 200')
            )
        ).all()
        if rows:
            lines.append("<catalogo_personagens>")
            for r in rows:
                tag_str = f" [{', '.join(r[1])}]" if r[1] else ""
                lines.append(f"  {r[0]}{tag_str}")
            lines.append("</catalogo_personagens>")
    except Exception:  # noqa: BLE001 — view may not exist on fresh DB
        pass

    return "\n".join(lines) if lines else ""


# ── Routes ─────────────────────────────────────────────────────────────────


@router.post("/message", response_model=ChatMessageOut)
async def send_message(
    body: ChatMessageIn,
    session: AsyncSession = Depends(get_session),
) -> ChatMessageOut:
    context = await _build_context(session)
    reply: ChatReply = await chat_turn(
        session_id=body.session_id,
        user_message=body.message,
        extra_context=context or None,
        db_session=session,
        guest_phone=body.guest_phone,
    )
    payment_out = None
    if reply.payment is not None:
        p = reply.payment
        payment_out = PaymentOut(
            order_id=p.order_id,
            payment_id=p.payment_id,
            qr_code_payload=p.qr_code_payload,
            qr_code_base64=p.qr_code_base64,
            ticket_url=p.ticket_url,
            expires_at=p.expires_at,
            amount_cents=p.amount_cents,
        )
    return ChatMessageOut(
        session_id=reply.session_id,
        messages=reply.messages,
        payment=payment_out,
        order_id=reply.order_id,
    )


@router.delete("/session/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    await clear_session(session_id)
