from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1 import admin, auth, orders
from app.db.session import get_session
from app.schemas.v1 import RegisterIn, PlanUpdate, CardCheckoutIn
from app.db.models import OrderStatus
from app.services import storefront_policy as policy
from app.services.account_tokens import grant_welcome_credit


@pytest.mark.asyncio
async def test_missing_policy_uses_existing_checkout_defaults():
    session = SimpleNamespace(get=AsyncMock(return_value=None))
    assert (await policy.read_policy(session)).model_dump() == policy.PolicyValues().model_dump()


@pytest.mark.asyncio
async def test_policy_writes_are_versioned_and_conflicts_do_not_commit():
    row = SimpleNamespace(**policy.PolicyValues().model_dump())
    session = SimpleNamespace(execute=AsyncMock(side_effect=[None, SimpleNamespace(scalar_one=lambda: row)]), commit=AsyncMock())
    result = await policy.write_policy(session, policy.PolicyValues(registration_enabled=False))
    assert result.revision == 1 and result.registration_enabled is False
    session.commit.assert_awaited_once()
    session.execute.side_effect = [None, SimpleNamespace(scalar_one=lambda: row)]
    with pytest.raises(HTTPException) as conflict:
        await policy.write_policy(session, policy.PolicyValues())
    assert conflict.value.status_code == 409 and session.commit.await_count == 1


def test_no_admin_field_can_enable_bonus_or_override_worker_safety():
    for forbidden in ['signup_bonus', 'purchase_bonus', 'processing_start_at', 'worker_enabled']:
        with pytest.raises(ValidationError):
            policy.PolicyValues.model_validate({forbidden: 1})
    with pytest.raises(ValidationError):
        policy.PolicyValues(registration_enabled='false')
    with pytest.raises(ValidationError):
        PlanUpdate(price_cents=0)


@pytest.mark.asyncio
async def test_registration_disabled_creates_no_account(monkeypatch):
    monkeypatch.setattr(policy, 'read_policy', AsyncMock(return_value=policy.PolicyValues(registration_enabled=False)))
    session = MagicMock()
    with pytest.raises(HTTPException) as disabled:
        await auth.register(RegisterIn(email='test@example.com', password='Password123'), session)
    assert disabled.value.status_code == 503
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_verification_never_grants_credit_or_changes_existing_balance():
    user = SimpleNamespace(credit_balance=5)
    session = MagicMock()
    await grant_welcome_credit(session, user)
    assert user.credit_balance == 5
    session.add.assert_not_called()
    session.execute.assert_not_called()


def test_admin_settings_require_key_and_never_expose_secrets(monkeypatch):
    monkeypatch.setattr(policy, 'read_policy', AsyncMock(return_value=policy.PolicyValues()))
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[get_session] = lambda: MagicMock()
    with TestClient(app) as client:
        assert client.get('/admin/storefront-settings').status_code == 401
        response = client.get('/admin/storefront-settings', headers={'X-Api-Key': 'admin-key'})
        assert response.status_code == 200
        data = response.json()
        assert data['status']['signup_bonus'] == data['status']['purchase_bonus'] == 0
        assert 'smtp_password' not in response.text and 'api_key' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('values,user_id,use_credits,expected', [
    ({'new_orders_enabled': False}, 1, False, 503),
    ({'require_account_checkout': True}, None, False, 401),
    ({'credit_redemption_enabled': False}, 1, True, 503),
])
async def test_checkout_enforces_admin_rules_before_payment(monkeypatch, values, user_id, use_credits, expected):
    monkeypatch.setattr(policy, 'read_policy', AsyncMock(return_value=policy.PolicyValues(**values)))
    monkeypatch.setattr('app.services.processing_policy.processing_allowed', lambda order: True)
    order = SimpleNamespace(status=OrderStatus.DRAFT, plan=SimpleNamespace(video_count=1), items=[SimpleNamespace(id=1)])
    monkeypatch.setattr(orders, '_load_order', AsyncMock(return_value=order))
    session = MagicMock()
    with pytest.raises(HTTPException) as blocked:
        await orders.checkout(1, CardCheckoutIn(use_credits=use_credits), user_id, [], None, session)
    assert blocked.value.status_code == expected
    session.add.assert_not_called()

