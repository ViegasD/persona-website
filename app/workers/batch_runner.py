"""arq worker that runs cameo order processing.

New per-item pipeline (two-phase, auto-approved):

Phase 1 — triggered immediately on payment:
  • Multi-character item: generate composite image via kie.ai → enqueue phase 2
  • Single-character item: generate video via xAI Grok → enqueue phase 2

Phase 2 — triggered automatically after phase 1 (no manual approval needed):
  • Multi-character item (has composite, no video): generate video → READY → deliver
  • Single-character item (already has video): mark READY → deliver

Legacy batch mode (run_batch / collector_check) is kept for manual use but
is no longer triggered automatically on new payments.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.core.logging import configure_logging
from app.core.settings import get_settings
from loguru import logger
from app.db.models import (
    Batch,
    BatchItem,
    BatchStatus,
    CompositeFrameCache,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
)
from app.db.session import session_scope
from app.services import delivery, kie_client, script_writer, storage, xai_client
from app.services.prompt_builder import (
    CharacterLine,
    VideoPromptInputs,
    build_video_prompt,
)
from app.workers.queue import redis_settings


# ─────────────────────────── helpers ──────────────────────────────────────


def _extract_child_meta(
    custom_message: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse per-video child info JSON prefix injected by fill_items.

    Returns (child_name, child_age, occasion_slug, cleaned_message).
    If no prefix is present all returned values are None except cleaned_message
    which equals the original custom_message unchanged.
    """
    if not custom_message:
        return None, None, None, custom_message
    first_line, _, rest = custom_message.partition("\n")
    try:
        data = json.loads(first_line)
        child = data.get("_child", {})
        if child:
            return (
                child.get("name") or None,
                child.get("age") or None,
                child.get("occ") or None,
                rest.strip() or None,
            )
    except (ValueError, KeyError):
        pass
    return None, None, None, custom_message


def composite_cache_key(
    character_ids: list[str], recipient_name: str | None, occasion_slug: str | None
) -> str:
    payload = json.dumps(
        {
            "characters": sorted(character_ids),
            "recipient": (recipient_name or "").strip().lower(),
            "occasion": (occasion_slug or "").strip().lower(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def _load_characters(character_slugs: list[str]) -> list[dict[str, Any]]:
    """Fetch ``{id, name, descriptor, reference_url}`` from ``web.character_v``."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT slug, name, description, thumbnail_s3_key "
                    "FROM web.character_v WHERE slug = ANY(:slugs)"
                ),
                {"slugs": character_slugs},
            )
        ).all()
    by_slug = {r[0]: r for r in rows}
    out: list[dict[str, Any]] = []
    for slug in character_slugs:
        row = by_slug.get(slug)
        if row is None:
            raise RuntimeError(f"character '{slug}' not found in catalog view")
        thumb_key = row[3]
        if not thumb_key:
            raise RuntimeError(f"character '{slug}' has no reference image")
        url = storage.get_image_as_data_url(thumb_key)
        out.append(
            {
                "id": slug,
                "name": row[1] or slug,
                "descriptor": (row[2] or "").strip(),
                "reference_url": url,
                "s3_key": thumb_key,
            }
        )
    return out


async def _resolve_composite_url(
    item: OrderItem,
    chars: list[dict[str, Any]],
    *,
    recipient_name: str | None,
    occasion_slug: str | None,
) -> tuple[str, str | None]:
    """Return ``(input_image_url, composite_s3_key_or_None)``.

    Single character → reuse its catalog reference image directly.
    Multi character → call kie.ai nano-banana-pro to merge them, with cache.
    """
    if len(chars) == 1:
        return chars[0]["reference_url"], None

    sha = composite_cache_key(item.character_ids, recipient_name, occasion_slug)
    async with session_scope() as session:
        cached = (
            await session.execute(
                select(CompositeFrameCache).where(CompositeFrameCache.sha == sha)
            )
        ).scalar_one_or_none()
    if cached:
        return storage.get_image_as_data_url(cached.s3_key), cached.s3_key

    # Build a tight, kid-safe composite prompt — Nano Banana is good at this.
    descriptors = ", ".join(
        f"({c['name']}: {c['descriptor']})" if c["descriptor"] else c["name"] for c in chars
    )
    composite_prompt = (
        f"Combine these {len(chars)} characters into a single image, standing side by side "
        "facing the camera on a colorful, vibrant kid-friendly stage with warm lighting. "
        f"Characters from left to right: {descriptors}. "
        "Keep each character's appearance, outfit, colors and proportions exactly as in their reference image. "
        "Full body or three-quarter framing. No text, no captions, no logos. Vertical 9:16 composition."
    )
    # KIE image_input requires public HTTP URLs, not data URIs.
    kie_image_urls = [storage.presigned_get_url(c["s3_key"], expires_in=3600) for c in chars]
    task_id = await kie_client.create_composite_task(
        prompt=composite_prompt,
        image_urls=kie_image_urls,
        image_size="9:16",
        output_format="PNG",
    )
    payload = await kie_client.wait_for_task(task_id)
    composite_url = kie_client.extract_first_image_url(payload)

    # Persist a copy in our S3 so we don't depend on kie's CDN expiry.
    raw = await xai_client.download_video(composite_url)  # generic GET wrapper
    s3_key = storage.storefront_key("composites", f"{sha}.png")
    storage.upload_bytes(s3_key, raw, content_type="image/png")
    async with session_scope() as session:
        session.add(
            CompositeFrameCache(sha=sha, s3_key=s3_key, character_ids=item.character_ids)
        )
        await session.commit()
    return storage.get_image_as_data_url(s3_key), s3_key


# ─────────────────────────── per-item task (legacy batch) ─────────────────


async def _generate_video_for_item(item_id: int) -> None:
    """Used by the legacy run_batch path. Generates composite + video end-to-end → READY."""
    async with session_scope() as session:
        item: OrderItem = (
            await session.execute(
                select(OrderItem)
                .where(OrderItem.id == item_id)
                .options(selectinload(OrderItem.order))
            )
        ).scalar_one()
        order: Order = item.order
        item.attempts += 1
        item.status = OrderItemStatus.COMPOSITING
        await session.commit()
        char_ids = list(item.character_ids)
        custom_message = item.custom_message
        recipient_name = order.recipient_name
        recipient_age = order.recipient_age
        occasion_slug = order.occasion_slug
        quality = order.quality

    chars = await _load_characters(char_ids)

    # Extract per-video child info from the custom_message JSON prefix (if present)
    _child_name, _child_age, _child_occ, custom_message = _extract_child_meta(custom_message)
    if _child_name: recipient_name = _child_name
    if _child_age:  recipient_age  = _child_age
    if _child_occ:  occasion_slug  = _child_occ

    input_image_url, composite_key = await _resolve_composite_url(
        item, chars,
        recipient_name=recipient_name,
        occasion_slug=occasion_slug,
    )

    async with session_scope() as session:
        item = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        if composite_key:
            item.composite_image_s3_key = composite_key
        item.status = OrderItemStatus.RENDERING
        await session.commit()

    await _generate_and_store_video(
        item_id=item_id,
        chars=chars,
        input_image_url=input_image_url,
        recipient_name=recipient_name,
        recipient_age=recipient_age,
        occasion_slug=occasion_slug,
        custom_message=custom_message,
        quality=quality,
    )

    async with session_scope() as session:
        item = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        item.status = OrderItemStatus.READY
        await session.commit()


# ─────────────────────────── arq tasks ────────────────────────────────────


async def run_batch(ctx: dict[str, Any], batch_id: int) -> None:  # noqa: ARG001
    settings = get_settings()
    try:
        async with session_scope() as session:
            batch = (
                await session.execute(select(Batch).where(Batch.id == batch_id))
            ).scalar_one()
            if batch.status not in {BatchStatus.STARTING_POD, BatchStatus.COLLECTING}:
                return
            batch.status = BatchStatus.RUNNING
            batch.started_at = batch.started_at or datetime.now(UTC)
            await session.commit()

            item_ids = (
                await session.execute(
                    select(BatchItem.order_item_id).where(BatchItem.batch_id == batch_id)
                )
            ).scalars().all()

        sem = asyncio.Semaphore(settings.batch_concurrency)
        failures: list[tuple[int, str]] = []

        async def _one(item_id: int) -> None:
            async with sem:
                try:
                    await _generate_video_for_item(item_id)
                except Exception as exc:  # noqa: BLE001
                    failures.append((item_id, str(exc)[:500]))
                    async with session_scope() as session:
                        item = (
                            await session.execute(
                                select(OrderItem).where(OrderItem.id == item_id)
                            )
                        ).scalar_one()
                        item.status = OrderItemStatus.FAILED
                        item.error = str(exc)[:500]
                        await session.commit()

        await asyncio.gather(*[_one(i) for i in item_ids])

        async with session_scope() as session:
            batch = (
                await session.execute(select(Batch).where(Batch.id == batch_id))
            ).scalar_one()
            batch.status = BatchStatus.DONE if not failures else BatchStatus.FAILED
            batch.finished_at = datetime.now(UTC)
            if failures:
                batch.error = "; ".join(f"item {i}: {e}" for i, e in failures[:5])
            await session.commit()

            order_ids = (
                await session.execute(
                    select(OrderItem.order_id)
                    .distinct()
                    .where(OrderItem.id.in_(item_ids))
                )
            ).scalars().all()
            for order_id in order_ids:
                order = (
                    await session.execute(
                        select(Order)
                        .where(Order.id == order_id)
                        .options(selectinload(Order.items))
                    )
                ).scalar_one()
                if all(it.status == OrderItemStatus.READY for it in order.items):
                    order.status = OrderStatus.READY
                    order.generated_at = datetime.now(UTC)
                    await session.commit()
                    await delivery.deliver_order(session, order.id)
                    await session.commit()
    except Exception as exc:  # noqa: BLE001
        async with session_scope() as session:
            batch = (
                await session.execute(select(Batch).where(Batch.id == batch_id))
            ).scalar_one_or_none()
            if batch is not None:
                batch.status = BatchStatus.FAILED
                batch.error = str(exc)[:500]
                batch.finished_at = datetime.now(UTC)
                await session.commit()
        raise


async def collector_check(ctx: dict[str, Any]) -> None:  # noqa: ARG001
    """Periodic: trigger the open batch when its age exceeds the policy."""
    from app.services import batch_collector
    from app.workers.queue import enqueue_run_batch

    async with session_scope() as session:
        batch = (
            await session.execute(
                select(Batch)
                .where(Batch.status == BatchStatus.COLLECTING)
                .order_by(Batch.id.desc())
            )
        ).scalar_one_or_none()
        if batch is None or batch.order_count == 0:
            return
        trigger = await batch_collector.should_trigger(session, batch)
        if trigger is None:
            return
        await batch_collector.mark_batch_starting(session, batch, trigger)
        batch_id = batch.id
        await session.commit()
    await enqueue_run_batch(batch_id)


# ─────────────────── Phase-1: process immediately on payment ──────────────


async def process_item_phase1(ctx: dict[str, Any], item_id: int) -> None:  # noqa: ARG001
    """Phase 1 — runs right after payment is confirmed.

    • Multi-char: generate composite image via kie.ai → AWAITING_APPROVAL
    • Single-char: generate video via xAI → AWAITING_APPROVAL

    In both cases the admin must approve before delivery happens.
    """
    try:
        async with session_scope() as session:
            item: OrderItem = (
                await session.execute(
                    select(OrderItem)
                    .where(OrderItem.id == item_id)
                    .options(selectinload(OrderItem.order))
                )
            ).scalar_one()
            order: Order = item.order
            item.attempts += 1
            item.status = OrderItemStatus.COMPOSITING
            await session.commit()
            char_ids = list(item.character_ids)
            custom_message = item.custom_message
            recipient_name = order.recipient_name
            recipient_age = order.recipient_age
            occasion_slug = order.occasion_slug
            quality = order.quality
            is_multi = len(char_ids) > 1

        chars = await _load_characters(char_ids)

        # Extract per-video child info from the custom_message JSON prefix (if present)
        _child_name, _child_age, _child_occ, custom_message = _extract_child_meta(custom_message)
        if _child_name: recipient_name = _child_name
        if _child_age:  recipient_age  = _child_age
        if _child_occ:  occasion_slug  = _child_occ

        if is_multi:
            # ── Multi-character: generate composite image only ──────────────
            _input_url, composite_key = await _resolve_composite_url(
                item, chars,
                recipient_name=recipient_name,
                occasion_slug=occasion_slug,
            )
            async with session_scope() as session:
                item = (
                    await session.execute(select(OrderItem).where(OrderItem.id == item_id))
                ).scalar_one()
                if composite_key:
                    item.composite_image_s3_key = composite_key
                item.status = OrderItemStatus.COMPOSITING
                await session.commit()
            # Auto-approve: immediately enqueue phase 2 (video generation)
            from app.workers.queue import enqueue_generate_video
            await enqueue_generate_video(item_id)
        else:
            # ── Single-character: generate video directly ───────────────────
            input_image_url, _composite_key = await _resolve_composite_url(
                item, chars,
                recipient_name=recipient_name,
                occasion_slug=occasion_slug,
            )
            await _generate_and_store_video(
                item_id=item_id,
                chars=chars,
                input_image_url=input_image_url,
                recipient_name=recipient_name,
                recipient_age=recipient_age,
                occasion_slug=occasion_slug,
                custom_message=custom_message,
                quality=quality,
            )
            # Auto-approve: immediately enqueue phase 2 (mark READY + deliver)
            from app.workers.queue import enqueue_generate_video
            await enqueue_generate_video(item_id)

    except Exception as exc:  # noqa: BLE001
        async with session_scope() as session:
            item = (
                await session.execute(select(OrderItem).where(OrderItem.id == item_id))
            ).scalar_one_or_none()
            if item is not None:
                item.status = OrderItemStatus.FAILED
                item.error = str(exc)[:500]
                await session.commit()
        raise


# ─────────────────── Phase-2: generate video after admin approves ─────────


async def generate_video_for_approved_item(ctx: dict[str, Any], item_id: int) -> None:  # noqa: ARG001
    """Phase 2 — triggered by admin approval.

    • Multi-char (has composite, no video): generate video → READY → deliver if order complete
    • Single-char (already has video from phase 1): just mark READY → deliver if order complete
    """
    try:
        async with session_scope() as session:
            item: OrderItem = (
                await session.execute(
                    select(OrderItem)
                    .where(OrderItem.id == item_id)
                    .options(selectinload(OrderItem.order))
                )
            ).scalar_one()
            order: Order = item.order
            char_ids = list(item.character_ids)
            composite_key = item.composite_image_s3_key
            has_video = bool(item.video_s3_key)
            custom_message = item.custom_message
            recipient_name = order.recipient_name
            recipient_age = order.recipient_age
            occasion_slug = order.occasion_slug
            quality = order.quality

        # Extract per-video child info from the custom_message JSON prefix (if present)
        _child_name, _child_age, _child_occ, custom_message = _extract_child_meta(custom_message)
        if _child_name: recipient_name = _child_name
        if _child_age:  recipient_age  = _child_age
        if _child_occ:  occasion_slug  = _child_occ

        if not has_video:
            # Multi-char: composite exists, need to generate video now
            chars = await _load_characters(char_ids)
            input_image_url = (
                storage.get_image_as_data_url(composite_key)
                if composite_key
                else (await _resolve_composite_url(item, chars, recipient_name=recipient_name, occasion_slug=occasion_slug))[0]
            )
            async with session_scope() as session:
                item = (
                    await session.execute(select(OrderItem).where(OrderItem.id == item_id))
                ).scalar_one()
                item.status = OrderItemStatus.RENDERING
                await session.commit()

            await _generate_and_store_video(
                item_id=item_id,
                chars=chars,
                input_image_url=input_image_url,
                recipient_name=recipient_name,
                recipient_age=recipient_age,
                occasion_slug=occasion_slug,
                custom_message=custom_message,
                quality=quality,
            )

        async with session_scope() as session:
            item = (
                await session.execute(
                    select(OrderItem)
                    .where(OrderItem.id == item_id)
                    .options(selectinload(OrderItem.order).selectinload(Order.items))
                )
            ).scalar_one()
            item.status = OrderItemStatus.READY
            await session.commit()

            order = (
                await session.execute(
                    select(Order)
                    .where(Order.id == item.order_id)
                    .options(selectinload(Order.items))
                )
            ).scalar_one()
            if all(it.status == OrderItemStatus.READY for it in order.items):
                order.status = OrderStatus.READY
                order.generated_at = datetime.now(UTC)
                await session.commit()
                await delivery.deliver_order(session, order.id)
                await session.commit()

    except Exception as exc:  # noqa: BLE001
        async with session_scope() as session:
            item = (
                await session.execute(select(OrderItem).where(OrderItem.id == item_id))
            ).scalar_one_or_none()
            if item is not None:
                item.status = OrderItemStatus.FAILED
                item.error = str(exc)[:500]
                await session.commit()
        raise


# ─────────────────────────── shared video helper ──────────────────────────


async def _generate_and_store_video(
    *,
    item_id: int,
    chars: list[dict[str, Any]],
    input_image_url: str,
    recipient_name: str | None,
    recipient_age: str | None,
    occasion_slug: str | None,
    custom_message: str | None,
    quality: str = "sd",
) -> None:
    """Generate video via xAI Grok and store in S3, updating the OrderItem row."""
    settings = get_settings()

    script = await script_writer.generate_structured_script(
        characters=[
            script_writer.CharacterSpec(
                id=c["id"], name=c["name"], descriptor=c["descriptor"]
            )
            for c in chars
        ],
        recipient_name=recipient_name or "amiguinho",
        recipient_age=recipient_age,
        occasion_slug=occasion_slug,
        user_message=custom_message,
    )

    char_slugs = [c["id"] for c in chars]
    char_names = ", ".join(c["name"] for c in chars)
    logger.info(
        "[item {item_id}] generate_video | chars={slugs} image_url_bytes={img_bytes}",
        item_id=item_id,
        slugs=char_slugs,
        img_bytes=len(input_image_url),
    )
    scene_description = (
        f"{char_names} — use the reference image exactly, preserve every visual detail of the character(s) — "
        f"standing on a vibrant, kid-friendly stage with warm lighting"
    )
    prompt_text = build_video_prompt(
        VideoPromptInputs(
            scene_description=scene_description,
            characters=[
                CharacterLine(
                    descriptor=chars[i]["descriptor"] or chars[i]["name"],
                    line_pt=script.character_lines[i],
                )
                for i in range(len(chars))
            ],
            group_line_pt=script.group_line,
            duration_seconds=settings.xai_video_duration_seconds,
        )
    )

    request_id = await xai_client.start_image_to_video(
        prompt=prompt_text,
        image_url=input_image_url,
        duration=settings.xai_video_duration_seconds,
        aspect_ratio=settings.xai_video_aspect_ratio,
        resolution="720p" if quality == "hd" else "480p",
    )
    result = await xai_client.wait_for_video(request_id)
    video_info = result.get("video") or {}
    video_url = video_info.get("url")
    if not video_url:
        raise RuntimeError(f"xAI returned no video url: {result}")

    async with session_scope() as session:
        item = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        order_id = item.order_id
        sequence = item.sequence

    raw = await xai_client.download_video(video_url)
    video_key = storage.storefront_key("orders", str(order_id), f"video-{sequence}.mp4")
    storage.upload_bytes(video_key, raw, content_type="video/mp4")

    async with session_scope() as session:
        item = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        item.comfy_workflow_b_prompt_id = request_id  # repurposed: xAI request id
        item.resolved_script = prompt_text
        item.video_s3_key = video_key
        await session.commit()


# ─────────────────────────── arq settings ─────────────────────────────────


class WorkerSettings:
    functions = [run_batch, collector_check, process_item_phase1, generate_video_for_approved_item]
    redis_settings = redis_settings()
    cron_jobs: list = []

    @staticmethod
    async def on_startup(ctx: dict) -> None:  # noqa: ARG004
        configure_logging()

    job_timeout = 60 * 60
    keep_result = 60 * 60
