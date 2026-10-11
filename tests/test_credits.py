from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1 import orders
from app.db.models import CreditTransactionType, OrderStatus
from app.schemas.v1 import CardCheckoutIn
from app.services.credits import grant_paid_order_credit


@pytest.fixture(autouse=True)
def valid_assets(monkeypatch):
    monkeypatch.setattr("app.services.catalog_validation.validate_order_assets", AsyncMock())


@pytest.mark.asyncio
async def test_purchases_never_award_free_credits():
    user = SimpleNamespace(id=1, credit_balance=2)
    order = SimpleNamespace(id=3, user_id=1)
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        SimpleNamespace(scalar_one_or_none=lambda: user),
        SimpleNamespace(scalar_one_or_none=lambda: None),
        SimpleNamespace(scalar_one_or_none=lambda: user),
        SimpleNamespace(scalar_one_or_none=lambda: session.add.call_args.args[0]),
    ])
    await grant_paid_order_credit(session, order)
    await grant_paid_order_credit(session, order)
    assert user.credit_balance == 2
    session.add.assert_not_called()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("balance,needed", [(0, 1), (2, 3)])
async def test_insufficient_credits_cannot_start_processing(monkeypatch, balance, needed):
    user = SimpleNamespace(id=1, credit_balance=balance, email_verified_at=True)
    order = SimpleNamespace(
        id=3, user_id=1, status=OrderStatus.DRAFT, created_at=datetime.now(UTC),
        plan=SimpleNamespace(video_count=needed),
        items=[SimpleNamespace(id=i) for i in range(needed)],
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: user))
    monkeypatch.setattr(orders, "_load_order", AsyncMock(return_value=order))
    with pytest.raises(HTTPException) as error:
        await orders.checkout(3, CardCheckoutIn(use_credits=True), user_id=1,
                              guest_order_ids=[], x_guest_token=None, session=session)
    assert error.value.status_code == 402
    assert user.credit_balance == balance
    assert session.add.call_count == 0


@pytest.mark.asyncio
async def test_redeeming_credits_records_debit_and_no_purchase_reward(monkeypatch):
    user = SimpleNamespace(id=1, credit_balance=3, email_verified_at=True)
    order = SimpleNamespace(id=3, user_id=1, status=OrderStatus.DRAFT, created_at=datetime.now(UTC),
                            plan=SimpleNamespace(video_count=2),
                            items=[SimpleNamespace(id=10), SimpleNamespace(id=11)])
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: user))
    session.commit = AsyncMock()
    async def assign_ids():
        for index, call in enumerate(session.add.call_args_list):
            call.args[0].id = index + 1
    session.flush = AsyncMock(side_effect=assign_ids)
    monkeypatch.setattr(orders, "_load_order", AsyncMock(return_value=order))
    result = await orders.checkout(3, CardCheckoutIn(use_credits=True), user_id=1,
                                   guest_order_ids=[], x_guest_token=None, session=session)
    assert user.credit_balance == 1
    transaction = session.add.call_args_list[0].args[0]
    assert transaction.amount == -2
    assert transaction.balance_after == 1
    assert transaction.kind == CreditTransactionType.REDEEM
    assert result.amount_cents == 0
    assert result.payment_method == "credits"
    assert order.status == OrderStatus.PAID
    jobs = [call.args[0].compile().params.get("payload") for call in session.execute.call_args_list]
    assert {"item_id": 10} in jobs
    assert {"item_id": 11} in jobs
