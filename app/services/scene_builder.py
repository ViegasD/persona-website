"""Compose and validate a finished scene before spending on video animation."""
from __future__ import annotations
import io
import json
from PIL import Image
from openai import AsyncOpenAI
from app.core.settings import get_settings

SCENE_VERSION = "finished-scene-v4"


def background_prompt(occasion: str | None) -> str:
    mood = "subtle birthday balloons and flower garlands" if occasion == "aniversario" else "flowers and gentle sunlight"
    return (
        f"A polished illustrated storybook garden with {mood}, warm welcoming lighting. "
        "Vertical 9:16 full-bleed scenery, edge to edge, foreground and background vegetation. "
        "Leave visual space in the center for a portrait. No people, characters, faces, statues, "
        "animals, writing, numbers, watermarks, white borders or panels."
    )


def compose_original_on_background(original: bytes, background: bytes) -> bytes:
    """Keep original character pixels when a single transparent/white cutout is available."""
    from PIL import ImageChops, ImageDraw, ImageFilter, ImageOps
    with Image.open(io.BytesIO(original)) as source, Image.open(io.BytesIO(background)) as backdrop:
        subject = ImageOps.exif_transpose(source).convert("RGBA")
        alpha = subject.getchannel("A")
        if alpha.getextrema() == (255, 255):
            rgb = subject.convert("RGB")
            corners = [rgb.getpixel(p) for p in [(0,0),(rgb.width-1,0),(0,rgb.height-1),(rgb.width-1,rgb.height-1)]]
            if sum(min(pixel) > 235 for pixel in corners) < 3:
                raise ValueError("manual cutout needed for this catalog reference")
            r, g, b = rgb.split()
            whites = ImageChops.darker(ImageChops.darker(r,g),b).point(lambda p: 255 if p > 235 else 0)
            border = [(x,y) for x in range(rgb.width) for y in [0,rgb.height-1]]
            border += [(x,y) for y in range(rgb.height) for x in [0,rgb.width-1]]
            for point in border:
                if whites.getpixel(point) == 255:
                    ImageDraw.floodfill(whites, point, 128)
            alpha = whites.point(lambda p: 0 if p == 128 else 255).filter(ImageFilter.GaussianBlur(0.4))
            subject.putalpha(alpha)
        box = alpha.getbbox()
        if box is None:
            raise ValueError("empty catalog cutout")
        subject = subject.crop(box)
        scene = backdrop.convert("RGBA")
        subject = ImageOps.contain(subject, (int(scene.width * .98), int(scene.height * .90)), Image.Resampling.LANCZOS)
        scene.alpha_composite(subject, ((scene.width-subject.width)//2, scene.height-subject.height))
        output=io.BytesIO(); scene.convert("RGB").save(output,"PNG"); return output.getvalue()


def scene_prompt(names: list[str], occasion: str | None) -> str:
    setting = {
        "aniversario": "an enchanting flower garden with subtle birthday decorations and warm sunlight",
        "natal": "a cozy festive Christmas room with warm lights",
        "medos": "a peaceful, welcoming storybook garden with gentle sunlight, reassuring and safe",
    }.get(occasion or "", "a colorful welcoming storybook garden with flowers and warm natural light")
    return (
        f"Create a finished vertical 9:16 cameo scene for {len(names)} character(s). "
        f"Use the supplied character images as strict identity references. Cast, left to right: {', '.join(names)}. "
        "Preserve the exact original faces, species, age, hair, clothing, colors, proportions and individual art styles. "
        "Do not substitute a generic lookalike or redesign the character. No extra people or characters. "
        f"Place them naturally in {setting}. "
        "Replace ALL reference backgrounds, including white backgrounds and checkerboard patterns. "
        "The scenery must fill the entire frame edge to edge: no white margins, panels, borders, collage or letterboxing. "
        "Frame the character prominently, filling about 65-80 percent of the frame height; "
        "if the reference is a portrait, retain waist-up framing rather than inventing a new full-body design. "
        "Keep faces and hands visible with comfortable headroom. Coherent lighting, clean polished composition. "
        "No text, captions, watermarks or logos."
    )


def validate_scene_dimensions(data: bytes) -> None:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.width < 450 or image.height < 800 or abs(image.width / image.height - 9 / 16) > 0.02:
            raise ValueError("scene must be a full-resolution portrait image")


async def review_scene(reference_urls: list[str], scene_url: str) -> dict:
    settings = get_settings()
    content = [{"type": "text", "text": (
        "Compare the original character references (all images except the last) with the final scene (last image). "
        "Return JSON booleans identity_preserved, full_background, prominent_subjects, and a short reason. "
        "identity_preserved: every requested character keeps its face, species, colors, outfit and art style, with no extra character. "
        "full_background: a real, coherent setting fills ALL edges, with no blank white bands, checkerboard, collage or letterboxing. "
        "prominent_subjects: characters occupy a substantial part of the frame and faces are readable, not tiny figures. "
        "Be strict. An attractive but different character fails."
    )}]
    content += [{"type": "image_url", "image_url": {"url": url, "detail": "low"}} for url in [*reference_urls, scene_url]]
    result = await AsyncOpenAI(api_key=settings.openai_api_key).chat.completions.create(
        model=settings.openai_model, messages=[{"role": "user", "content": content}],
        response_format={"type": "json_object"}, max_completion_tokens=500,
    )
    verdict = json.loads(result.choices[0].message.content or "{}")
    if not all(verdict.get(key) is True for key in ["identity_preserved", "full_background", "prominent_subjects"]):
        raise ValueError("scene failed character/background review; manual review required")
    return verdict
