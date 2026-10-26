"""xAI Grok Imagine Video client — image-to-video for the cameo videos.

Two-step async flow:

* ``POST /v1/videos/generations`` returns ``{request_id}``.
* ``GET /v1/videos/{request_id}`` returns ``{status, video: {url, duration}}``.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from app.core.settings import get_settings

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 5.0  # seconds; doubles each attempt


class XaiError(RuntimeError):
    pass


def _error_data(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _response_error(resp: httpx.Response) -> XaiError:
    data = _error_data(resp)
    nested = data.get("error")
    code = data.get("code") or (nested.get("code") if isinstance(nested, dict) else None)
    safe_code = code if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", code) else "unknown"
    return XaiError(f"xAI HTTP {resp.status_code} ({safe_code})")


def _retryable(resp: httpx.Response) -> bool:
    if resp.status_code not in _RETRY_STATUSES:
        return False
    # Billing exhaustion is permanent until an operator changes the provider account.
    data = str(_error_data(resp)).lower()
    return not any(word in data for word in ("credits", "quota", "spending", "billing", "balance"))


def _delay(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(120, max(0, float(resp.headers["retry-after"])))
    except (KeyError, ValueError):
        return _RETRY_BASE_DELAY * (2 ** attempt)


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        raise XaiError("xAI returned invalid JSON") from None
    if not isinstance(data, dict):
        raise XaiError("xAI returned an invalid response")
    return data


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
    model: str | None = None,
) -> str:
    if not image_url.strip():
        raise ValueError("a reference image is required for image-to-video")
    if not 1 <= duration <= 15:
        raise ValueError("video duration must be between 1 and 15 seconds")
    s = get_settings()
    url = f"{s.xai_api_base.rstrip('/')}/videos/generations"
    body: dict[str, Any] = {
        "model": model or s.xai_video_model,
        "prompt": prompt,
        # REST uses an image object; image_url is the Python SDK argument.
        "image": {"url": image_url},
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    logger.info(
        "xAI start_image_to_video | model={model} duration={dur}s aspect={ar} "
        "prompt_len={plen}",
        model=body["model"],
        dur=body["duration"],
        ar=body["aspect_ratio"],
        plen=len(prompt),
    )
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=body, headers=_headers())
        except httpx.RequestError:
            # An ambiguous POST may already be billed; do not blindly resubmit it.
            raise XaiError("xAI generation connection failed; outcome unknown") from None
        if not _retryable(resp):
            if resp.status_code >= 400:
                raise _response_error(resp)
            break
        if attempt == _MAX_RETRIES - 1:
            raise _response_error(resp)
        delay = _delay(resp, attempt)
        logger.warning(
            "xAI start_image_to_video got {status}, retry {attempt}/{max} in {delay}s",
            status=resp.status_code, attempt=attempt + 1, max=_MAX_RETRIES, delay=delay,
        )
        await asyncio.sleep(delay)
    data = _json_object(resp)
    rid = data.get("request_id")
    if not rid:
        raise XaiError("xAI start returned no request_id")
    logger.info("xAI request_id={rid}", rid=rid)
    return str(rid)


async def get_video(request_id: str) -> dict[str, Any]:
    s = get_settings()
    url = f"{s.xai_api_base.rstrip('/')}/videos/{request_id}"
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(url, headers=_headers())
        except httpx.RequestError:
            if attempt == _MAX_RETRIES - 1:
                raise XaiError("xAI polling connection failed") from None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
            continue
        if _retryable(resp) and attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(_delay(resp, attempt))
            continue
        if resp.status_code >= 400:
            raise _response_error(resp)
        return _json_object(resp)
    raise XaiError("xAI polling failed")


async def wait_for_video(
    request_id: str, *, timeout_s: int = 1200, interval_s: float = 5.0
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        data = await get_video(request_id)
        status = (data.get("status") or "").lower()
        logger.info("xAI poll request_id={rid} status={s}", rid=request_id, s=status)
        if status == "done":
            logger.info("xAI video ready")
            return data
        if status in {"failed", "expired"}:
            raise XaiError(f"xAI video {status}")
        if asyncio.get_event_loop().time() > deadline:
            raise XaiError(f"xAI video {request_id} timed out after {timeout_s}s")
        await asyncio.sleep(interval_s)


async def _download_asset(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url)
    except httpx.RequestError:
        raise XaiError("xAI video download connection failed") from None
    if resp.status_code != 200:
        raise _response_error(resp)
    if not resp.content:
        raise XaiError("provider returned an empty download")
    return resp.content


def _validate_video(data: bytes, expected_duration: int | None = None) -> None:
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            path = Path(handle.name)
            handle.write(data)
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=width,height,nb_read_frames:format=duration", "-of", "json", str(path),
        ], capture_output=True, timeout=120, check=True)
        streams = json.loads(probe.stdout).get("streams", [])
        actual_duration = float(json.loads(probe.stdout).get("format", {}).get("duration", 0))
        if expected_duration is not None and abs(actual_duration - expected_duration) > 0.5:
            raise ValueError("unexpected video duration")
        if (not streams or int(streams[0].get("width", 0)) <= 0
                or int(streams[0].get("height", 0)) <= 0
                or int(streams[0].get("nb_read_frames", 0)) <= 0 or probe.stderr.strip()):
            raise ValueError("no valid video frames")
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise XaiError("video download is invalid or ffprobe is unavailable") from None
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


async def download_video(url: str, *, expected_duration: int | None = None) -> bytes:
    """Require a decodable video track before the caller uploads or marks READY."""
    data = await _download_asset(url)
    await asyncio.to_thread(_validate_video, data, expected_duration)
    return data


async def download_image(url: str) -> bytes:
    from PIL import Image
    data = await _download_asset(url)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, ValueError):
        raise XaiError("provider returned an invalid composite image") from None
    return data
