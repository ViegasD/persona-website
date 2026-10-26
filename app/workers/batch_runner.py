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
from uuid import uuid4

from arq import cron
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload

from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.db.models import (
    ApiCostLog,
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
from app.services import delivery, kie_client, script_writer, storage, xai_client, scene_builder
from app.services.outbox import dispatch_outbox, record_delivery_job, record_video_jobs
from app.services.processing_policy import processing_allowed, processing_enabled
from app.services.prompt_builder import (
    CharacterLine,
    VideoPromptInputs,
    build_video_prompt,
)
from app.services.worker_recovery import recover_interrupted_jobs
from app.services.generation_config import read_generation_config
from app.services.cost_tracking import tracked, reconcile_xai_usage
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
    character_ids: list[str], recipient_name: str | None, occasion_slug: str | None,
    *, reference_hashes: list[str] | None = None, model: str | None = None,
    scene_context: dict | None = None,
) -> str:
    payload = json.dumps(
        {
            "characters": character_ids,
            "references": reference_hashes or [],
            "model": model or get_settings().kie_nano_banana_model,
            "prompt_version": scene_builder.SCENE_VERSION,
            "recipient": (recipient_name or "").strip().lower(),
            "occasion": (occasion_slug or "").strip().lower(),
            "scene_context": scene_context or {},
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def _log_api_cost(
    provider: str, operation: str, order_item_id: int | None = None,
    *, duration: int | None = None, resolution: str | None = None, request_id: str | None = None, model: str | None = None,
) -> None:
    """Insert an ApiCostLog row using the configured cost estimate for the provider+operation."""
    s = get_settings()
    if provider == "xai" and operation == "video_generation":
        is_hd = (resolution or s.xai_video_resolution) != "480p"
        rate = s.cost_xai_hd_per_sec_micro_usd if is_hd else s.cost_xai_sd_per_sec_micro_usd
        cost = rate * (duration or s.xai_video_duration_seconds)
    elif provider == "kie" and operation == "composite_image":
        cost = s.cost_kie_composite_micro_usd
    else:
        cost = 0
    if cost <= 0:
        return
    try:
        async with session_scope() as session:
            session.add(ApiCostLog(
                order_item_id=order_item_id,
                provider=provider,
                operation=operation,
                cost_micro_usd=cost,
                provider_request_id=request_id,model=model,
            ))
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to log API cost: {exc}", exc=exc)


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
        url = await asyncio.to_thread(storage.get_image_as_data_url, thumb_key, portrait=True)
        out.append(
            {
                "id": slug,
                "name": row[1] or slug,
                "descriptor": (row[2] or "").strip(),
                "reference_url": url,
                "s3_key": thumb_key,
                "reference_sha256": hashlib.sha256(url.encode()).hexdigest(),
            }
        )
    return out


@tracked
async def _resolve_composite_url(
    item: OrderItem,
    chars: list[dict[str, Any]],
    *,
    recipient_name: str | None,
    occasion_slug: str | None,
    custom_message: str | None = None,
) -> tuple[str, str | None]:
    """Return ``(input_image_url, composite_s3_key_or_None)``.

    All videos start from a finished, reference-preserving background scene.
    """
    context = scene_builder.scene_context(occasion_slug, custom_message)
    sha = composite_cache_key(item.character_ids, recipient_name, occasion_slug,
                              reference_hashes=[c["reference_sha256"] for c in chars], scene_context=context)
    async with session_scope() as session:
        cached = (
            await session.execute(
                select(CompositeFrameCache).where(CompositeFrameCache.sha == sha)
            )
        ).scalar_one_or_none()
    if cached:
        async with session_scope() as session:
            current = await session.get(OrderItem, item.id)
            current.composite_image_s3_key = cached.s3_key
            await session.commit()
        return await asyncio.to_thread(storage.get_image_as_data_url, cached.s3_key), cached.s3_key

    composite_prompt = scene_builder.scene_prompt([c["name"] for c in chars], occasion_slug, context=context)
    # KIE image_input requires public HTTP URLs, not data URIs.
    kie_image_urls = [storage.presigned_get_url(c["s3_key"], expires_in=3600) for c in chars]
    task_id = await kie_client.create_composite_task(
        prompt=composite_prompt,
        image_urls=kie_image_urls,
        image_size="9:16",
        output_format="PNG",
    )
    async with session_scope() as session:
        current = await session.get(OrderItem, item.id)
        current.comfy_workflow_a_prompt_id = task_id
        await session.commit()
    await _log_api_cost("kie", "composite_image", item.id,request_id=task_id,model=get_settings().kie_nano_banana_model)
    try:
        payload = await kie_client.wait_for_task(task_id)
        raw = await xai_client.download_image(kie_client.extract_first_image_url(payload))
    except kie_client.KieError as exc:
        if len(chars) != 1 or "content policy" not in str(exc).lower():
            raise
        # Compose the existing catalog artwork unchanged over scenery generated on its own.
        task_id = await kie_client.create_composite_task(prompt=scene_builder.background_prompt(occasion_slug, context=context), image_urls=[])
        async with session_scope() as session:
            current = await session.get(OrderItem, item.id)
            current.comfy_workflow_a_prompt_id = task_id
            await session.commit()
        await _log_api_cost("kie", "composite_image", item.id,request_id=task_id,model=get_settings().kie_nano_banana_model)
        payload = await kie_client.wait_for_task(task_id)
        background = await xai_client.download_image(kie_client.extract_first_image_url(payload))
        original = await asyncio.to_thread(storage.download_bytes, chars[0]["s3_key"])
        raw = await asyncio.to_thread(scene_builder.compose_original_on_background, original, background)
    scene_builder.validate_scene_dimensions(raw)
    s3_key = storage.storefront_key("composites", f"{sha}.png")
    storage.upload_bytes(s3_key, raw, content_type="image/png")
    candidate = await asyncio.to_thread(storage.get_image_as_data_url, s3_key)
    async with session_scope() as session:
        current = await session.get(OrderItem, item.id)
        current.composite_image_s3_key = s3_key
        current.comfy_workflow_a_prompt_id = task_id
        await session.commit()
    original_refs = [await asyncio.to_thread(storage.get_image_as_data_url, c["s3_key"]) for c in chars]
    verdict = await scene_builder.review_scene(original_refs, candidate, context=context)
    async with session_scope() as session:
        session.add(
            CompositeFrameCache(sha=sha, s3_key=s3_key, character_ids=item.character_ids,
                                payload={"review": verdict, "version": scene_builder.SCENE_VERSION, "scene_context": context})
        )
        await session.commit()
    return await asyncio.to_thread(storage.get_image_as_data_url, s3_key), s3_key


# ─────────────────────────── per-item task (legacy batch) ─────────────────


async def _generate_video_for_item(item_id: int) -> None:
    """Used by the legacy run_batch path. Generates composite + video end-to-end → READY."""
    if not processing_enabled():
        return
    async with session_scope() as session:
        item: OrderItem = (
            await session.execute(
                select(OrderItem)
                .where(OrderItem.id == item_id)
                .options(selectinload(OrderItem.order))
            )
        ).scalar_one()
        order: Order = item.order
        if not processing_allowed(order) or order.status not in {OrderStatus.PAID, OrderStatus.QUEUED, OrderStatus.GENERATING}:
            return
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
        custom_message=custom_message,
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
    if not processing_enabled():
        return
    settings = get_settings()
    try:
        async with session_scope() as session:
            batch = (
                await session.execute(select(Batch).where(Batch.id == batch_id))
            ).scalar_one()
            if batch.status not in {BatchStatus.STARTING_POD, BatchStatus.COLLECTING}:
                return
            orders = (await session.execute(select(Order).join(OrderItem).join(
                BatchItem, BatchItem.order_item_id == OrderItem.id
            ).where(BatchItem.batch_id == batch_id))).scalars().all()
            if not orders or any(not processing_allowed(order) for order in orders):
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
                not_ready = (
                    await session.execute(
                        select(func.count()).where(
                            OrderItem.order_id == order_id,
                            OrderItem.status != OrderItemStatus.READY,
                        )
                    )
                ).scalar_one()
                if not_ready == 0:
                    order = (
                        await session.execute(select(Order).where(Order.id == order_id))
                    ).scalar_one()
                    if order.status not in {OrderStatus.READY, OrderStatus.DELIVERED}:
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
    if not processing_enabled():
        return
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
        orders = (await session.execute(select(Order).join(OrderItem).join(
            BatchItem, BatchItem.order_item_id == OrderItem.id
        ).where(BatchItem.batch_id == batch.id))).scalars().all()
        if not orders or any(not processing_allowed(order) for order in orders):
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
    if not processing_enabled():
        return
    phase2_generation = "from:" + str(ctx.get("job_id") or uuid4().hex)
    try:
        async with session_scope() as session:
            item: OrderItem = (
                await session.execute(
                    select(OrderItem)
                    .where(OrderItem.id == item_id).with_for_update()
                    .options(selectinload(OrderItem.order))
                )
            ).scalar_one()
            order: Order = item.order
            if (not processing_allowed(order) or order.status not in {OrderStatus.PAID, OrderStatus.QUEUED, OrderStatus.GENERATING}
                    or item.status != OrderItemStatus.PENDING):
                return
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
                custom_message=custom_message,
            )
            async with session_scope() as session:
                item = (
                    await session.execute(select(OrderItem).where(OrderItem.id == item_id))
                ).scalar_one()
                if composite_key:
                    item.composite_image_s3_key = composite_key
                item.status = OrderItemStatus.COMPOSITING
                await record_video_jobs(session, order, [item_id], kind="generate_video", generation=phase2_generation)
                await session.commit()
        else:
            # ── Single-character: generate video directly ───────────────────
            input_image_url, _composite_key = await _resolve_composite_url(
                item, chars,
                recipient_name=recipient_name,
                occasion_slug=occasion_slug,
                custom_message=custom_message,
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
                queue_phase2=True,
                phase2_generation=phase2_generation,
            )

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
    if not processing_enabled():
        return
    try:
        async with session_scope() as session:
            item: OrderItem = (
                await session.execute(
                    select(OrderItem)
                    .where(OrderItem.id == item_id).with_for_update()
                    .options(selectinload(OrderItem.order))
                )
            ).scalar_one()
            order: Order = item.order
            if not processing_allowed(order) or order.status not in {OrderStatus.PAID, OrderStatus.QUEUED, OrderStatus.GENERATING} or item.status not in {
                OrderItemStatus.COMPOSITING, OrderItemStatus.AWAITING_APPROVAL, OrderItemStatus.APPROVED,
            }:
                return
            item.status = OrderItemStatus.RENDERING
            item.attempts += 1
            await session.commit()
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
                await asyncio.to_thread(storage.get_image_as_data_url, composite_key)
                if composite_key
                else (await _resolve_composite_url(item, chars, recipient_name=recipient_name, occasion_slug=occasion_slug,
                                                 custom_message=custom_message))[0]
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

        # Mark this item READY in its own session, then check all-ready
        # using a fresh session + scalar COUNT to avoid stale identity-map
        # values from the initial selectinload earlier in this function.
        async with session_scope() as session:
            item = (
                await session.execute(select(OrderItem).where(OrderItem.id == item_id))
            ).scalar_one()
            order_id = item.order_id
            item.status = OrderItemStatus.READY

        # Fresh session: count items that are NOT yet READY.
        async with session_scope() as session:
            not_ready = (
                await session.execute(
                    select(func.count()).where(
                        OrderItem.order_id == order_id,
                        OrderItem.status != OrderItemStatus.READY,
                    )
                )
            ).scalar_one()
            if not_ready == 0:
                order = (
                    await session.execute(select(Order).where(Order.id == order_id).with_for_update())
                ).scalar_one()
                if order.status not in {OrderStatus.READY, OrderStatus.DELIVERED, OrderStatus.REFUNDED}:
                    order.status = OrderStatus.READY
                    order.generated_at = datetime.now(UTC)
                    await record_delivery_job(session, order)

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


@tracked
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
    queue_phase2: bool = False,
    phase2_generation: str = "",
) -> None:
    """Generate video via xAI Grok and store in S3, updating the OrderItem row."""
    settings = get_settings()

    async with session_scope() as session:
        generation = await read_generation_config(session)

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
        duration_seconds=generation.duration,
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
        "preserving the original background, occasion props, composition, pose and viewing angle. "
        "Keep cakes, gifts and decorations exactly as they appear in the reference; do not make props appear or disappear. "
        "Do not introduce a new setting"
    )
    prompt_text = build_video_prompt(
        VideoPromptInputs(
            scene_description=scene_description,
            characters=[
                CharacterLine(
                    descriptor=f"character in reference position {i + 1}",
                    line_pt=script.character_lines[i],
                )
                for i in range(len(chars))
            ],
            group_line_pt=script.group_line,
            duration_seconds=generation.duration,
        )
    )

    request_id = await xai_client.start_image_to_video(
        prompt=prompt_text,
        image_url=input_image_url,
        duration=generation.duration,
        model=generation.model,
        aspect_ratio=settings.xai_video_aspect_ratio,
        resolution="720p" if quality == "hd" else "480p",
    )
    await _log_api_cost("xai", "video_generation", item_id, duration=generation.duration,
                        resolution="720p" if quality == "hd" else "480p",request_id=request_id,model=generation.model)
    logger.info("[item {item}] reference_sha256={sha} request_id={rid}", item=item_id,
                sha=hashlib.sha256(input_image_url.encode()).hexdigest(), rid=request_id)
    async with session_scope() as session:
        item = await session.get(OrderItem, item_id)
        item.comfy_workflow_b_prompt_id = request_id
        item.resolved_script = prompt_text
        await session.commit()
    result = await xai_client.wait_for_video(request_id)
    await reconcile_xai_usage(request_id,result)
    video_info = result.get("video") or {}
    video_url = video_info.get("url")
    if not video_url:
        raise xai_client.XaiError("xAI returned no video URL")

    async with session_scope() as session:
        item = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        order_id = item.order_id
        sequence = item.sequence

    raw = await xai_client.download_video(video_url, expected_duration=generation.duration)
    video_key = storage.storefront_key("orders", str(order_id), f"video-{sequence}-{uuid4().hex}.mp4")
    storage.upload_bytes(video_key, raw, content_type="video/mp4")

    async with session_scope() as session:
        item = (
            await session.execute(select(OrderItem).where(OrderItem.id == item_id))
        ).scalar_one()
        item.comfy_workflow_b_prompt_id = request_id  # repurposed: xAI request id
        item.resolved_script = prompt_text
        item.video_s3_key = video_key
        if queue_phase2:
            order = await session.get(Order, item.order_id)
            await record_video_jobs(session, order, [item_id], kind="generate_video", generation=phase2_generation)
        await session.commit()


# ─────────────────────────── arq settings ─────────────────────────────────


class WorkerSettings:
    queue_name = get_settings().video_queue_name
    functions = [run_batch, collector_check, process_item_phase1, generate_video_for_approved_item, dispatch_outbox, recover_interrupted_jobs]
    redis_settings = redis_settings()
    cron_jobs = [cron(dispatch_outbox, second={0, 10, 20, 30, 40, 50}, run_at_startup=True),
                 cron(recover_interrupted_jobs, minute={0, 15, 30, 45}, run_at_startup=True)]

    @staticmethod
    async def on_startup(ctx: dict) -> None:  # noqa: ARG004
        if not processing_enabled():
            raise RuntimeError("Video processing is paused; use AccountWorkerSettings")
        configure_logging()

    job_timeout = 60 * 60
    keep_result = 60 * 60


class AccountWorkerSettings(WorkerSettings):
    """Keep account mail running without consuming the paused video queue."""

    # ARQ reads the class __dict__, not inherited connection settings.
    redis_settings = redis_settings()
    queue_name = "persona:account:queue"
    functions = [dispatch_outbox]
    cron_jobs = [cron(dispatch_outbox, second={0, 10, 20, 30, 40, 50}, run_at_startup=True)]

    @staticmethod
    async def on_startup(ctx: dict) -> None:  # noqa: ARG004
        configure_logging()
