"""Credit account endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_user
from app.db.models import CreditTransaction, User
from app.db.session import get_session
from app.schemas.v1 import CreditAccountOut

router = APIRouter(prefix="/credits", tags=["credits"])


@router.get("/me", response_model=CreditAccountOut)
async def my_credits(
    user_id: int = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> CreditAccountOut:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")

    transactions = (
        await session.execute(
            select(CreditTransaction)
            .where(CreditTransaction.user_id == user.id)
            .order_by(CreditTransaction.created_at.desc(), CreditTransaction.id.desc())
            .limit(25)
        )
    ).scalars().all()
    return CreditAccountOut(balance=user.credit_balance, debt=user.credit_debt, transactions=list(transactions))
