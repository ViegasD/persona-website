"""kie.ai client — used to combine N character reference images into one
composite "stage" frame using the nano-banana-pro (Gemini 3 Image) model.

Async job model: ``createTask`` returns a ``taskId`` that we poll on
``recordInfo`` until it becomes ``success`` / ``failed``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.settings import get_settings


class KieError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    s = get_settings()
    if not s.kie_api_key:
        raise KieError("KIE_API_KEY is not configured")
    return {
        "Authorization": f"Bearer {s.kie_api_key}",
        "Content-Type": "application/json",
    }


async def create_composite_task(
    *,
    prompt: str,
    image_urls: list[str],
    image_size: str = "9:16",
    output_format: str = "PNG",
) -> str:
    """Submit a nano-banana-pro composite task and return ``taskId``."""
    s = get_settings()
    url = f"{s.kie_api_base.rstrip('/')}/playground/createTask"
    body: dict[str, Any] = {
        "model": s.kie_nano_banana_model,
        "input": {
            "prompt": prompt,
            "image_urls": image_urls,
            "image_size": image_size,
            "output_format": output_format,
        },
    }
    if s.kie_callback_url:
        body["callBackUrl"] = s.kie_callback_url

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=body, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    if data.get("code") not in (200, 0):
        raise KieError(f"kie createTask failed: {data}")
    task_id = (data.get("data") or {}).get("taskId") or data.get("taskId")
    if not task_id:
        raise KieError(f"kie createTask returned no taskId: {data}")
    return str(task_id)


async def get_task(task_id: str) -> dict[str, Any]:
    s = get_settings()
    url = f"{s.kie_api_base.rstrip('/')}/playground/recordInfo"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, params={"taskId": task_id}, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def wait_for_task(task_id: str, *, timeout_s: int = 600, interval_s: float = 4.0) -> dict[str, Any]:
    """Block until the kie task completes; returns the parsed ``data`` payload."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict[str, Any] = {}
    while True:
        last = await get_task(task_id)
        data = last.get("data") or {}
        state = (data.get("state") or data.get("status") or "").lower()
        if state in {"success", "succeed", "succeeded", "done", "completed"}:
            return data
        if state in {"fail", "failed", "error"}:
            raise KieError(f"kie task {task_id} failed: {data.get('failMsg') or data}")
        if asyncio.get_event_loop().time() > deadline:
            raise KieError(f"kie task {task_id} timed out after {timeout_s}s; last={last}")
        await asyncio.sleep(interval_s)


def extract_first_image_url(task_payload: dict[str, Any]) -> str:
    """Pull the first generated image URL out of a completed task payload."""
    result = task_payload.get("resultJson") or task_payload.get("result") or {}
    if isinstance(result, str):
        # Sometimes returned as a JSON-encoded string.
        import json as _json

        try:
            result = _json.loads(result)
        except ValueError:
            result = {}
    candidates: list[str] = []
    for key in ("resultUrls", "imageUrls", "images", "outputs", "url", "image_url"):
        v = result.get(key) if isinstance(result, dict) else None
        if isinstance(v, str):
            candidates.append(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    for k in ("url", "image_url"):
                        if isinstance(item.get(k), str):
                            candidates.append(item[k])
    if not candidates:
        raise KieError(f"could not locate output image url in payload: {task_payload}")
    return candidates[0]
