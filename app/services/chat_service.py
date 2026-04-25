"""Chat service — stateful conversation backed by Redis history.

Each session stores up to MAX_HISTORY_TURNS turns (user + assistant pairs).

On every turn we run two parallel LLM calls:
1. **Conversation agent** — generates reply bubbles for the user.
2. **Extraction agent** — silently extracts structured order data from the
   latest user message and accumulates it in Redis (key ``chat:order:<sid>``).

When the extracted order data is complete (plan + characters confirmed) and the
user has confirmed the summary, we automatically create the DB order + Pix
payment and return the QR code in the reply so the frontend can show it.

Usage
-----
    from app.services.chat_service import chat_turn

    reply = await chat_turn(
        session_id="abc123",
        user_message="Quero o pacote de 3, personagem Sonic",
        db_session=session,     # AsyncSession — needed to create order
        guest_phone="5511999...",
    )
    # reply.messages       → list[str] (chat bubbles)
    # reply.session_id     → echoed session id
    # reply.payment        → PaymentInfo | None (set when QR just generated)
    # reply.order_id       → int | None
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings

MAX_HISTORY_TURNS = 20
HISTORY_TTL_SECONDS = 60 * 60          # 1-hour idle expiry
ORDER_STATE_TTL = 60 * 60 * 2          # 2 hours

# ─────────────────────────── system prompts ──────────────────────────────

_SYSTEM_PROMPT = """\
# Identidade

Você é a assistente virtual da Persona, um serviço que cria **vídeos personalizados** com personagens animados favoritos de crianças (e adultos!). Você é simpática, descontraída e fala português brasileiro informal.

# Sobre o Serviço

O cliente escolhe um ou mais personagens do nosso catálogo, informa quem vai receber (nome + idade), a ocasião e uma mensagem opcional. A gente gera um vídeo curto (~10 segundos) com o personagem fazendo uma homenagem especial. Perfeito para aniversários, datas comemorativas, motivação, declarações de amor, etc.

# Fluxo Esperado

Guie o cliente por estas etapas, UMA pergunta por vez:

1. **Nome do destinatário** — "Qual o nome de quem vai receber o vídeo?"
2. **Personagem(ns)** — liste opções se o cliente pedir sugestões. Até 4 personagens por vídeo.
3. **Ocasião** — aniversário, Natal, motivação, etc.
4. **Mensagem personalizada** — opcional; se quiser pode deixar a equipe criar.
5. **Plano** — apresente as opções com preços (use os dados de <planos> se disponíveis).
6. **Resumo + confirmação** — mostre resumo e peça "Tudo certo?" antes de gerar o QR.

Se o cliente já forneceu alguma informação voluntariamente, aceite e pule para o próximo dado faltante.

Quando a confirmação for positiva ("sim", "pode ir", "tudo certo", "confirmo"), diga que está gerando o QR e aguarde — o sistema vai entregar automaticamente.

Se já apareceu um QR na conversa (campo <pedido_atual>), **NÃO peça os dados novamente** — o pedido já existe. Apenas confirme e ajude com dúvidas.

# Regras de Mensagem

- Responda em JSON: `{"messages": ["bolha 1", "bolha 2"], "reasoning": "..."}`
- 1 a 3 mensagens curtas (1-3 frases cada). Ninguém lê parágrafos num chat.
- Use **negrito** (com asteriscos) para destaques. Sem listas com `-` ou cabeçalhos `#`.
- Emojis: 1-2 por mensagem, no final. Nunca 3+ seguidos.
- Idioma: português brasileiro, "você", informal.
- NUNCA mencione IA, algoritmo ou tecnologia. Fale como equipe humana.
- NUNCA peça dados de pagamento (CPF, chave Pix, cartão). O sistema gera o link automaticamente.
- NUNCA invente nem mostre prévia da mensagem que o personagem vai falar.
- Não se apresente a cada resposta.

# Nunca Faça

- Não repita perguntas já respondidas no histórico
- Não invente personagens que não existem no catálogo
- Não faça mais de 3 bolhas por resposta
- Não use português de Portugal
"""

_EXTRACTOR_SYSTEM_PROMPT = """\
Você é um motor de extração de dados. Analise APENAS a última mensagem do usuário e extraia campos estruturados.
Retorne SOMENTE JSON válido com os campos encontrados. Omita campos sem dados. Se nada extrair: {}

# Campos disponíveis

## planSlug (string)
Slug do plano. Mapeie conforme exemplos:
"1 vídeo" / "teste" / "19,90" → o slug do plano mais barato disponível em <planos>
"3 vídeos" / "surpresa" / "29,90" → slug do plano de 3 vídeos
"5 vídeos" / "completo" / "49,90" → slug do plano de 5 vídeos
Use o campo `slug` exato de <planos>.

## characterSlugs (array of strings)
Nomes/slugs dos personagens escolhidos para o vídeo. Ex: ["sonic", "tails"]
Use os nomes exatos do catálogo em <catalogo_personagens> quando possível.
Máximo 4. NUNCA extraia se o cliente só pediu "sugestões" sem escolher.

## recipientName (string)
Nome de quem vai receber. "pro meu filho João" → "João"

## recipientAge (string)
Idade do destinatário. "vai fazer 8 anos" → "8"

## occasionSlug (string)
Normalize: "aniversário/niver" → "aniversario", "natal/boas festas" → "natal",
"motivação" → "motivacao", "amor/declaração" → "amor",
"dia das mães" → "dia-das-maes", "dia dos pais" → "dia-dos-pais",
"formatura" → "formatura", "casamento" → "casamento"

## customMessage (string)
Mensagem personalizada que o cliente quer no vídeo. Máximo 400 chars.

## autoMessage (boolean)
true se o cliente disse "deixa a equipe criar", "pode inventar", "surpresa na mensagem".

## dataConfirmed (boolean)
true SOMENTE quando o cliente confirma explicitamente o resumo:
"tudo certo", "confirma", "pode ir", "sim", "confirmo", "isso mesmo", "perfeito"
ATENÇÃO: só extraia se o assistente acabou de mostrar um resumo.

## guestPhone (string)
Número de telefone do cliente em formato E.164 se mencionado. Ex: "+5511999998888"
"""


# ─────────────────────────── data classes ────────────────────────────────


@dataclass
class PaymentInfo:
    order_id: int
    payment_id: int
    qr_code_payload: str
    qr_code_base64: str | None
    ticket_url: str | None
    expires_at: datetime | None
    amount_cents: int


@dataclass
class ChatReply:
    session_id: str
    messages: list[str]
    payment: PaymentInfo | None = None
    order_id: int | None = None


@dataclass
class OrderState:
    """Accumulated order data extracted across multiple turns."""
    plan_slug: str | None = None
    character_slugs: list[str] = field(default_factory=list)
    recipient_name: str | None = None
    recipient_age: str | None = None
    occasion_slug: str | None = None
    custom_message: str | None = None
    auto_message: bool = False
    data_confirmed: bool = False
    order_id: int | None = None          # set once DB order is created
    payment_id: int | None = None        # set once Pix payment is created

    def is_ready_for_checkout(self) -> bool:
        """True when we have enough data to create an order + Pix payment."""
        return bool(
            self.plan_slug
            and self.character_slugs
            and self.recipient_name
            and self.data_confirmed
            and self.order_id is None   # not already created
        )


# ─────────────────────────── Redis helpers ───────────────────────────────


def _history_key(session_id: str) -> str:
    return f"chat:history:{session_id}"


def _order_state_key(session_id: str) -> str:
    return f"chat:order:{session_id}"


async def _load_history(session_id: str) -> list[dict[str, str]]:
    try:
        from app.workers.queue import get_pool
        pool = await get_pool()
        raw = await pool.get(_history_key(session_id))
        if raw is None:
            return []
        return json.loads(raw)
    except Exception:
        return []


async def _save_history(session_id: str, history: list[dict[str, str]]) -> None:
    try:
        from app.workers.queue import get_pool
        pool = await get_pool()
        trimmed = history[-(MAX_HISTORY_TURNS * 2):]
        await pool.set(_history_key(session_id), json.dumps(trimmed), ex=HISTORY_TTL_SECONDS)
    except Exception:
        pass


async def _load_order_state(session_id: str) -> OrderState:
    try:
        from app.workers.queue import get_pool
        pool = await get_pool()
        raw = await pool.get(_order_state_key(session_id))
        if raw is None:
            return OrderState()
        data = json.loads(raw)
        return OrderState(
            plan_slug=data.get("plan_slug"),
            character_slugs=data.get("character_slugs") or [],
            recipient_name=data.get("recipient_name"),
            recipient_age=data.get("recipient_age"),
            occasion_slug=data.get("occasion_slug"),
            custom_message=data.get("custom_message"),
            auto_message=data.get("auto_message", False),
            data_confirmed=data.get("data_confirmed", False),
            order_id=data.get("order_id"),
            payment_id=data.get("payment_id"),
        )
    except Exception:
        return OrderState()


async def _save_order_state(session_id: str, state: OrderState) -> None:
    try:
        from app.workers.queue import get_pool
        pool = await get_pool()
        data = {
            "plan_slug": state.plan_slug,
            "character_slugs": state.character_slugs,
            "recipient_name": state.recipient_name,
            "recipient_age": state.recipient_age,
            "occasion_slug": state.occasion_slug,
            "custom_message": state.custom_message,
            "auto_message": state.auto_message,
            "data_confirmed": state.data_confirmed,
            "order_id": state.order_id,
            "payment_id": state.payment_id,
        }
        await pool.set(_order_state_key(session_id), json.dumps(data), ex=ORDER_STATE_TTL)
    except Exception:
        pass


# ─────────────────────────── LLM helpers ─────────────────────────────────


async def _call_llm(history: list[dict[str, str]]) -> list[str]:
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, *history]
    completion = await client.chat.completions.create(
        model=settings.openai_model,
        messages=messages,  # type: ignore[arg-type]
        response_format={"type": "json_object"},
        max_completion_tokens=600,
    )
    raw = (completion.choices[0].message.content or "{}").strip()
    payload = json.loads(raw)
    bubbles = payload.get("messages") or payload.get("message") or []
    if isinstance(bubbles, str):
        bubbles = [bubbles]
    return [str(b).strip() for b in bubbles if str(b).strip()]


async def _extract_order_data(
    latest_user_message: str,
    context: str,
) -> dict[str, Any]:
    """Run the extraction agent against the latest user message only.

    Returns a dict with the fields the extractor found, or {} on any error.
    """
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        completion = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": _EXTRACTOR_SYSTEM_PROMPT + "\n\n" + context},
                {"role": "user", "content": latest_user_message},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=300,
        )
        raw = (completion.choices[0].message.content or "{}").strip()
        return json.loads(raw)
    except Exception:
        return {}


def _merge_extracted(state: OrderState, extracted: dict[str, Any]) -> OrderState:
    """Merge newly extracted fields into the accumulated order state."""
    if extracted.get("planSlug"):
        state.plan_slug = extracted["planSlug"]
    if extracted.get("characterSlugs"):
        slugs = extracted["characterSlugs"]
        if isinstance(slugs, list) and slugs:
            state.character_slugs = [str(s) for s in slugs[:4]]
    if extracted.get("recipientName"):
        state.recipient_name = str(extracted["recipientName"])
    if extracted.get("recipientAge"):
        state.recipient_age = str(extracted["recipientAge"])
    if extracted.get("occasionSlug"):
        state.occasion_slug = str(extracted["occasionSlug"])
    if extracted.get("customMessage"):
        state.custom_message = str(extracted["customMessage"])[:400]
    if extracted.get("autoMessage") is True:
        state.auto_message = True
    if extracted.get("dataConfirmed") is True:
        state.data_confirmed = True
    return state


# ─────────────────────────── order + payment ────────────────────────────


async def _create_order_and_checkout(
    state: OrderState,
    db_session: AsyncSession,
    guest_phone: str | None,
    api_base_url: str,
) -> PaymentInfo | None:
    """Create the DB order, items, and Pix payment. Returns PaymentInfo on success."""
    from app.db.models import Order, OrderItem, OrderStatus, Payment, PaymentStatus, Plan
    from app.services import mercadopago_client

    # Resolve plan
    plan = (
        await db_session.execute(
            select(Plan).where(Plan.slug == state.plan_slug, Plan.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if plan is None:
        return None

    # Create order
    order = Order(
        plan_id=plan.id,
        recipient_name=state.recipient_name,
        recipient_age=state.recipient_age,
        occasion_slug=state.occasion_slug,
        guest_phone=guest_phone,
        total_cents=plan.price_cents,
        status=OrderStatus.DRAFT,
    )
    db_session.add(order)
    await db_session.flush()

    # Add item(s) — one item per video slot, all with the chosen characters.
    # For multi-video plans with a single character list we duplicate across slots.
    for seq in range(1, plan.video_count + 1):
        db_session.add(OrderItem(
            order_id=order.id,
            sequence=seq,
            character_ids=state.character_slugs,
            custom_message=None if state.auto_message else state.custom_message,
        ))

    order.status = OrderStatus.AWAITING_PAYMENT
    await db_session.flush()

    # Create Pix payment
    notification_url = f"{api_base_url.rstrip('/')}/api/v1/payments/mercadopago/webhook"
    try:
        pix = await mercadopago_client.create_pix_payment(
            order_id=order.id,
            amount_cents=order.total_cents,
            description=f"Persona — pedido #{order.id}",
            notification_url=notification_url,
            payer_email=None,
        )
    except Exception:
        # Rollback the order if payment creation fails; caller will handle
        await db_session.rollback()
        return None

    payment = Payment(
        order_id=order.id,
        provider="mercadopago",
        provider_id=pix["payment_id"],
        status=PaymentStatus.PENDING,
        amount_cents=order.total_cents,
        qr_code_payload=pix["qr_code"],
        ticket_url=pix.get("ticket_url"),
        expires_at=pix.get("expires_at"),
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)

    return PaymentInfo(
        order_id=order.id,
        payment_id=payment.id,
        qr_code_payload=pix["qr_code"],
        qr_code_base64=pix.get("qr_code_base64"),
        ticket_url=pix.get("ticket_url"),
        expires_at=pix.get("expires_at"),
        amount_cents=order.total_cents,
    )


# ─────────────────────────── public API ──────────────────────────────────


async def chat_turn(
    *,
    session_id: str,
    user_message: str,
    extra_context: str | None = None,
    db_session: AsyncSession | None = None,
    guest_phone: str | None = None,
) -> ChatReply:
    """Process one user turn and return the assistant's reply bubbles.

    ``extra_context``: optional plain-text block injected as a brief system
    message (e.g. current catalog + plans).
    ``db_session``: AsyncSession required to create orders. If None, order
    creation is skipped (graceful degradation).
    """
    history = await _load_history(session_id)
    order_state = await _load_order_state(session_id)

    # Build context block for both LLM calls
    context_block = extra_context or ""
    if order_state.order_id:
        context_block += f"\n<pedido_atual>Pedido #{order_state.order_id} já criado. QR code enviado.</pedido_atual>"

    # Run conversation agent and extraction agent in parallel
    history_with_context = list(history)
    if context_block:
        history_with_context.append({"role": "system", "content": context_block})
    history_with_context.append({"role": "user", "content": user_message.strip()})

    llm_task = asyncio.create_task(_call_llm(history_with_context))
    extract_task = asyncio.create_task(_extract_order_data(user_message.strip(), context_block))
    await asyncio.gather(llm_task, extract_task, return_exceptions=True)

    # Process conversation reply
    try:
        bubbles: list[str] = llm_task.result()  # type: ignore[union-attr]
    except Exception:
        bubbles = ["Ops, tive um probleminha aqui 😅 Pode repetir?"]
    if not bubbles:
        bubbles = ["Desculpa, não entendi! Pode repetir? 😊"]

    # Process extraction
    payment_info: PaymentInfo | None = None
    try:
        extracted: dict[str, Any] = extract_task.result()  # type: ignore[union-attr]
        if extracted:
            order_state = _merge_extracted(order_state, extracted)
    except Exception:
        extracted = {}

    # Check if we should create order + checkout
    if order_state.is_ready_for_checkout() and db_session is not None:
        settings = get_settings()
        payment_info = await _create_order_and_checkout(
            state=order_state,
            db_session=db_session,
            guest_phone=guest_phone,
            api_base_url=settings.api_base_url,
        )
        if payment_info:
            order_state.order_id = payment_info.order_id
            order_state.payment_id = payment_info.payment_id
            # Replace last bubble with a "QR code ready" message if it was generic
            bubbles = [
                "Perfeito! Pedido criado com sucesso 🎉",
                "Aqui está seu **QR code Pix** para pagamento. Após a confirmação, sua equipe começa a produção! 🎬",
            ]

    # Persist state
    await _save_order_state(session_id, order_state)

    # Save history (clean — no injected system messages)
    clean_history = [m for m in history if m["role"] != "system"]
    clean_history.append({"role": "user", "content": user_message.strip()})
    clean_history.append({"role": "assistant", "content": " ".join(bubbles)})
    await _save_history(session_id, clean_history)

    return ChatReply(
        session_id=session_id,
        messages=bubbles,
        payment=payment_info,
        order_id=order_state.order_id,
    )


async def clear_session(session_id: str) -> None:
    """Delete the conversation history and order state for a session."""
    try:
        from app.workers.queue import get_pool
        pool = await get_pool()
        await pool.delete(_history_key(session_id))
        await pool.delete(_order_state_key(session_id))
    except Exception:
        pass

