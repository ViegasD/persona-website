"""Credit ledger operations within the caller's database transaction."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CreditTransaction, CreditTransactionType, Order, User


def apply_credit(user: User, amount: int) -> None:
    net = user.credit_balance - (getattr(user, "credit_debt", 0) or 0) + amount
    user.credit_balance = max(0, net)
    user.credit_debt = max(0, -net)


async def reverse_order_credits(session: AsyncSession, order: Order) -> None:
    if order.user_id is None:
        return
    user = (await session.execute(select(User).where(User.id == order.user_id).with_for_update()
                                  .execution_options(populate_existing=True))).scalar_one_or_none()
    if user is None:
        return
    transactions = (await session.execute(select(CreditTransaction).where(
        CreditTransaction.order_id == order.id,
        CreditTransaction.user_id == user.id,
    ))).scalars().all()
    if any(tx.kind == CreditTransactionType.REFUND for tx in transactions):
        return
    amount = -sum(tx.amount for tx in transactions if tx.kind in {
        CreditTransactionType.PURCHASE, CreditTransactionType.REDEEM,
    })
    if not amount:
        return
    apply_credit(user, amount)
    session.add(CreditTransaction(
        user_id=user.id, order_id=order.id, kind=CreditTransactionType.REFUND,
        amount=amount, balance_after=user.credit_balance, debt_after=user.credit_debt,
        note="Reversao de creditos do pedido reembolsado",
    ))


async def grant_paid_order_credit(session: AsyncSession, order: Order) -> None:
    """Retired reward hook: purchases never grant free credits."""
    return
