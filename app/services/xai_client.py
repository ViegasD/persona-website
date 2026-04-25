"""xAI Grok Imagine Video client — image-to-video for the cameo videos.

Two-step async flow:

* ``POST /v1/videos/generations`` returns ``{request_id}``.
* ``GET /v1/videos/{request_id}`` returns ``{status, video: {url, duration}}``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from app.core.settings import get_settings


class XaiError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    s = get_settings()
    if not s.xai_api_key:
        raise XaiError("XAI_API_KEY is not configured")
    return {
        "Authorization": f"Bearer {s.xai_api_key}",
        "Content-Type": "application/json",
    }


async def start_image_to_video(
    *,
    prompt: str,
    image_url: str,
    duration: int = 10,
    aspect_ratio: str = "9:16",
    resolution: str = "720p",
) -> str:
    s = get_settings()
    url = f"{s.xai_api_base.rstrip('/')}/videos/generations"
    body: dict[str, Any] = {
        "model": s.xai_video_model,
        "prompt": prompt,
        "image_url": image_url,
        "duration": max(1, min(duration, 15)),
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    logger.info(
        "xAI start_image_to_video | model={model} duration={dur}s aspect={ar} "
        "prompt_len={plen} image_url_prefix={img_prefix}",
        model=body["model"],
        dur=body["duration"],
        ar=body["aspect_ratio"],
        plen=len(prompt),
        img_prefix=(image_url[:80] + "…") if len(image_url) > 80 else image_url,
    )
    logger.debug("xAI prompt text:\n{p}", p=prompt)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=body, headers=_headers())
        if resp.status_code >= 400:
            raise XaiError(f"xAI start failed {resp.status_code}: {resp.text}")
        data = resp.json()
    rid = data.get("request_id")
    if not rid:
        raise XaiError(f"xAI start returned no request_id: {data}")
    logger.info("xAI request_id={rid}", rid=rid)
    return str(rid)


async def get_video(request_id: str) -> dict[str, Any]:
    s = get_settings()
    url = f"{s.xai_api_base.rstrip('/')}/videos/{request_id}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def wait_for_video(
    request_id: str, *, timeout_s: int = 1200, interval_s: float = 5.0
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        data = await get_video(request_id)
        status = (data.get("status") or "").lower()
        logger.info("xAI poll request_id={rid} status={s}", rid=request_id, s=status)
        if status == "done":
            logger.info("xAI video ready: {data}", data=data)
            return data
        if status in {"failed", "expired"}:
            raise XaiError(f"xAI video {request_id} {status}: {data}")
        if asyncio.get_event_loop().time() > deadline:
            raise XaiError(f"xAI video {request_id} timed out after {timeout_s}s")
        await asyncio.sleep(interval_s)


async def download_video(url: str) -> bytes:
    """Download the generated MP4 to memory (xAI URLs are short-lived)."""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content
