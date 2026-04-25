"""Tiny arq queue helpers — the worker itself lives in ``batch_runner``."""

from __future__ import annotations

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.core.settings import get_settings


def redis_settings() -> RedisSettings:
    url = get_settings().redis_url
    return RedisSettings.from_dsn(url)


_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def enqueue_run_batch(batch_id: int) -> None:
    pool = await get_pool()
    await pool.enqueue_job("run_batch", batch_id)


async def enqueue_collector_check() -> None:
    pool = await get_pool()
    await pool.enqueue_job("collector_check")


async def enqueue_process_item(item_id: int) -> None:
    """Phase 1: composite (multi-char) or video (single-char) → AWAITING_APPROVAL."""
    pool = await get_pool()
    await pool.enqueue_job("process_item_phase1", item_id)


async def enqueue_generate_video(item_id: int) -> None:
    """Phase 2 (after admin approval): generate video for already-composited item, then deliver."""
    pool = await get_pool()
    await pool.enqueue_job("generate_video_for_approved_item", item_id)
