"""Public catalog: plans, characters, occasions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan
from app.db.session import get_session
from app.schemas.v1 import CharacterOut as BaseCharacterOut
from app.schemas.v1 import OccasionOut, PlanOut
from app.services.plan_availability import RETIRED_PLAN_SLUGS


class CharacterOut(BaseCharacterOut):
    # The shared character view exposes Node UUIDs; older catalogs used integers.
    id: str | int


router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(session: AsyncSession = Depends(get_session)) -> list[Plan]:
    stmt = select(Plan).where(
        Plan.is_active.is_(True), Plan.slug.not_in(RETIRED_PLAN_SLUGS)
    ).order_by(Plan.sort_order, Plan.id)
    return list((await session.execute(stmt)).scalars().all())


@router.get("/characters", response_model=list[CharacterOut])
async def list_characters(
    occasion: str | None = Query(None),
    gender: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[CharacterOut]:
    """Read active characters from the shared read-only character view."""
    where: list[str] = ["is_active = true"]
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if gender:
        where.append('lower("gender") = lower(:gender)')
        params["gender"] = gender
    if occasion:
        where.append(':occasion = ANY("tags")')
        params["occasion"] = occasion
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = text(
        f"""
        SELECT id, name, slug, description, thumbnail_s3_key AS thumbnail_key,
               tags, gender, age_range
        FROM web.character_v
        {where_sql}
        ORDER BY id ASC
        LIMIT :limit OFFSET :offset
        """
    )
    try:
        rows = (await session.execute(sql, params)).mappings().all()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Character catalog unavailable") from exc

    from app.services.storage import presigned_get_url

    out: list[CharacterOut] = []
    for r in rows:
        thumb_url = None
        if r.get("thumbnail_key"):
            try:
                thumb_url = presigned_get_url(r["thumbnail_key"], expires_in=3600)
            except Exception:  # noqa: BLE001
                thumb_url = None
        out.append(
            CharacterOut(
                id=r["id"] if isinstance(r["id"], int) else str(r["id"]),
                name=r["name"],
                slug=r.get("slug"),
                description=r.get("description"),
                thumbnail_url=thumb_url,
                tags=r.get("tags"),
                gender=r.get("gender"),
                age_range=r.get("age_range"),
            )
        )
    return out


@router.get("/occasions", response_model=list[OccasionOut])
async def list_occasions(session: AsyncSession = Depends(get_session)) -> list[OccasionOut]:
    sql = text(
        """
        SELECT slug, label, prompt_hint
        FROM web.occasion_v
        WHERE COALESCE(is_active, true) = true
        ORDER BY slug
        """
    )
    try:
        rows = (await session.execute(sql)).mappings().all()
    except Exception:
        # Fallback to the static list mirrored from the Node backend.
        from app.services.script_writer import OCCASIONS

        return [OccasionOut(slug=k, label=v, prompt_hint=None) for k, v in OCCASIONS.items()]

    return [OccasionOut(**dict(r)) for r in rows]
