"""Persisted checkout controls; deployment safety limits cannot be overridden."""

from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StorefrontPolicy


class PolicyValues(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    revision: int = Field(0, ge=0)
    new_orders_enabled: StrictBool = True
    registration_enabled: StrictBool = True
    credit_redemption_enabled: StrictBool = True
    require_account_checkout: StrictBool = False


async def read_policy(session: AsyncSession) -> PolicyValues:
    row = await session.get(StorefrontPolicy, 1, populate_existing=True)
    return PolicyValues.model_validate(row) if row is not None else PolicyValues()


async def write_policy(session: AsyncSession, values: PolicyValues) -> PolicyValues:
    await session.execute(insert(StorefrontPolicy).values(id=1).on_conflict_do_nothing())
    row = (await session.execute(select(StorefrontPolicy).where(StorefrontPolicy.id == 1)
        .with_for_update().execution_options(populate_existing=True))).scalar_one()
    if row.revision != values.revision:
        raise HTTPException(409, detail="settings changed; reload before saving")
    for key, value in values.model_dump(exclude={"revision"}).items():
        setattr(row, key, value)
    row.revision += 1
    row.updated_at = datetime.now(UTC)
    await session.commit()
    return PolicyValues.model_validate(row)
