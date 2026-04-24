"""LLM script writer.

Produces the **structured** per-character speaking lines that the
``prompt_builder`` will splice into the strict Grok video prompt.

For a single video with N characters we generate:

* one short PT-BR line per character (≤14 words each, single sentence,
  greets the recipient or builds toward the occasion),
* one final group line (≤14 words) that all characters say together.

Characters speak one at a time inside a fixed 10-second clip, so each
individual line must be brief — see ``MAX_WORDS_PER_LINE`` below.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.settings import get_settings

MAX_WORDS_PER_LINE = 14
MAX_ATTEMPTS = 3

OCCASIONS: dict[str, str] = {
    "aniversario": "aniversário",
    "parabens": "parabéns",
    "motivacao": "motivação",
    "natal": "Natal",
    "dia-das-maes": "Dia das Mães",
    "dia-dos-pais": "Dia dos Pais",
    "casamento": "casamento",
    "formatura": "formatura",
    "boas_festas": "boas festas",
    "amor": "declaração de amor",
    "personalizado": "mensagem carinhosa",
}

_REASONING_MODEL_PREFIXES = ("o", "gpt-5")


def _is_reasoning_model(model: str) -> bool:
    m = model.lower()
    return any(m.startswith(prefix) for prefix in _REASONING_MODEL_PREFIXES)


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=get_settings().openai_api_key)


@dataclass(slots=True)
class CharacterSpec:
    id: int
    name: str
    descriptor: str  # short visual descriptor, e.g. "short black hair in two buns"


@dataclass(slots=True)
class StructuredScript:
    """Result of generation — exactly N character lines + 1 group line."""

    character_lines: list[str]  # parallel to ``characters`` order
    group_line: str


async def _chat_json(messages: list[dict[str, str]]) -> dict:
    settings = get_settings()
    extra: dict[str, str] = {}
    if _is_reasoning_model(settings.openai_model):
        extra["reasoning_effort"] = "minimal"
    completion = await _client().chat.completions.create(
        model=settings.openai_model,
        messages=messages,
        response_format={"type": "json_object"},
        max_completion_tokens=800,
        **extra,  # type: ignore[arg-type]
    )
    raw = (completion.choices[0].message.content or "").strip()
    return json.loads(raw)


def _coerce_lines(payload: dict, n: int) -> StructuredScript | None:
    raw_lines = payload.get("character_lines") or payload.get("lines") or []
    group = payload.get("group_line") or payload.get("final_line") or ""
    if not isinstance(raw_lines, list) or len(raw_lines) != n:
        return None
    cleaned: list[str] = []
    for item in raw_lines:
        if isinstance(item, dict):
            text = item.get("line") or item.get("text") or ""
        else:
            text = str(item)
        text = text.strip().strip('"').strip("“").strip("”")
        if not text:
            return None
        cleaned.append(text)
    group = str(group).strip().strip('"').strip("“").strip("”")
    if not group:
        return None
    return StructuredScript(character_lines=cleaned, group_line=group)


async def generate_structured_script(
    *,
    characters: list[CharacterSpec],
    recipient_name: str,
    recipient_age: str | None = None,
    occasion_slug: str | None = None,
    user_message: str | None = None,
) -> StructuredScript:
    """Generate the per-character + group lines for one video.

    ``user_message``: optional client-supplied message that MUST be
    preserved verbatim (distributed across character lines if needed).
    """
    n = len(characters)
    if n < 1:
        raise ValueError("at least one character is required")

    occasion = OCCASIONS.get(occasion_slug or "personalizado", "mensagem carinhosa")
    age_hint = f" ({recipient_age} anos)" if recipient_age else ""

    char_block = "\n".join(
        f"  {idx + 1}. {c.name} — {c.descriptor or 'personagem infantil carismático'}"
        for idx, c in enumerate(characters)
    )

    custom_block = ""
    if user_message:
        custom_block = (
            "\n\nMENSAGEM DO CLIENTE (preserve a intenção e palavras-chave, "
            "distribuindo entre os personagens de forma natural):\n"
            f'"""\n{user_message.strip()}\n"""\n'
        )

    user_prompt = f"""Escreva as falas de um vídeo cameo curto em português brasileiro para a criança {recipient_name}{age_hint}.

Personagens (na ordem em que aparecem da esquerda para a direita):
{char_block}

Ocasião: {occasion}

REGRAS RÍGIDAS:
- Total: {n} fala(s) individual(is) + 1 fala final em grupo.
- CADA fala individual: 1 frase curta, no MÁXIMO {MAX_WORDS_PER_LINE} palavras.
- Fala em grupo final: 1 frase curta, no MÁXIMO {MAX_WORDS_PER_LINE} palavras, em uníssono.
- Tudo em português brasileiro, tom alegre, carinhoso, 100% seguro para criança.
- Use o JEITO DE FALAR de cada personagem (vocabulário/maneirismo), mas o conteúdo deve ser positivo.
- A primeira fala deve cumprimentar {recipient_name} pelo nome.
- Sem narração, sem aspas, sem indicações de cena.{custom_block}

Responda em JSON com exatamente este formato:
{{
  "character_lines": ["fala da posição 1", "fala da posição 2", ...],
  "group_line": "fala final que todos dizem em uníssono"
}}"""

    messages = [
        {
            "role": "system",
            "content": "You write short, wholesome, kid-safe character cameo lines in Brazilian Portuguese. Always reply with valid JSON matching the requested schema.",
        },
        {"role": "user", "content": user_prompt},
    ]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            payload = await _chat_json(messages)
            result = _coerce_lines(payload, n)
            if result is not None:
                return result
        except Exception:
            if attempt == MAX_ATTEMPTS:
                break

    # ── Fallback: deterministic generic lines so generation never blocks ──
    occ_word = OCCASIONS.get(occasion_slug or "personalizado", "carinho")
    fallback_lines: list[str] = []
    for idx in range(n):
        if idx == 0:
            fallback_lines.append(f"Oi, {recipient_name}! Olha quem veio te ver!")
        elif idx == 1:
            fallback_lines.append("A gente preparou algo muito especial pra você!")
        else:
            fallback_lines.append("É o seu dia! Aproveita cada segundo!")
    group = (
        f"Feliz {occ_word}, {recipient_name}! A gente te ama!"
        if occasion_slug == "aniversario"
        else f"Um super beijo pra você, {recipient_name}!"
    )
    return StructuredScript(character_lines=fallback_lines, group_line=group)
