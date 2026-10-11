from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Response

from app.api.v1 import catalog, orders
from app.db.models import OrderStatus
from app.schemas.v1 import CardCheckoutIn, OrderCreate
from app.services.plan_availability import plan_is_available
from app.services import chat_service


@pytest.mark.parametrize("slug,active,available", [
    ("teste", True, True), ("surpresa", True, True), ("completo", True, True),
    ("teste", False, False), ("aniversario", False, False), ("aniversario", True, False),
])
def test_plan_availability(slug, active, available):
    assert plan_is_available(SimpleNamespace(slug=slug, is_active=active)) is available


@pytest.mark.asyncio
async def test_catalog_excludes_retired_plan_in_query():
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    assert await catalog.list_plans(session) == []
    query = session.execute.call_args.args[0]
    compiled = query.compile(compile_kwargs={"literal_binds": True})
    assert "NOT IN ('aniversario')" in str(compiled)
    assert "is_active IS true" in str(compiled)


@pytest.mark.asyncio
@pytest.mark.parametrize("slug,active", [("aniversario", True), ("teste", False)])
async def test_unavailable_plan_cannot_create_draft(slug, active):
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(slug=slug, is_active=active)
    session.execute = AsyncMock(return_value=result)
    with pytest.raises(HTTPException) as blocked:
        await orders.create_order(OrderCreate(plan_id=slug), Response(), None, None, session)
    assert blocked.value.status_code == 404
    session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OrderStatus.DRAFT, OrderStatus.AWAITING_PAYMENT])
@pytest.mark.parametrize("slug,active", [("aniversario", True), ("teste", False)])
@pytest.mark.parametrize("use_credits", [False, True])
async def test_unavailable_draft_cannot_checkout(monkeypatch, status, slug, active, use_credits):
    order = SimpleNamespace(status=status, plan=SimpleNamespace(slug=slug, is_active=active))
    monkeypatch.setattr(orders, "_load_order", AsyncMock(return_value=order))
    payment = AsyncMock()
    monkeypatch.setattr(orders.mercadopago_client, "create_pix_payment", payment)
    session = MagicMock()
    with pytest.raises(HTTPException) as blocked:
        await orders.checkout(1, CardCheckoutIn(use_credits=use_credits), 1, [], None, session)
    assert blocked.value.status_code == 409
    payment.assert_not_awaited()
    session.add.assert_not_called()
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_chat_cannot_sell_retired_package():
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(slug="aniversario", is_active=True)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    state = chat_service.OrderState(plan_slug="aniversario")
    assert await chat_service._create_order_and_checkout(state, session, None, "https://example.com") is None
    session.add.assert_not_called()
