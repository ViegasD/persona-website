from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import credits


def test_reward_repays_refund_debt_before_becoming_spendable():
    user = SimpleNamespace(credit_balance=0, credit_debt=2)
    credits.apply_credit(user, 1)
    assert (user.credit_balance, user.credit_debt) == (0, 1)
    credits.apply_credit(user, 3)
    assert (user.credit_balance, user.credit_debt) == (2, 0)


def test_reversal_tracks_spent_reward_without_negative_balance():
    user = SimpleNamespace(credit_balance=0, credit_debt=0)
    credits.apply_credit(user, -1)
    assert (user.credit_balance, user.credit_debt) == (0, 1)


@pytest.mark.asyncio
async def test_repeated_refund_restores_redemption_only_once():
    user = SimpleNamespace(id=1, credit_balance=0, credit_debt=0)
    redemption = SimpleNamespace(kind=credits.CreditTransactionType.REDEEM, amount=-3)
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        SimpleNamespace(scalar_one_or_none=lambda: user),
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [redemption])),
        SimpleNamespace(scalar_one_or_none=lambda: user),
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [redemption, session.add.call_args.args[0]])),
    ])
    order = SimpleNamespace(id=7, user_id=1)
    await credits.reverse_order_credits(session, order)
    await credits.reverse_order_credits(session, order)
    assert user.credit_balance == 3
    assert session.add.call_count == 1


def test_token_expiry_and_single_use():
    from app.services.account_tokens import token_is_usable
    now = datetime.now(UTC)
    token = SimpleNamespace(expires_at=now + timedelta(minutes=1), used_at=None)
    assert token_is_usable(token, now)
    token.used_at = now
    assert not token_is_usable(token, now)
    token.used_at = None
    token.expires_at = now - timedelta(seconds=1)
    assert not token_is_usable(token, now)


def test_welcome_identity_normalizes_gmail_aliases():
    from app.services.account_tokens import welcome_identity
    assert welcome_identity('F.A.B+one@googlemail.com') == welcome_identity('fab@gmail.com')
    assert welcome_identity('fab@example.com') != welcome_identity('fab+one@example.com')


@pytest.mark.asyncio
async def test_queue_retry_keeps_stable_job_identity(monkeypatch):
    from app.services import outbox

    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(id=12)))
    enqueue = AsyncMock(side_effect=[ConnectionError("redis unavailable"), None])
    monkeypatch.setattr(outbox, "get_pool", AsyncMock(return_value=SimpleNamespace(enqueue_job=enqueue)))
    job = SimpleNamespace(id=19, kind="process_item", payload={"item_id": 12}, delivered_at=None, last_error=None)
    with pytest.raises(ConnectionError):
        await outbox.deliver_outbox_job(session, job)
    assert job.delivered_at is None
    await outbox.deliver_outbox_job(session, job)
    assert job.delivered_at is not None
    assert enqueue.call_args_list[0] == enqueue.call_args_list[1]
    assert enqueue.call_args.kwargs["_job_id"] == "outbox:19"
