"""Chat service — stateful conversation backed by Redis history.

Each session stores up to MAX_HISTORY_TURNS turns (user + assistant pairs).
The system prompt mirrors the WhatsApp conversation agent but is adapted for
a web chat UI (no WhatsApp-specific formatting, no multi-bubble concept —
the assistant still may return multiple short messages that the frontend can
display as sequential bubbles).

Usage
-----
    from app.services.chat_service import chat_turn

    reply = await chat_turn(
        session_id="abc123",
        user_message="Oi, quero comprar um vídeo",
        context={"catalog": [...]},   # optional extra context
    )
    # reply.messages  → list[str]  (1-3 short bubbles)
    # reply.session_id → same session_id echoed back
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from app.core.settings import get_settings

MAX_HISTORY_TURNS = 20          # user+assistant pairs stored per session
HISTORY_TTL_SECONDS = 60 * 60  # 1 hour idle expiry

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
6. **Resumo + confirmação** — mostre resumo antes de finalizar.

Se o cliente já forneceu alguma informação voluntariamente, aceite e pule para o próximo dado faltante.

# Regras de Mensagem

- Responda em JSON: `{"messages": ["bolha 1", "bolha 2"], "reasoning": "..."}`
- 1 a 3 mensagens curtas (1-3 frases cada). Ninguém lê parágrafos num chat.
- Use **negrito** (com asteriscos) para destaques. Sem listas com `-` ou cabeçalhos `#`.
- Emojis: 1-2 por mensagem, no final. Nunca 3+ seguidos.
- Idioma: português brasileiro, "você", informal.
- NUNCA mencione IA, algoritmo ou tecnologia. Fale como equipe humana.
- NUNCA peça dados de pagamento (CPF, chave Pix, cartão). O sistema gera o link automaticamente.
- NUNCA invente nem mostre prévia da mensagem que o personagem vai falar — isso é gerado por outro sistema após o pagamento.
- Não se apresente a cada resposta. A saudação inicial já foi feita.

# Nunca Faça

- Não repita perguntas já respondidas no histórico
- Não invente personagens que não existem no catálogo
- Não faça mais de 3 bolhas por resposta
- Não use português de Portugal
"""


@dataclass
class ChatReply:
    session_id: str
    messages: list[str]


def _redis_key(session_id: str) -> str:
    return f"chat:history:{session_id}"


async def _load_history(session_id: str) -> list[dict[str, str]]:
    try:
        from app.workers.queue import get_pool  # lazy import to avoid circular deps
        pool = await get_pool()
        raw = await pool.get(_redis_key(session_id))  # type: ignore[union-attr]
        if raw is None:
            return []
        return json.loads(raw)
    except Exception:  # Redis unavailable — stateless degradation
        return []


async def _save_history(session_id: str, history: list[dict[str, str]]) -> None:
    try:
        from app.workers.queue import get_pool
        pool = await get_pool()
        trimmed = history[-(MAX_HISTORY_TURNS * 2):]
        await pool.set(  # type: ignore[union-attr]
            _redis_key(session_id),
            json.dumps(trimmed),
            ex=HISTORY_TTL_SECONDS,
        )
    except Exception:  # Redis unavailable — skip saving
        pass


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


async def chat_turn(
    *,
    session_id: str,
    user_message: str,
    extra_context: str | None = None,
) -> ChatReply:
    """Process one user turn and return the assistant's reply bubbles.

    ``extra_context``: optional plain-text block injected as an extra system
    message (e.g. current catalog, plan list) — fetched by the router per request.
    """
    history = await _load_history(session_id)

    if extra_context:
        # Inject context as a brief system-role reminder before the user message.
        history.append({"role": "system", "content": extra_context})

    history.append({"role": "user", "content": user_message.strip()})

    try:
        bubbles = await _call_llm(history)
    except Exception:  # noqa: BLE001  – fallback so the UI never gets a 500
        bubbles = ["Ops, tive um probleminha aqui 😅 Pode repetir?"]

    if not bubbles:
        bubbles = ["Desculpa, não entendi! Pode repetir? 😊"]

    assistant_text = " ".join(bubbles)
    # Remove injected system messages before saving so history stays clean
    clean_history = [m for m in history if m["role"] != "system"]
    clean_history.append({"role": "assistant", "content": assistant_text})
    await _save_history(session_id, clean_history)

    return ChatReply(session_id=session_id, messages=bubbles)


async def clear_session(session_id: str) -> None:
    """Delete the conversation history for a session (e.g. on logout)."""
    from app.workers.queue import get_pool

    pool = await get_pool()
    await pool.delete(_redis_key(session_id))  # type: ignore[union-attr]
