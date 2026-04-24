"""Seed the three reference plans from planos.txt (Entrada / Core / Premium).

Run with:
    python -m app.scripts.seed_plans
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db.models import BillingPeriod, PersonalizationLevel, Plan
from app.db.session import session_scope

PLANS = [
    {
        "slug": "entrada",
        "name": "Plano Entrada",
        "description": "1 vídeo com personagem aleatório. Pra experimentar.",
        "price_cents": 1990,
        "video_count": 1,
        "max_characters_per_video": 1,
        "personalization_level": PersonalizationLevel.NAME_ONLY,
        "is_subscription": False,
        "billing_period": None,
        "features": ["1 vídeo", "Personagem aleatório", "Personalização: nome"],
        "sort_order": 1,
    },
    {
        "slug": "core",
        "name": "Plano Surpresa",
        "description": "3 vídeos com escolha de personagens. Mais popular.",
        "price_cents": 2990,
        "video_count": 3,
        "max_characters_per_video": 1,
        "personalization_level": PersonalizationLevel.MEDIUM,
        "is_subscription": False,
        "billing_period": None,
        "features": ["3 vídeos", "Escolha de personagens", "Nome + idade + ocasião"],
        "sort_order": 2,
    },
    {
        "slug": "premium",
        "name": "Plano Completo",
        "description": "5 vídeos, até 3 personagens por vídeo, personalização total e prioridade.",
        "price_cents": 4990,
        "video_count": 5,
        "max_characters_per_video": 3,
        "personalization_level": PersonalizationLevel.FULL,
        "is_subscription": False,
        "billing_period": None,
        "features": [
            "5 vídeos",
            "Até 3 personagens por vídeo (Dupla Especial)",
            "Mensagem personalizada",
            "Prioridade na fila",
        ],
        "sort_order": 3,
    },
    {
        "slug": "club_weekly",
        "name": "Clube Persona Semanal",
        "description": "1 vídeo novo por semana, personagens variados.",
        "price_cents": 3990,
        "video_count": 1,
        "max_characters_per_video": 1,
        "personalization_level": PersonalizationLevel.MEDIUM,
        "is_subscription": True,
        "billing_period": BillingPeriod.WEEKLY,
        "features": ["1 vídeo por semana", "Renovação automática", "Cancele quando quiser"],
        "sort_order": 10,
    },
]


async def main() -> None:
    async with session_scope() as session:
        for spec in PLANS:
            existing = (
                await session.execute(select(Plan).where(Plan.slug == spec["slug"]))
            ).scalar_one_or_none()
            if existing is not None:
                for k, v in spec.items():
                    setattr(existing, k, v)
            else:
                session.add(Plan(**spec))
        await session.commit()
        print(f"Seeded {len(PLANS)} plans.")


if __name__ == "__main__":
    asyncio.run(main())
