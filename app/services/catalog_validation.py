"""Check purchasable characters and readable image assets before charging."""

import asyncio

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order
from app.services import storage


async def validate_order_assets(session: AsyncSession, order: Order) -> None:
    slugs: set[str] = set()
    for item in order.items:
        characters = item.character_ids
        if (not characters or len(characters) > order.plan.max_characters_per_video
                or len(set(characters)) != len(characters)):
            raise HTTPException(422, "invalid character selection")
        slugs.update(characters)
    try:
        rows = (await session.execute(text(
            "SELECT slug, thumbnail_s3_key FROM web.character_v "
            "WHERE is_active = true AND slug = ANY(:slugs)"
        ), {"slugs": sorted(slugs)})).mappings().all()
    except Exception:
        raise HTTPException(503, "character catalog temporarily unavailable") from None
    if {row["slug"] for row in rows} != slugs or any(not row["thumbnail_s3_key"] for row in rows):
        raise HTTPException(422, "selected character is unavailable")
    try:
        for key in {row["thumbnail_s3_key"] for row in rows}:
            await asyncio.to_thread(storage.get_image_as_data_url, key)
    except Exception:
        raise HTTPException(503, "character reference temporarily unavailable") from None
