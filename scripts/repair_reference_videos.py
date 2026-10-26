"""Explicit, resumable repair for the five confirmed 2026-10-26 fidelity failures.

Never enqueue the historical backlog. Generate into new immutable keys, review
the contact sheet, publish atomically, and notify only after publication.
Run in a private mounted state directory; it contains customer information.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
from datetime import UTC, datetime
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.db.models import Order, OrderItem, OrderStatus, OrderItemStatus, User
from app.db.session import session_scope
from app.services import storage, xai_client, script_writer, account_mail
from app.services.generation_config import read_generation_config
from app.services.prompt_builder import build_video_prompt, VideoPromptInputs, CharacterLine
from app.workers.batch_runner import _load_characters, _extract_child_meta, _log_api_cost

CAMPAIGN = "reference-repair-20261026"
TARGETS = {290: 175, 291: 176, 292: 176, 293: 176, 294: 177}


def save(root, state):
    path = root / "state.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


async def prepare(root):
    if (root / "state.json").exists():
        raise RuntimeError("Campaign already prepared; use the existing state")
    state = {"campaign": CAMPAIGN, "created_at": datetime.now(UTC).isoformat(), "items": {}, "notifications": {}}
    async with session_scope() as session:
        config = await read_generation_config(session)
        state["duration"] = config.duration
        state["model"] = config.model
        for item_id, order_id in TARGETS.items():
            item = await session.get(OrderItem, item_id)
            order = await session.get(Order, order_id)
            if (not item or item.order_id != order_id or not order or order.status != OrderStatus.DELIVERED
                    or item.status != OrderItemStatus.READY or not item.video_s3_key or len(item.character_ids) != 1):
                raise RuntimeError(f"Target eligibility changed for item {item_id}")
            user = await session.get(User, order.user_id) if order.user_id else None
            email = user.email if user and user.email_verified_at and user.email else order.guest_email
            if not email or "@" not in email:
                raise RuntimeError(f"No customer email for order {order_id}")
            state["items"][str(item_id)] = {
                "order_id": order_id, "sequence": item.sequence, "characters": item.character_ids,
                "old_key": item.video_s3_key, "old_thumbnail": item.thumbnail_s3_key,
                "old_scene": item.composite_image_s3_key, "old_scene_request_id": item.comfy_workflow_a_prompt_id,
                "old_prompt": item.resolved_script, "old_request_id": item.comfy_workflow_b_prompt_id,
                "custom_message": item.custom_message, "recipient_name": order.recipient_name,
                "recipient_age": order.recipient_age, "occasion": order.occasion_slug,
                "quality": order.quality, "email": email.strip().lower(),
                "account_exists": bool(user), "status": "prepared",
            }
    save(root, state)
    print("Prepared exactly five videos from orders 175, 176, 177", flush=True)


async def generate(root, state):
    for item_id in TARGETS:
        entry = state["items"][str(item_id)]
        if entry["status"] in {"generated", "published"}:
            continue
        if entry["status"] == "submitting":
            raise RuntimeError(f"Item {item_id}: ambiguous submission; inspect provider before retrying")
        if not entry.get("request_id"):
            chars = await _load_characters(entry["characters"])
            image_url = (storage.get_image_as_data_url(entry["scene_key"])
                         if entry.get("scene_key") else chars[0]["reference_url"])
            child, age, occasion, message = _extract_child_meta(entry["custom_message"])
            if not entry.get("reuse_prompt"):
                script = await script_writer.generate_structured_script(
                    characters=[script_writer.CharacterSpec(c["id"], c["name"], c["descriptor"]) for c in chars],
                    recipient_name=child or entry["recipient_name"] or "amiguinho",
                    recipient_age=age or entry["recipient_age"], occasion_slug=occasion or entry["occasion"],
                    user_message=message, duration_seconds=state["duration"],
                )
                entry["prompt"] = build_video_prompt(VideoPromptInputs(
                    "Animate only the character in the supplied image. Preserve the complete original appearance, pose, viewing angle and background.",
                    [CharacterLine("character in the supplied image", script.character_lines[0])],
                    script.group_line, state["duration"],
                ))
            else:
                entry["prompt"] = entry["reuse_prompt"]
            entry["reference_sha256"] = hashlib.sha256(image_url.encode()).hexdigest()
            entry["reference_key"] = chars[0]["s3_key"]
            entry["status"] = "submitting"
            save(root, state)
            entry["request_id"] = await xai_client.start_image_to_video(
                prompt=entry["prompt"], image_url=image_url, duration=state["duration"], model=state["model"],
                resolution="720p" if entry["quality"] == "hd" else "480p", aspect_ratio="9:16",
            )
            entry["status"] = "submitted"
            save(root, state)
            await _log_api_cost("xai", "video_generation", item_id, duration=state["duration"],
                                resolution="720p" if entry["quality"] == "hd" else "480p")
        print(f"Waiting for corrected item {item_id}", flush=True)
        result = await xai_client.wait_for_video(entry["request_id"])
        video_url = (result.get("video") or {}).get("url")
        if not video_url:
            raise RuntimeError(f"No video result for item {item_id}")
        raw = await xai_client.download_video(video_url, expected_duration=state["duration"])
        local = root / f"item-{item_id}.mp4"
        local.write_bytes(raw)
        frame = root / f"item-{item_id}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(local), "-frames:v", "1", str(frame)], check=True)
        key = storage.storefront_key("orders", str(entry["order_id"]), CAMPAIGN, f"item-{item_id}-{entry['request_id']}.mp4")
        thumbnail = key.removesuffix(".mp4") + ".jpg"
        storage.upload_bytes(key, raw, "video/mp4")
        storage.upload_bytes(thumbnail, frame.read_bytes(), "image/jpeg")
        entry.update(new_key=key, new_thumbnail=thumbnail, status="generated", bytes=len(raw))
        save(root, state)
        print(f"Generated item {item_id}; original remains published", flush=True)


def review_sheet(root, state):
    sheet = Image.new("RGB", (960, 290 * len(TARGETS)), "#eeeeee")
    draw = ImageDraw.Draw(sheet)
    for i, item_id in enumerate(TARGETS):
        entry = state["items"][str(item_id)]
        ref = Image.open(io.BytesIO(storage.download_bytes(entry["reference_key"]))).convert("RGB")
        ref.thumbnail((230, 250))
        sheet.paste(ref, ((240 - ref.width) // 2, i * 290 + 30))
        draw.text((5, i * 290 + 5), f"Order {entry['order_id']} item {item_id}", fill="black")
        for j, sec in enumerate([0, 3, state["duration"] - 1]):
            frame = root / f"review-{item_id}-{j}.png"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(sec), "-i", str(root / f"item-{item_id}.mp4"), "-frames:v", "1", str(frame)], check=True)
            im = Image.open(frame).convert("RGB"); im.thumbnail((230, 250))
            sheet.paste(im, (240 * (j + 1) + (240 - im.width) // 2, i * 290 + 30))
            draw.text((240 * (j + 1) + 5, i * 290 + 10), f"{sec}s", fill="black")
    sheet.save(root / "comparison.jpg", quality=90)
    print("Review comparison.jpg and listen to the videos before publishing", flush=True)


async def publish(root, state):
    if any(e["status"] not in {"generated", "published"} for e in state["items"].values()):
        raise RuntimeError("All five replacements must be generated and reviewed first")
    async with session_scope() as session:
        for item_id, order_id in TARGETS.items():
            entry = state["items"][str(item_id)]
            item = (await session.execute(select(OrderItem).where(OrderItem.id == item_id).with_for_update())).scalar_one()
            order = await session.get(Order, order_id)
            if item.order_id != order_id or order.status != OrderStatus.DELIVERED or item.status != OrderItemStatus.READY:
                raise RuntimeError(f"Order eligibility changed: {order_id}")
            if item.video_s3_key not in {entry["old_key"], entry["new_key"]}:
                raise RuntimeError(f"Concurrent replacement detected: {item_id}")
            item.video_s3_key = entry["new_key"]
            item.thumbnail_s3_key = entry["new_thumbnail"]
            item.resolved_script = entry["prompt"]
            item.comfy_workflow_b_prompt_id = entry["request_id"]
            if entry.get("scene_key"):
                item.composite_image_s3_key = entry["scene_key"]
                item.comfy_workflow_a_prompt_id = entry.get("background_task_id") or entry["scene_task_id"]
        await session.commit()
    for entry in state["items"].values(): entry["status"] = "published"
    state["published_at"] = datetime.now(UTC).isoformat()
    save(root, state)
    print("Published exactly five replacements; payment, credit and historical job records unchanged", flush=True)


async def notify(root, state):
    if not state.get("published_at"):
        raise RuntimeError("Replacements must be published before notifying customers")
    groups = {}
    for entry in state["items"].values():
        groups.setdefault(entry["email"], set()).add(entry["order_id"])
    for email, orders in groups.items():
        key = hashlib.sha256(email.encode()).hexdigest()
        previous = state["notifications"].get(key)
        if previous == "accepted": continue
        if previous == "sending": raise RuntimeError("Ambiguous SMTP delivery; inspect provider before resending")
        async with session_scope() as session:
            for item_id in TARGETS:
                entry = state["items"][str(item_id)]
                if entry["email"] == email:
                    item = await session.get(OrderItem, item_id)
                    if item.video_s3_key != entry["new_key"] or item.status != OrderItemStatus.READY:
                        raise RuntimeError("Published replacement is unavailable")
        numbers = ", ".join(f"#{order}" for order in sorted(orders))
        body = (
            "Olá!\n\n"
            f"Pedimos desculpas pelo problema nos vídeos dos seus pedidos {numbers}. "
            "Identificamos uma falha que fez com que a aparência de alguns personagens não correspondesse às imagens escolhidas.\n\n"
            "Corrigimos a falha e geramos novas versões dos seus vídeos, sem custo adicional. "
            "Eles já estão disponíveis em “Meus vídeos”, na sua conta:\n"
            "https://recadoanimado.com/meus-videos.html\n\n"
            "Entre usando o mesmo e-mail utilizado na compra. Se ainda não tiver uma conta, "
            "crie uma em https://recadoanimado.com/conta.html e confirme esse e-mail para acessar os seus vídeos.\n\n"
            "Sentimos muito por não termos acertado na primeira entrega e agradecemos pela sua compreensão.\n\n"
            "Equipe Recado Animado"
        )
        state["notifications"][key] = "sending"
        save(root, state)
        await account_mail.send_account_email({"to": email, "subject": "Seus vídeos foram corrigidos — Recado Animado", "body": body})
        state["notifications"][key] = "accepted"
        save(root, state)
        print(f"Apology email accepted by SMTP for orders {numbers}", flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "generate", "review", "publish", "notify"])
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    root = Path(args.state_dir); root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.phase == "prepare": return await prepare(root)
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    if state.get("campaign") != CAMPAIGN or {int(x) for x in state["items"]} != set(TARGETS):
        raise RuntimeError("Unexpected campaign state")
    if args.phase == "review": return review_sheet(root, state)
    await {"generate": generate, "publish": publish, "notify": notify}[args.phase](root, state)


if __name__ == "__main__":
    asyncio.run(main())
