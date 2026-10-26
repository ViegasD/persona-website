"""Verify the raw REST contract without submitting billable generations."""

from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.core.settings import Settings
from app.services import xai_client


@pytest.mark.asyncio
async def test_reference_image_is_sent_using_rest_image_object(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={"request_id": "mock-job"})
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", lambda **kwargs: client)
    reference = "data:image/png;base64,catalog-reference"

    result = await xai_client.start_image_to_video(
        prompt="Animate this exact character", image_url=reference, duration=12,
    )

    assert result == "mock-job"
    body = client.post.call_args.kwargs["json"]
    assert body["image"] == {"url": reference}
    assert "image_url" not in body
    assert body["duration"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [0, 16])
async def test_duration_is_rejected_instead_of_silently_clamped(monkeypatch, duration):
    client_factory = AsyncMock()
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", client_factory)
    with pytest.raises(ValueError, match="between 1 and 15"):
        await xai_client.start_image_to_video(
            prompt="test", image_url="data:image/png;base64,test", duration=duration,
        )
    client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_missing_reference_cannot_fall_back_to_text_only(monkeypatch):
    client_factory = AsyncMock()
    monkeypatch.setattr(xai_client.httpx, "AsyncClient", client_factory)
    with pytest.raises(ValueError, match="reference image"):
        await xai_client.start_image_to_video(prompt="test", image_url=" ")
    client_factory.assert_not_called()


@pytest.mark.parametrize("duration", [0, 16])
def test_invalid_duration_configuration_fails_at_startup(duration):
    with pytest.raises(ValidationError):
        Settings(XAI_VIDEO_DURATION_SECONDS=duration)
