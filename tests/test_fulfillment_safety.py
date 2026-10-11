"""Offline regressions: no existing customer data or live provider calls."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.settings import Settings, get_settings
from app.db.models import DeliveryChannel, DeliveryStatus, OrderItemStatus, OrderStatus
from app.services import delivery, xai_client


@pytest.mark.asyncio
async def test_paused_tasks_do_not_open_database(monkeypatch):
    from app.services import worker_recovery
    from app.workers import batch_runner
    monkeypatch.setattr(get_settings(), "worker_enabled", False)
    db = MagicMock(side_effect=AssertionError("database must not be opened"))
    monkeypatch.setattr(batch_runner, "session_scope", db)
    monkeypatch.setattr(worker_recovery, "session_scope", db)
    await worker_recovery.recover_interrupted_jobs()
    await batch_runner.process_item_phase1({}, 1)
    await batch_runner.generate_video_for_approved_item({}, 1)
    await batch_runner.run_batch({}, 1)
    await batch_runner.collector_check({})
    db.assert_not_called()
    with pytest.raises(RuntimeError, match="paused"):
        await batch_runner.WorkerSettings.on_startup({})
    assert batch_runner.AccountWorkerSettings.functions == [batch_runner.dispatch_outbox]
    assert batch_runner.AccountWorkerSettings.queue_name != "arq:queue"


@pytest.mark.asyncio
async def test_historical_delivery_is_unchanged(monkeypatch):
    monkeypatch.setattr(get_settings(), "processing_start_at", datetime.now(UTC))
    order = SimpleNamespace(created_at=datetime(2020, 1, 1, tzinfo=UTC), status=OrderStatus.READY)
    session = MagicMock(execute=AsyncMock(return_value=result(order)))
    assert await delivery.deliver_order(session, 1) == []
    session.add.assert_not_called()
    assert order.status == OrderStatus.READY


@pytest.mark.asyncio
async def test_transient_polling_retries(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.side_effect = [httpx.Response(503), httpx.Response(200, json={"status": "pending"})]
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", lambda **kw: client)
    sleep = AsyncMock()
    monkeypatch.setattr(xai_client.asyncio, "sleep", sleep)
    assert await xai_client.get_video("test") == {"status": "pending"}
    assert client.get.await_count == 2
    sleep.assert_awaited_once()


def test_corrupt_reference_image_is_rejected(monkeypatch):
    from PIL import UnidentifiedImageError

    from app.services import storage
    monkeypatch.setattr(storage, "download_bytes", lambda key: b"not an image")
    with pytest.raises(UnidentifiedImageError):
        storage.get_image_as_data_url("reference.png")


@pytest.mark.asyncio
async def test_missing_character_rejected_before_payment():
    from fastapi import HTTPException

    from app.services.catalog_validation import validate_order_assets
    order = SimpleNamespace(items=[SimpleNamespace(character_ids=["missing"])],
                            plan=SimpleNamespace(max_characters_per_video=2))
    session = MagicMock(execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda: result([]).scalars())))
    with pytest.raises(HTTPException) as error:
        await validate_order_assets(session, order)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_unavailable_asset_fails_closed(monkeypatch):
    from fastapi import HTTPException

    from app.services import catalog_validation
    order = SimpleNamespace(items=[SimpleNamespace(character_ids=["bluey"])],
                            plan=SimpleNamespace(max_characters_per_video=2))
    rows = [{"slug": "bluey", "thumbnail_s3_key": "reference.png"}]
    session = MagicMock(execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda: result(rows).scalars())))
    monkeypatch.setattr(catalog_validation.storage, "get_image_as_data_url", MagicMock(side_effect=OSError("private")))
    with pytest.raises(HTTPException) as error:
        await catalog_validation.validate_order_assets(session, order)
    assert error.value.status_code == 503 and "private" not in error.value.detail


@pytest.mark.asyncio
async def test_download_does_not_expose_unapproved_video(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import orders
    item = SimpleNamespace(id=2, status=OrderItemStatus.AWAITING_APPROVAL, video_s3_key="private.mp4")
    order = SimpleNamespace(status=OrderStatus.PAID, items=[item])
    monkeypatch.setattr(orders, "_load_order", AsyncMock(return_value=order))
    signer = MagicMock(return_value="https://test.invalid/video")
    monkeypatch.setattr(orders.storage, "presigned_get_url", signer)
    with pytest.raises(HTTPException) as error:
        await orders.download_item(1, 2, user_id=1, guest_order_ids=[], session=MagicMock())
    assert error.value.status_code == 404
    signer.assert_not_called()


@pytest.mark.asyncio
async def test_manual_delivery_historical_order_is_unchanged(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import admin
    monkeypatch.setattr(get_settings(), "processing_start_at", datetime.now(UTC))
    order = SimpleNamespace(status=OrderStatus.PAID, created_at=datetime(2020, 1, 1, tzinfo=UTC),
                            items=[SimpleNamespace(status=OrderItemStatus.READY, video_s3_key="old.mp4")])
    session = MagicMock(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: order)))
    session.commit = AsyncMock()
    monkeypatch.setattr(delivery, "deliver_order", AsyncMock(return_value=[]))
    with pytest.raises(HTTPException) as error:
        await admin.deliver_order_admin(1, session)
    assert error.value.status_code == 503 and order.status == OrderStatus.PAID
    session.commit.assert_not_called()


def result(value):
    return SimpleNamespace(scalar_one=lambda: value, scalars=lambda: SimpleNamespace(all=lambda: value))


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [[], [SimpleNamespace(status=OrderItemStatus.FAILED, video_s3_key=None)],
    [SimpleNamespace(status=OrderItemStatus.READY, video_s3_key=None)]])
async def test_incomplete_order_cannot_be_delivered(items):
    order = SimpleNamespace(id=1, status=OrderStatus.READY, created_at=datetime.now(UTC),
                            plan=SimpleNamespace(video_count=1), guest_email=None, guest_phone=None)
    session = MagicMock(execute=AsyncMock(side_effect=[result(order), result(items), result([])]))
    assert await delivery.deliver_order(session, 1) == []
    assert order.status == OrderStatus.READY
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_website_delivery_does_not_require_whatsapp(monkeypatch):
    order = SimpleNamespace(id=1, status=OrderStatus.READY, created_at=datetime.now(UTC),
                            plan=SimpleNamespace(video_count=1), delivered_at=None, guest_email=None, guest_phone="test")
    item = SimpleNamespace(status=OrderItemStatus.READY, video_s3_key="web/orders/1/video.mp4")
    session = MagicMock(execute=AsyncMock(side_effect=[result(order), result([item]), result([])]))
    monkeypatch.setattr(get_settings(), "evolution_api_url", "https://unconfigured.invalid")
    monkeypatch.setattr(get_settings(), "evolution_api_key", "unused")
    send = AsyncMock()
    monkeypatch.setattr(delivery, "_send_whatsapp_videos", send)
    records = await delivery.deliver_order(session, 1)
    assert order.status == OrderStatus.DELIVERED
    assert len(records) == 1 and records[0].channel == DeliveryChannel.ACCOUNT
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_website_delivery_reuses_record():
    order = SimpleNamespace(id=1, status=OrderStatus.DELIVERED, created_at=datetime.now(UTC),
                            plan=SimpleNamespace(video_count=1), delivered_at=None, guest_email=None, guest_phone=None)
    item = SimpleNamespace(status=OrderItemStatus.READY, video_s3_key="web/orders/1/video.mp4")
    existing = SimpleNamespace(channel=DeliveryChannel.ACCOUNT, status=DeliveryStatus.SUCCEEDED)
    session = MagicMock(execute=AsyncMock(side_effect=[result(order), result([item]), result([existing])]))
    assert await delivery.deliver_order(session, 1) == [existing]
    session.add.assert_not_called()


def test_processing_and_whatsapp_are_safe_by_default():
    for field in ["worker_enabled", "recovery_enabled", "whatsapp_delivery_enabled"]:
        definition = Settings.model_fields.get(field)
        assert definition is not None and definition.default is False


def test_enabling_processing_requires_aware_cutoff():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(STOREFRONT_WORKER_ENABLED=True, STOREFRONT_PROCESSING_START_AT=None)
    with pytest.raises(ValidationError):
        Settings(STOREFRONT_WORKER_ENABLED=True, STOREFRONT_PROCESSING_START_AT="2026-10-11T15:00:00")


@pytest.mark.asyncio
async def test_whatsapp_failure_does_not_fail_website_delivery(monkeypatch):
    order = SimpleNamespace(id=1, status=OrderStatus.READY, created_at=datetime.now(UTC),
                            plan=SimpleNamespace(video_count=1), delivered_at=None, guest_email=None, guest_phone="test")
    item = SimpleNamespace(status=OrderItemStatus.READY, video_s3_key="video.mp4")
    session = MagicMock(execute=AsyncMock(side_effect=[result(order), result([item]), result([])]))
    session.commit = AsyncMock()
    settings = get_settings()
    for key, value in {"whatsapp_delivery_enabled": True, "evolution_api_url": "https://test.invalid",
                       "evolution_api_key": "test", "evolution_instance_name": "test"}.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(delivery, "_send_whatsapp_videos", AsyncMock(side_effect=RuntimeError("private URL")))
    records = await delivery.deliver_order(session, 1)
    assert order.status == OrderStatus.DELIVERED
    assert records[0].status == DeliveryStatus.SUCCEEDED
    assert records[1].status == DeliveryStatus.FAILED and records[1].error == "RuntimeError"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"not a video"])
async def test_invalid_video_download_is_rejected(monkeypatch, body):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(200, content=body, request=httpx.Request("GET", "https://test.invalid/file"))
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", lambda **kw: client)
    with pytest.raises(xai_client.XaiError):
        await xai_client.download_video("https://test.invalid/file?token=private")


@pytest.mark.asyncio
async def test_video_download_error_is_sanitized(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(403, request=httpx.Request("GET", "https://test.invalid/file?token=private"))
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", lambda **kw: client)
    with pytest.raises(xai_client.XaiError) as error:
        await xai_client.download_video("https://test.invalid/file?token=private")
    assert "403" in str(error.value) and "private" not in str(error.value)


@pytest.mark.asyncio
async def test_bounded_boxes_without_video_track_rejected(monkeypatch):
    import struct
    body = b"".join(struct.pack(">I4s", 12, kind) + b"test" for kind in [b"ftyp", b"moov", b"mdat"])
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(200, content=body)
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", lambda **kw: client)
    with pytest.raises(xai_client.XaiError):
        await xai_client.download_video("https://test.invalid/file")
    client.get.return_value = httpx.Response(200, content=body[:-1])
    with pytest.raises(xai_client.XaiError):
        await xai_client.download_video("https://test.invalid/file")


@pytest.mark.asyncio
async def test_playable_mp4_download(monkeypatch, tmp_path):
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg required for the real MP4 fixture")
    path = tmp_path / "sample.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=16x16:d=0.2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
    body = path.read_bytes()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(200, content=body)
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", lambda **kw: client)
    assert await xai_client.download_video("https://test.invalid/file") == body


@pytest.mark.asyncio
async def test_composite_image_download_is_not_validated_as_video(monkeypatch):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buf, format="PNG")
    body = buf.getvalue()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(200, content=body)
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", lambda **kw: client)
    assert await xai_client.download_image("https://test.invalid/image") == body


@pytest.mark.asyncio
async def test_quota_exhaustion_is_not_retried(monkeypatch):
    response = httpx.Response(429, json={"error": "Your team has used all available credits"})
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", lambda **kw: client)
    sleep = AsyncMock()
    monkeypatch.setattr(xai_client.asyncio, "sleep", sleep)
    with pytest.raises(xai_client.XaiError):
        await xai_client.start_image_to_video(prompt="test", image_url="data:image/png;base64,test")
    assert client.post.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_polling_errors_are_sanitized(monkeypatch):
    response = httpx.Response(400, json={"code": "invalid-argument", "error": "secret-token private prompt"},
                              request=httpx.Request("GET", "https://api.invalid/videos/private?token=secret"))
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = response
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", lambda **kw: client)
    with pytest.raises(xai_client.XaiError) as error:
        await xai_client.get_video("private")
    assert "400" in str(error.value) and "invalid-argument" in str(error.value)
    assert "secret" not in str(error.value) and "private" not in str(error.value)
