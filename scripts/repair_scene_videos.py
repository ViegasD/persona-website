"""Stage finished scenes, review them, then animate the five explicit repair targets.

Reuses the original repair's guarded publication; never emails automatically.
"""
from __future__ import annotations
import argparse
import asyncio
import io
import json
from pathlib import Path
from datetime import UTC, datetime
from PIL import Image, ImageDraw
from app.services import kie_client, storage, xai_client, scene_builder
from app.workers.batch_runner import _load_characters, _extract_child_meta, _log_api_cost
from scripts import repair_reference_videos as base

base.CAMPAIGN = "scene-repair-20261026"


async def scenes(root, state):
    for item_id in base.TARGETS:
        entry = state["items"][str(item_id)]
        if entry.get("scene_review"):
            continue
        chars = await _load_characters(entry["characters"])
        if entry.get("scene_submission") == "submitting" and not entry.get("scene_task_id"):
            raise RuntimeError("Ambiguous image submission; inspect provider before resubmitting")
        if not entry.get("scene_task_id"):
            _, _, occasion, _ = _extract_child_meta(entry["custom_message"])
            entry["scene_submission"] = "submitting"
            base.save(root, state)
            entry["scene_task_id"] = await kie_client.create_composite_task(
                prompt=scene_builder.scene_prompt([c["name"] for c in chars], occasion or entry["occasion"]),
                image_urls=[storage.presigned_get_url(c["s3_key"]) for c in chars],
            )
            entry["scene_submission"] = "submitted"
            base.save(root, state)
            await _log_api_cost("kie", "composite_image", item_id)
        print(f"Waiting for scene {item_id}", flush=True)
        try:
            payload = await kie_client.wait_for_task(entry["scene_task_id"])
            raw = await xai_client.download_image(kie_client.extract_first_image_url(payload))
        except kie_client.KieError as exc:
            if len(chars) != 1 or "content policy" not in str(exc).lower(): raise
            if not entry.get("background_task_id"):
                if entry.get("background_submission") == "submitting": raise RuntimeError("Ambiguous background submission")
                entry["background_submission"] = "submitting"; base.save(root,state)
                _,_,occasion,_ = _extract_child_meta(entry["custom_message"])
                entry["background_task_id"] = await kie_client.create_composite_task(
                    prompt=scene_builder.background_prompt(occasion or entry["occasion"]), image_urls=[])
                entry["background_submission"] = "submitted"; base.save(root,state)
                await _log_api_cost("kie","composite_image",item_id)
            payload = await kie_client.wait_for_task(entry["background_task_id"])
            background = await xai_client.download_image(kie_client.extract_first_image_url(payload))
            raw = scene_builder.compose_original_on_background(storage.download_bytes(chars[0]["s3_key"]),background)
        scene_builder.validate_scene_dimensions(raw)
        key = storage.storefront_key("orders", str(entry["order_id"]), base.CAMPAIGN, f"scene-{item_id}-{entry['scene_task_id']}.png")
        storage.upload_bytes(key, raw, "image/png")
        (root / f"scene-{item_id}.png").write_bytes(raw)
        entry["scene_key"] = key
        entry["reference_key"] = chars[0]["s3_key"]
        base.save(root, state)
        originals = [storage.get_image_as_data_url(c["s3_key"]) for c in chars]
        entry["scene_review"] = await scene_builder.review_scene(originals, storage.get_image_as_data_url(key))
        entry["reuse_prompt"] = entry["old_prompt"]
        base.save(root, state)
        print(f"Scene {item_id} passed identity/background review", flush=True)


def scene_sheet(root, state):
    sheet = Image.new("RGB", (720, 400 * len(base.TARGETS)), "#eeeeee")
    draw = ImageDraw.Draw(sheet)
    for i, item_id in enumerate(base.TARGETS):
        entry = state["items"][str(item_id)]
        draw.text((10, i * 400 + 5), f"Order {entry['order_id']} item {item_id}: reference / finished scene", fill="black")
        ref = Image.open(io.BytesIO(storage.download_bytes(entry["reference_key"]))).convert("RGB")
        ref.thumbnail((330, 365)); sheet.paste(ref, ((360 - ref.width) // 2, i * 400 + 30))
        scene = Image.open(root / f"scene-{item_id}.png").convert("RGB")
        scene.thumbnail((330, 365)); sheet.paste(scene, (360 + (360 - scene.width) // 2, i * 400 + 30))
    sheet.save(root / "scenes.jpg", quality=92)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "scenes", "review-scenes", "approve-scenes", "generate", "review", "publish"])
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    root = Path(args.state_dir); root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.phase == "prepare": return await base.prepare(root)
    state = json.loads((root / "state.json").read_text())
    if state.get("campaign") != base.CAMPAIGN or {int(x) for x in state["items"]} != set(base.TARGETS):
        raise RuntimeError("Unexpected campaign")
    if args.phase == "scenes": return await scenes(root, state)
    if args.phase == "review-scenes": return scene_sheet(root, state)
    if args.phase == "approve-scenes":
        if not all(e.get("scene_review") for e in state["items"].values()): raise RuntimeError("Scene checks incomplete")
        state["scenes_approved_at"] = datetime.now(UTC).isoformat(); base.save(root, state); return
    if not state.get("scenes_approved_at"): raise RuntimeError("Review all scenes before animating")
    if args.phase == "review": return base.review_sheet(root, state)
    await {"generate": base.generate, "publish": base.publish}[args.phase](root, state)


if __name__ == "__main__": asyncio.run(main())
