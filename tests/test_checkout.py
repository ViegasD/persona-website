"""Checkout regressions without external payment or queue services."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import orders
from app.db.models import OrderStatus, PaymentStatus
from app.schemas.v1 import CardCheckoutIn


@pytest.fixture(autouse=True)
def valid_assets(monkeypatch):
    monkeypatch.setattr("app.services.catalog_validation.validate_order_assets", AsyncMock())


@pytest.mark.asyncio
async def test_approved_card_records_payment_and_durable_jobs(monkeypatch):
    order = SimpleNamespace(
        id=10, user_id=None, status=OrderStatus.DRAFT, created_at=datetime.now(UTC),
        plan=SimpleNamespace(video_count=1, slug="teste", is_active=True), items=[SimpleNamespace(id=20)],
        total_cents=1000, guest_email="buyer@example.com", guest_phone=None, paid_at=None,
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    monkeypatch.setattr(orders, "_load_order", AsyncMock(return_value=order))
    monkeypatch.setattr(orders.mercadopago_client, "create_card_payment", AsyncMock(return_value={
        "payment_id": "42", "status": "approved", "status_detail": "accredited",
    }))
    monkeypatch.setattr(orders, "report_purchase", AsyncMock())
    async def assign_id(*args):
        for call in session.add.call_args_list:
            call.args[0].id = 30
    session.flush.side_effect = assign_id
    session.refresh.side_effect = assign_id

    result = await orders.checkout(
        10, CardCheckoutIn(card_token="token", payment_method_id="visa"),
        user_id=None, guest_order_ids=[10], x_guest_token=None, session=session,
    )
    payment = session.add.call_args_list[0].args[0]
    assert payment.status == PaymentStatus.APPROVED
    assert payment.paid_at is not None
    assert order.status == OrderStatus.PAID
    assert result.card_status == "approved"
    jobs = [call.args[0].compile().params.get("payload") for call in session.execute.call_args_list]
    assert {"item_id": 20} in jobs


@pytest.mark.asyncio
async def test_card_selection_does_not_reuse_pix(monkeypatch):
    order = SimpleNamespace(
        id=10, user_id=None, status=OrderStatus.DRAFT, created_at=datetime.now(UTC),
        plan=SimpleNamespace(video_count=1, slug="teste", is_active=True), items=[SimpleNamespace(id=20)],
        total_cents=1000, guest_email="buyer@example.com",
    )
    pending_pix = SimpleNamespace(id=99, qr_code_payload="pix", ticket_url=None,
                                  expires_at=None, amount_cents=1000)
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: pending_pix))
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=lambda payment: setattr(payment, "id", 30))
    monkeypatch.setattr(orders, "_load_order", AsyncMock(return_value=order))
    monkeypatch.setattr(orders.mercadopago_client, "create_card_payment", AsyncMock(return_value={
        "payment_id": "42", "status": "pending", "status_detail": "pending_review_manual",
    }))
    result = await orders.checkout(
        10, CardCheckoutIn(card_token="token", payment_method_id="visa"),
        user_id=None, guest_order_ids=[10], x_guest_token=None, session=session,
    )
    assert result.payment_method == "credit_card"
    assert result.payment_id == 30
