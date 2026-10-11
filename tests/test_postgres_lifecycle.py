"""Integration checks run only against the explicitly named disposable DB."""

import asyncio
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import deps
from app.api.v1 import auth, orders, payments
from app.core.settings import get_settings
from app.db.models import (
    AccountToken,
    CompositeFrameCache,
    CreditTransaction,
    CreditTransactionType,
    GoogleChallenge,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    OutboxJob,
    Payment,
    PaymentStatus,
    Plan,
    User,
)
from app.schemas.v1 import (
    AccountEmailIn,
    AccountTokenIn,
    CardCheckoutIn,
    LoginIn,
    PasswordResetIn,
    RegisterIn,
)
from app.services import outbox, worker_recovery
from app.services.account_tokens import verify_account
from app.services.credits import grant_paid_order_credit, reverse_order_credits


@pytest.fixture
async def database(monkeypatch):
    url = os.environ.get("PERSONA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PERSONA_TEST_DATABASE_URL not set")
    if not re.match(r"postgresql://[^/]+@127\.0\.0\.1:\d+/persona_storefront_test$", url):
        pytest.fail("Integration tests require the isolated persona_storefront_test database")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    monkeypatch.setattr(get_settings(), "smtp_host", "smtp.test.invalid")
    monkeypatch.setattr(get_settings(), "smtp_from", "noreply@example.com")
    monkeypatch.setattr("app.services.catalog_validation.validate_order_assets", AsyncMock())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()


async def latest_link(session, user_id, purpose):
    jobs = (await session.execute(select(OutboxJob).where(OutboxJob.user_id == user_id)
                                  .order_by(OutboxJob.id.desc()))).scalars().all()
    return re.search(r"#" + purpose + r"=([A-Za-z0-9_-]+)", jobs[0].payload["body"]).group(1)


@pytest.mark.asyncio
async def test_admin_policy_concurrent_save_and_no_customer_mutation(database):
    from app.services import storefront_policy
    async with database() as session:
        original = await storefront_policy.read_policy(session)
    async def save():
        async with database() as session:
            try:
                await storefront_policy.write_policy(session, original.model_copy(update={'registration_enabled': False}))
                return 200
            except HTTPException as error:
                return error.status_code
    assert sorted(await asyncio.gather(save(), save())) == [200, 409]
    async with database() as session:
        current = await storefront_policy.read_policy(session)
        assert not current.registration_enabled
        await storefront_policy.write_policy(session, original.model_copy(update={'revision': current.revision}))


@pytest.mark.asyncio
async def test_verified_signup_reset_and_session_revocation(database):
    email = f"person-{uuid.uuid4().hex}@example.com"
    async with database() as session:
        issued = await auth.register(RegisterIn(email=email, password="Original123"), session)
        user = await session.get(User, issued.user_id)
        assert user.credit_balance == 0 and user.email_verified_at is None
        raw = await latest_link(session, user.id, "verify")
        await auth.verify_email(AccountTokenIn(token=raw), session)
        assert user.credit_balance == 0 and user.email_verified_at is not None
        with pytest.raises(HTTPException) as replay:
            await auth.verify_email(AccountTokenIn(token=raw), session)
        assert replay.value.status_code == 400
        await session.rollback()
        await auth.forgot_password(AccountEmailIn(email=email), session)
        reset = await latest_link(session, issued.user_id, "reset")
        await auth.reset_password(PasswordResetIn(token=reset, password="Changed123"), session)
        with pytest.raises(HTTPException) as old_session:
            await deps.get_current_user_id("Bearer " + issued.access_token, session)
        assert old_session.value.status_code == 401
        logged_in = await auth.login(LoginIn(email=email, password="Changed123"), session)
        assert await deps.get_current_user_id("Bearer " + logged_in.access_token, session) == issued.user_id


@pytest.mark.asyncio
async def test_expired_verification_and_gmail_accounts_without_bonus(database):
    local = uuid.uuid4().hex
    async with database() as session:
        first = await auth.register(RegisterIn(email=f"{local}@gmail.com", password="Original123"), session)
        raw = await latest_link(session, first.user_id, "verify")
        await auth.verify_email(AccountTokenIn(token=raw), session)
        second = await auth.register(RegisterIn(email=f"{local}+two@gmail.com", password="Original123"), session)
        raw2 = await latest_link(session, second.user_id, "verify")
        await auth.verify_email(AccountTokenIn(token=raw2), session)
        assert (await session.get(User, second.user_id)).credit_balance == 0
        third = await auth.register(RegisterIn(email=f"expired-{local}@example.com", password="Original123"), session)
        raw3 = await latest_link(session, third.user_id, "verify")
        token = (await session.execute(select(AccountToken).where(AccountToken.user_id == third.user_id))).scalar_one()
        token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        with pytest.raises(HTTPException):
            await verify_account(session, raw3)


async def make_orders(database, balance=1, count=2):
    async with database() as session:
        user = User(email=f"buyer-{uuid.uuid4().hex}@example.com", credit_balance=balance,
                    email_verified_at=datetime.now(UTC))
        plan = Plan(slug=uuid.uuid4().hex, name="Test", price_cents=1990, video_count=1)
        session.add_all([user, plan])
        await session.flush()
        ids = []
        for _ in range(count):
            order = Order(user_id=user.id, plan_id=plan.id, total_cents=1990, status=OrderStatus.DRAFT)
            session.add(order)
            await session.flush()
            session.add(OrderItem(order_id=order.id, sequence=1, character_ids=["bluey"]))
            ids.append(order.id)
        await session.commit()
        return user.id, ids


@pytest.mark.asyncio
async def test_website_delivery_download_without_whatsapp_and_while_paused(database, monkeypatch):
    from app.db.models import Delivery, DeliveryChannel
    from app.services import delivery
    user_id, ids = await make_orders(database, count=1)
    monkeypatch.setattr(get_settings(), "evolution_api_url", None)
    monkeypatch.setattr(get_settings(), "evolution_api_key", None)
    monkeypatch.setattr(orders.storage, "presigned_get_url", lambda *args, **kwargs: "https://test.invalid/video.mp4")
    async with database() as session:
        order = await session.get(Order, ids[0])
        item = (await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalar_one()
        order.status = OrderStatus.READY
        item.status = OrderItemStatus.READY
        item.video_s3_key = "offline/video.mp4"
        await session.commit()
        first = await delivery.deliver_order(session, order.id)
        await session.commit()
        second = await delivery.deliver_order(session, order.id)
        await session.commit()
        assert first[0].id == second[0].id
        records = (await session.execute(select(Delivery).where(Delivery.order_id == order.id))).scalars().all()
        assert len(records) == 1 and records[0].channel == DeliveryChannel.ACCOUNT
        assert order.status == OrderStatus.DELIVERED
        monkeypatch.setattr(get_settings(), "worker_enabled", False)
        assert await orders.download_item(order.id, item.id, user_id, [], session) == {"url": "https://test.invalid/video.mp4"}
        assert await orders.download_item(order.id, item.id, None, [order.id], session) == {"url": "https://test.invalid/video.mp4"}
        with pytest.raises(HTTPException) as denied:
            await orders.download_item(order.id, item.id, None, [], session)
        assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_historical_outbox_and_recovery_records_unchanged(database, monkeypatch):
    _, ids = await make_orders(database, count=1)
    async with database() as session:
        order = await session.get(Order, ids[0])
        order.status = OrderStatus.PAID
        order.created_at = datetime(2020, 1, 1, tzinfo=UTC)
        item = (await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalar_one()
        item.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
        await outbox.record_video_jobs(session, order, [item.id])
        await session.commit()
        job = (await session.execute(select(OutboxJob).where(OutboxJob.order_id == order.id))).scalar_one()
        job_id, item_id = job.id, item.id
    monkeypatch.setattr(get_settings(), "processing_start_at", datetime.now(UTC))
    monkeypatch.setattr(outbox, "session_scope", database)
    monkeypatch.setattr(worker_recovery, "session_scope", database)
    pool = AsyncMock()
    monkeypatch.setattr(outbox, "get_pool", pool)
    await outbox.dispatch_outbox()
    await worker_recovery.recover_interrupted_jobs()
    async with database() as session:
        job = await session.get(OutboxJob, job_id)
        item = await session.get(OrderItem, item_id)
        assert job.attempts == 0 and job.delivered_at is None
        assert item.status == OrderItemStatus.PENDING and item.attempts == 0
        assert (await session.get(Order, ids[0])).status == OrderStatus.PAID
    pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_draft_mutation_waits_for_checkout_lock_and_rechecks_status(database):
    user_id, ids = await make_orders(database, count=1)
    async with database() as checkout_session:
        order = await orders._load_order(checkout_session, ids[0], user_id=user_id, guest_order_ids=[], lock=True)
        item_id = order.items[0].id
        async def delete_draft():
            async with database() as session:
                with pytest.raises(HTTPException) as blocked:
                    await orders.delete_item(ids[0], item_id, user_id, [], session)
                assert blocked.value.status_code == 409
        mutation = asyncio.create_task(delete_draft())
        await asyncio.sleep(0.05)
        assert not mutation.done()
        order.status = OrderStatus.PAID
        await checkout_session.commit()
        await asyncio.wait_for(mutation, timeout=5)
    async with database() as session:
        assert await session.get(OrderItem, item_id) is not None


@pytest.mark.asyncio
async def test_customer_media_and_admin_preview_are_separate(database, monkeypatch):
    from app.api.v1 import media
    from app.core import security
    _, ids = await make_orders(database, count=1)
    async with database() as session:
        order = await session.get(Order, ids[0])
        order.status = OrderStatus.PAID
        item = (await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalar_one()
        item.status = OrderItemStatus.AWAITING_APPROVAL
        item.video_s3_key = "preview.mp4"
        item_id = item.id
        await session.commit()
    monkeypatch.setattr(media, "session_scope", database)
    monkeypatch.setattr(media.storage, "download_bytes", lambda key: b"offline-video")
    with pytest.raises(HTTPException) as unavailable:
        await media.proxy_media(security.sign_media_token(item_id))
    assert unavailable.value.status_code == 404
    preview_token = security.sign_media_preview_token(item_id)
    assert security.verify_media_token(preview_token) is None
    assert (await media.proxy_media(preview_token)).body == b"offline-video"
    async with database() as session:
        (await session.get(Order, ids[0])).status = OrderStatus.DELIVERED
        (await session.get(OrderItem, item_id)).status = OrderItemStatus.READY
        await session.commit()
    monkeypatch.setattr(get_settings(), "worker_enabled", False)
    assert (await media.proxy_media(security.sign_media_token(item_id))).body == b"offline-video"


@pytest.mark.asyncio
async def test_concurrent_redemption_cannot_overspend(database):
    user_id, order_ids = await make_orders(database)
    async def redeem(order_id):
        async with database() as session:
            try:
                await orders.checkout(order_id, CardCheckoutIn(use_credits=True), user_id, [], None, session)
                return 200
            except HTTPException as error:
                await session.rollback()
                return error.status_code
    results = await asyncio.gather(*(redeem(order_id) for order_id in order_ids))
    assert sorted(results) == [200, 402]
    async with database() as session:
        assert (await session.get(User, user_id)).credit_balance == 0
        txs = (await session.execute(select(CreditTransaction).where(CreditTransaction.user_id == user_id))).scalars().all()
        assert len(txs) == 1 and txs[0].amount == -1
        jobs = (await session.execute(select(OutboxJob).where(OutboxJob.order_id.in_(order_ids)))).scalars().all()
        assert len(jobs) == 1 and jobs[0].delivered_at is None


@pytest.mark.asyncio
async def test_concurrent_awards_and_refund_replay(database):
    user_id, order_ids = await make_orders(database, balance=0, count=1)
    async def award():
        async with database() as session:
            order = await session.get(Order, order_ids[0])
            await grant_paid_order_credit(session, order)
            await session.commit()
    await asyncio.gather(award(), award())
    async with database() as session:
        user = await session.get(User, user_id)
        assert user.credit_balance == 0
        # Preserve refund coverage for rewards issued before the no-free-credit policy.
        session.add(CreditTransaction(user_id=user_id, order_id=order_ids[0],
            kind=CreditTransactionType.PURCHASE, amount=1, balance_after=1, debt_after=0))
        await session.commit()
        order = await session.get(Order, order_ids[0])
        await reverse_order_credits(session, order)
        await session.commit()
        await reverse_order_credits(session, order)
        await session.commit()
        assert user.credit_balance == 0 and user.credit_debt == 1
        refunds = (await session.execute(select(CreditTransaction).where(
            CreditTransaction.order_id == order.id, CreditTransaction.kind == CreditTransactionType.REFUND,
        ))).scalars().all()
        assert len(refunds) == 1 and refunds[0].amount == -1


@pytest.mark.asyncio
async def test_outbox_failure_persists_retry_and_success_scrubs_email(database, monkeypatch):
    async with database() as session:
        # Other cases enqueue work; keep it outside this dispatcher's test batch.
        await session.execute(update(OutboxJob).where(OutboxJob.delivered_at.is_(None)).values(
            available_at=datetime.now(UTC) + timedelta(days=1),
        ))
        job = OutboxJob(dedup_key=uuid.uuid4().hex, kind="account_email",
                        payload={"to": "test@example.com", "subject": "Test", "body": "secret-link"})
        session.add(job)
        await session.commit()
        job_id = job.id
    monkeypatch.setattr(outbox, "session_scope", database)
    monkeypatch.setattr(outbox, "get_pool", AsyncMock(return_value=SimpleNamespace(enqueue_job=AsyncMock())))
    monkeypatch.setattr(outbox, "send_account_email", AsyncMock(side_effect=ConnectionError("unavailable")))
    await outbox.dispatch_outbox()
    async with database() as session:
        job = await session.get(OutboxJob, job_id)
        assert job.delivered_at is None and job.attempts == 1
        assert job.available_at > datetime.now(UTC)
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    monkeypatch.setattr(outbox, "send_account_email", AsyncMock())
    await outbox.dispatch_outbox()
    async with database() as session:
        job = await session.get(OutboxJob, job_id)
        assert job.delivered_at is not None and job.payload == {}


@pytest.mark.asyncio
async def test_refunds_only_cancel_funding_payment_and_full_amount(database, monkeypatch):
    from starlette.requests import Request

    user_id, ids = await make_orders(database, balance=0, count=1)
    async with database() as session:
        order = await session.get(Order, ids[0])
        order.status = OrderStatus.PAID
        user = await session.get(User, user_id)
        user.credit_balance = 1
        session.add(CreditTransaction(user_id=user_id, order_id=order.id,
            kind=CreditTransactionType.PURCHASE, amount=1, balance_after=1, debt_after=0))
        funding = Payment(order_id=order.id, provider="mercadopago", provider_id=uuid.uuid4().hex,
                          status=PaymentStatus.APPROVED, amount_cents=1990, funds_order=True)
        duplicate = Payment(order_id=order.id, provider="mercadopago", provider_id=uuid.uuid4().hex,
                            status=PaymentStatus.APPROVED, amount_cents=1990)
        session.add_all([funding, duplicate])
        await session.commit()
        funding_id, duplicate_id = funding.provider_id, duplicate.provider_id
    monkeypatch.setattr(payments.mercadopago_client, "verify_webhook_signature", lambda **kw: True)

    async def notify(provider_id, refunded):
        details = {"id": provider_id, "external_reference": f"web_{ids[0]}",
                   "status": "refunded", "amount": 19.90, "amount_refunded": refunded}
        monkeypatch.setattr(payments.mercadopago_client, "get_payment_details", AsyncMock(return_value=details))
        request = Request({"type": "http", "headers": [], "query_string": b""})
        request._json = {"type": "payment", "data": {"id": provider_id}}
        async with database() as session:
            await payments.mercadopago_webhook(request, session)

    await notify(duplicate_id, 19.90)
    await notify(funding_id, 5)
    async with database() as session:
        assert (await session.get(Order, ids[0])).status == OrderStatus.PAID
        assert (await session.get(User, user_id)).credit_balance == 1
    await notify(funding_id, 19.90)
    await notify(funding_id, 19.90)
    async with database() as session:
        assert (await session.get(Order, ids[0])).status == OrderStatus.REFUNDED
        assert (await session.get(User, user_id)).credit_balance == 0


@pytest.mark.asyncio
async def test_recovery_requeues_stale_work_not_active_or_refunded_work(database, monkeypatch):
    _, ids = await make_orders(database, balance=0, count=4)
    async with database() as session:
        for index, order_id in enumerate(ids):
            order = await session.get(Order, order_id)
            order.status = OrderStatus.REFUNDED if index == 2 else OrderStatus.PAID
            item = (await session.execute(select(OrderItem).where(OrderItem.order_id == order_id))).scalar_one()
            item.status = OrderItemStatus.COMPOSITING
            item.updated_at = datetime.now(UTC) - timedelta(minutes=90 if index != 1 else 1)
            if index == 3:
                item.status = OrderItemStatus.RENDERING
                item.video_s3_key = "web/existing-video.mp4"
        await session.commit()
    monkeypatch.setattr(worker_recovery, "session_scope", database)
    await worker_recovery.recover_interrupted_jobs()
    await worker_recovery.recover_interrupted_jobs()
    async with database() as session:
        jobs = (await session.execute(select(OutboxJob).where(OutboxJob.order_id.in_(ids)))).scalars().all()
        assert sorted((job.order_id, job.kind) for job in jobs) == [(ids[0], "process_item"), (ids[3], "generate_video")]
        items = (await session.execute(select(OrderItem).where(OrderItem.order_id.in_(ids)).order_by(OrderItem.order_id))).scalars().all()
        assert [item.status for item in items] == [OrderItemStatus.PENDING, OrderItemStatus.COMPOSITING,
                                                 OrderItemStatus.COMPOSITING, OrderItemStatus.APPROVED]


@pytest.mark.asyncio
async def test_refund_blocks_delivery_and_admin_resurrection(database, monkeypatch):
    from app.api.v1 import admin
    from app.services import delivery

    _, ids = await make_orders(database, count=1)
    async with database() as session:
        order = await session.get(Order, ids[0])
        order.status = OrderStatus.REFUNDED
        item = (await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalar_one()
        item.status = OrderItemStatus.READY
        await session.commit()
        assert await delivery.deliver_order(session, order.id) == []
        for endpoint in [admin.deliver_order_admin, admin.approve_order]:
            with pytest.raises(HTTPException) as error:
                await endpoint(order.id, session)
            assert error.value.status_code == 409
        assert order.status == OrderStatus.REFUNDED


@pytest.mark.asyncio
async def test_admin_retry_renews_budget_and_phase_two_identity(database, monkeypatch):
    from app.api.v1 import admin
    from app.workers import batch_runner

    _, ids = await make_orders(database, count=1)
    async with database() as session:
        order = await session.get(Order, ids[0])
        order.status = OrderStatus.PAID
        item = (await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalar_one()
        item.character_ids = ["bluey", "bingo"]
        item.status = OrderItemStatus.FAILED
        item.attempts = 5
        await session.commit()
        item_id = item.id
        await admin.retry_item(item_id, session)
        assert item.attempts == 0
    monkeypatch.setattr(batch_runner, "session_scope", database)
    monkeypatch.setattr(batch_runner, "_load_characters", AsyncMock(return_value=[]))
    monkeypatch.setattr(batch_runner, "_resolve_composite_url", AsyncMock(return_value=("mock", "web/composite.png")))
    await batch_runner.process_item_phase1({"job_id": "first-retry"}, item_id)
    async with database() as session:
        item = await session.get(OrderItem, item_id)
        item.status = OrderItemStatus.FAILED
        item.attempts = 5
        await session.commit()
        await admin.retry_item(item_id, session)
    await batch_runner.process_item_phase1({"job_id": "second-retry"}, item_id)
    async with database() as session:
        jobs = (await session.execute(select(OutboxJob).where(
            OutboxJob.order_id == ids[0], OutboxJob.kind == "generate_video",
        ))).scalars().all()
        assert len(jobs) == 2 and jobs[0].dedup_key != jobs[1].dedup_key


@pytest.mark.asyncio
async def test_composite_cache_persists_slug_arrays(database):
    async with database() as session:
        entry = CompositeFrameCache(sha=uuid.uuid4().hex, s3_key="web/test-composite.png",
                                    character_ids=["bluey", "bingo"])
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        assert entry.character_ids == ["bluey", "bingo"]


async def google_attempt(database, monkeypatch, subject, email, link_user_id=None, authoritative=True):
    from app.services import google_auth
    monkeypatch.setattr(get_settings(), "google_client_id", "test.apps.googleusercontent.com")
    async with database() as session:
        challenge = await google_auth.issue_challenge(session)
        claims = {"sub": subject, "email": email, "email_verified": True, "nonce": challenge["nonce"]}
        if authoritative and not email.endswith("@gmail.com"):
            claims["hd"] = email.split("@")[1]
        monkeypatch.setattr(google_auth, "verify_credential", AsyncMock(return_value=claims))
        user = await google_auth.authenticate(session, "mock-google-credential", challenge["nonce"], link_user_id,
                                              expected_token_version=0 if link_user_id is not None else None)
        return user.id, challenge["nonce"]


@pytest.mark.asyncio
async def test_google_signup_login_subject_and_nonce_replay(database, monkeypatch):
    from app.services import google_auth
    subject = uuid.uuid4().hex
    email = subject + "@gmail.com"
    user_id, nonce = await google_attempt(database, monkeypatch, subject, email)
    async with database() as session:
        user = await session.get(User, user_id)
        assert user.credit_balance == 0 and user.email_verified_at is not None
        assert user.password_hash is None and user.google_sub == subject
        with pytest.raises(HTTPException) as replay:
            await google_auth.authenticate(session, "mock-google-credential", nonce)
        assert replay.value.status_code == 401
    again, _ = await google_attempt(database, monkeypatch, subject, "changed-" + email)
    assert again == user_id
    async with database() as session:
        user = await session.get(User, user_id)
        assert user.email == email and user.credit_balance == 0


@pytest.mark.asyncio
async def test_google_cannot_implicitly_link_password_account(database, monkeypatch):
    email = uuid.uuid4().hex + "@gmail.com"
    async with database() as session:
        issued = await auth.register(RegisterIn(email=email, password="Original123"), session)
        raw = await latest_link(session, issued.user_id, "verify")
        await auth.verify_email(AccountTokenIn(token=raw), session)
    subject = uuid.uuid4().hex
    with pytest.raises(HTTPException) as conflict:
        await google_attempt(database, monkeypatch, subject, email)
    assert conflict.value.status_code == 409
    linked, _ = await google_attempt(database, monkeypatch, subject, email, link_user_id=issued.user_id)
    assert linked == issued.user_id
    async with database() as session:
        user = await session.get(User, linked)
        assert user.credit_balance == 0 and user.google_sub == subject
        assert user.password_hash is not None


@pytest.mark.asyncio
async def test_google_alias_signup_cannot_duplicate_bonus(database, monkeypatch):
    local = uuid.uuid4().hex
    first, _ = await google_attempt(database, monkeypatch, uuid.uuid4().hex, local + "@gmail.com")
    second, _ = await google_attempt(database, monkeypatch, uuid.uuid4().hex, local + "+alias@gmail.com")
    async with database() as session:
        assert (await session.get(User, first)).credit_balance == 0
        assert (await session.get(User, second)).credit_balance == 0


@pytest.mark.asyncio
async def test_google_third_party_email_needs_our_verification(database, monkeypatch):
    email = uuid.uuid4().hex + "@example.com"
    user_id, _ = await google_attempt(database, monkeypatch, uuid.uuid4().hex, email, authoritative=False)
    async with database() as session:
        user = await session.get(User, user_id)
        assert user.email_verified_at is None and user.credit_balance == 0
        raw = await latest_link(session, user_id, "verify")
        await auth.verify_email(AccountTokenIn(token=raw), session)
        assert user.credit_balance == 0


@pytest.mark.asyncio
async def test_google_http_contract_and_authenticated_link(database, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.db.session import get_session
    from app.services import google_auth

    monkeypatch.setattr(get_settings(), "google_client_id", "test.apps.googleusercontent.com")
    app = FastAPI()
    app.include_router(auth.router)
    async def sessions():
        async with database() as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.get('/auth/google/config')).json() == {"enabled": True}
        response = await client.post('/auth/google/challenge')
        nonce = response.json()["nonce"]
        assert response.headers["cache-control"] == "no-store"
        claims = {"sub": uuid.uuid4().hex, "email": uuid.uuid4().hex + "@gmail.com", "email_verified": True}
        monkeypatch.setattr(google_auth, "verify_credential", AsyncMock(return_value=claims))
        payload = {"credential": "mock-google-credential", "nonce": nonce}
        assert (await client.post('/auth/google/link', json=payload)).status_code == 401
        result = await client.post('/auth/google/login', json=payload)
        assert result.status_code == 200
        token = result.json()["access_token"]
        me = (await client.get('/auth/me', headers={"Authorization": "Bearer " + token})).json()
        assert me["google_linked"] and me["email_verified"] and me["credit_balance"] == 0
        assert (await client.post('/auth/google/login', json=payload)).status_code == 401


@pytest.mark.asyncio
async def test_concurrent_google_signup_creates_one_account_without_bonus(database, monkeypatch):
    from app.services import google_auth

    subject, email = uuid.uuid4().hex, uuid.uuid4().hex + "@gmail.com"
    monkeypatch.setattr(get_settings(), "google_client_id", "test.apps.googleusercontent.com")
    monkeypatch.setattr(google_auth, "verify_credential", AsyncMock(return_value={
        "sub": subject, "email": email, "email_verified": True,
    }))
    async def sign_in():
        async with database() as session:
            challenge = await google_auth.issue_challenge(session)
            user = await google_auth.authenticate(session, "mock-google-credential", challenge["nonce"])
            return user.id
    ids = await asyncio.gather(sign_in(), sign_in())
    assert ids[0] == ids[1]
    async with database() as session:
        user = await session.get(User, ids[0])
        assert user.credit_balance == 0
        txs = (await session.execute(select(CreditTransaction).where(CreditTransaction.user_id == user.id))).scalars().all()
        assert len(txs) == 0


@pytest.mark.asyncio
async def test_google_failed_link_consumes_nonce(database, monkeypatch):
    from app.services import google_auth
    email = uuid.uuid4().hex + "@gmail.com"
    async with database() as session:
        issued = await auth.register(RegisterIn(email=email, password="Original123"), session)
    monkeypatch.setattr(get_settings(), "google_client_id", "test.apps.googleusercontent.com")
    async with database() as session:
        nonce = (await google_auth.issue_challenge(session))["nonce"]
        monkeypatch.setattr(google_auth, "verify_credential", AsyncMock(return_value={
            "sub": uuid.uuid4().hex, "email": email, "email_verified": True,
        }))
        with pytest.raises(HTTPException) as conflict:
            await google_auth.authenticate(session, "mock-google-credential", nonce)
        assert conflict.value.status_code == 409
        await session.rollback()
        with pytest.raises(HTTPException) as replay:
            await google_auth.authenticate(session, "mock-google-credential", nonce, link_user_id=issued.user_id)
        assert replay.value.status_code == 401


@pytest.mark.asyncio
async def test_google_link_requires_current_session_version_under_lock(database, monkeypatch):
    from app.services import google_auth
    email = uuid.uuid4().hex + "@gmail.com"
    async with database() as session:
        user = User(email=email, token_version=1)
        session.add(user)
        await session.commit()
        user_id = user.id
        monkeypatch.setattr(get_settings(), "google_client_id", "test.apps.googleusercontent.com")
        nonce = (await google_auth.issue_challenge(session))["nonce"]
        monkeypatch.setattr(google_auth, "verify_credential", AsyncMock(return_value={
            "sub": uuid.uuid4().hex, "email": email, "email_verified": True,
        }))
        with pytest.raises(HTTPException) as revoked:
            await google_auth.authenticate(session, "mock-google-credential", nonce,
                                           link_user_id=user_id, expected_token_version=0)
        assert revoked.value.status_code == 401


@pytest.mark.asyncio
async def test_google_concurrent_nonce_consumption_and_expiry(database, monkeypatch):
    from app.services import google_auth
    from app.services.account_tokens import token_digest

    monkeypatch.setattr(get_settings(), "google_client_id", "test.apps.googleusercontent.com")
    monkeypatch.setattr(google_auth, "verify_credential", AsyncMock(return_value={
        "sub": uuid.uuid4().hex, "email": uuid.uuid4().hex + "@gmail.com", "email_verified": True,
    }))
    async with database() as session:
        nonce = (await google_auth.issue_challenge(session))["nonce"]
    async def sign_in():
        async with database() as session:
            try:
                await google_auth.authenticate(session, "mock-google-credential", nonce)
                return 200
            except HTTPException as error:
                return error.status_code
    assert sorted(await asyncio.gather(sign_in(), sign_in())) == [200, 401]
    async with database() as session:
        expired = (await google_auth.issue_challenge(session))["nonce"]
        challenge = await session.get(GoogleChallenge, token_digest(expired))
        challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        with pytest.raises(HTTPException) as error:
            await google_auth.authenticate(session, "mock-google-credential", expired)
        assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_google_nonce_does_not_require_a_second_pool_connection(database, monkeypatch):
    from app.services import google_auth

    monkeypatch.setattr(get_settings(), "google_client_id", "test.apps.googleusercontent.com")
    monkeypatch.setattr(google_auth, "verify_credential", AsyncMock(return_value={
        "sub": uuid.uuid4().hex, "email": uuid.uuid4().hex + "@gmail.com", "email_verified": True,
    }))
    engine = create_async_engine(database.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            nonce = (await google_auth.issue_challenge(session))["nonce"]
            await session.execute(select(1))  # The request's preceding auth read.
            user = await google_auth.authenticate(session, "mock-google-credential", nonce)
            assert user.google_sub is not None
    finally:
        await engine.dispose()
