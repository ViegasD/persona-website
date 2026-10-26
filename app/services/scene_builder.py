"""Compose and validate a finished scene before spending on video animation."""
from __future__ import annotations
import io
import json
import re
import unicodedata
from PIL import Image
from openai import AsyncOpenAI
from app.core.settings import get_settings
from app.services.cost_tracking import record_openai_usage

SCENE_VERSION = "occasion-scene-v5"

# Curated visual instructions, never raw customer text or prompt instructions.
SETTINGS = {
    "birthday": "an enchanting flower garden prepared for a birthday celebration, with a pastel balloon arch, unlettered fabric bunting and warm sunlight",
    "christmas": "a cozy festive Christmas room with a decorated tree, warm fairy lights and layered garlands",
    "reassurance": "a peaceful, welcoming storybook reading nook with gentle daylight, soft cushions and a reassuring, safe atmosphere",
    "achievement": "a cheerful garden celebration with a small decorative star arch, warm sunlight and colorful ribbons",
    "graduation": "a welcoming school courtyard with a tasteful celebration display, colorful pennants and warm afternoon light",
    "family": "a cozy family garden with flower garlands, a comfortable seating nook and warm natural light",
    "wedding": "an elegant garden celebration with a floral arch, ribbons and soft warm light",
    "affection": "a welcoming flower garden with a thoughtful gift display and soft golden light",
    "greeting": "a colorful welcoming storybook garden with layered flowers, a small gift display and warm natural light",
}
PROPS = {
    "birthday": "a clearly visible decorated birthday cake on a small table beside the cast, a few wrapped gifts and grouped balloons",
    "christmas": "wrapped presents beside the tree and a small festive ornament display",
    "reassurance": "a small side table with closed storybooks and a soft star-shaped cushion; no scary or threatening imagery",
    "achievement": "a small display of star ornaments and colorful celebratory ribbons",
    "graduation": "stacked books and a rolled, unlettered diploma on a small display table",
    "family": "a bouquet and a neatly wrapped gift on a small side table",
    "wedding": "a small celebration cake on a decorated side table and coordinated flowers",
    "affection": "a bouquet and a small wrapped present beside the cast",
    "greeting": "a few coordinated wrapped presents and decorative flowers beside the cast",
}
THEMES = {
    "football": "subtle football-inspired decorations and a stationary football prop beside the cast",
    "space": "a decorative planet mobile and star-and-rocket motifs, while retaining the primary occasion and setting",
    "ocean": "decorative seashells and ocean-blue accents, while retaining the primary occasion and setting",
    "dinosaurs": "leaf garlands and dinosaur-footprint motifs, with no extra creatures or character figures",
    "reading": "a small stack of closed storybooks with unlettered covers and a cozy reading-themed display",
}
THEME_WORDS = {
    "football": ("futebol", "football"),
    "space": ("astronauta", "astronautas", "planetas", "foguete", "espaco sideral", "tema espacial"),
    "ocean": ("praia", "oceano", "fundo do mar"),
    "dinosaurs": ("dinossauro", "dinossauros"),
    "reading": ("leitura", "livros", "ler historias"),
}


def _normalized(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return re.sub(r"\s+", " ", "".join(c for c in value if not unicodedata.combining(c)).lower()).strip()


def _positive_mention(message: str, phrase: str) -> bool:
    for match in re.finditer(r"\b" + re.escape(phrase) + r"\b", message):
        preceding = message[max(0, match.start()-60):match.start()]
        preceding = re.split(r"[.!?;,\n]", preceding)[-1]
        if not re.search(r"\b(nao|sem|evite|odeia|detesta)\b", preceding):
            return True
    return False


def scene_context(occasion: str | None, message: str | None = None) -> dict[str, str | None]:
    """Select stable, allowlisted scene details without another provider call."""
    occasion = _normalized(occasion)
    kind = {
        "aniversario": "birthday", "natal": "christmas", "medos": "reassurance",
        "motivacao": "achievement", "parabens": "achievement", "formatura": "graduation",
        "dia-das-maes": "family", "dia-dos-pais": "family", "casamento": "wedding",
        "amor": "affection", "boas_festas": "christmas",
    }.get(occasion, "greeting")
    message = _normalized(message)
    if kind == "greeting" and _positive_mention(message, "aniversario"):
        kind = "birthday"
    theme = next((name for name, words in THEME_WORDS.items()
                  if any(_positive_mention(message, word) for word in words)), None)
    return {"kind": kind, "theme": theme}


def scene_details(context: dict) -> str:
    # Look up keys rather than interpolating customer-supplied descriptions.
    kind = context.get("kind") if context.get("kind") in SETTINGS else "greeting"
    theme = THEMES.get(context.get("theme"), "")
    return f"{SETTINGS[kind]}. Include {PROPS[kind]}. " + (f"Add {theme}. " if theme else "")


def background_prompt(occasion: str | None, *, context: dict | None = None) -> str:
    return (
        f"Create polished illustrated scenery: {scene_details(context or scene_context(occasion))} "
        "Vertical 9:16 full-bleed scenery, edge to edge, foreground and background vegetation. "
        "Leave visual space in the center for a portrait; keep the occasion props visible to either side, not hidden behind it. No people, characters, faces, statues, "
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
        subject = ImageOps.contain(subject, (int(scene.width * .88), int(scene.height * .80)), Image.Resampling.LANCZOS)
        scene.alpha_composite(subject, ((scene.width-subject.width)//2, scene.height-subject.height))
        output=io.BytesIO(); scene.convert("RGB").save(output,"PNG"); return output.getvalue()


def scene_prompt(names: list[str], occasion: str | None, *, context: dict | None = None) -> str:
    setting = scene_details(context or scene_context(occasion))
    return (
        f"Create a finished vertical 9:16 cameo scene for {len(names)} character(s). "
        f"Use the supplied character images as strict identity references. Cast, left to right: {', '.join(names)}. "
        "Preserve the exact original faces, species, age, hair, clothing, colors, proportions and individual art styles. "
        "Do not substitute a generic lookalike or redesign the character. No extra people or characters. "
        f"Place them naturally in {setting}. "
        "Build one coherent, thoughtfully decorated scene with foreground, middle ground and background depth; "
        "use a limited, coordinated palette rather than filling every space with clutter. "
        "Keep the occasion props clearly visible without covering faces. Props rest on tables or scenery: "
        "do not invent hands, change anatomy or replace the character's existing accessories to hold them. "
        "Replace ALL reference backgrounds, including white backgrounds and checkerboard patterns. "
        "The scenery must fill the entire frame edge to edge: no white margins, panels, borders, collage or letterboxing. "
        "Frame the character prominently, filling about 65-80 percent of the frame height; "
        "if the reference is a portrait, retain waist-up framing rather than inventing a new full-body design. "
        "Keep faces and hands visible with comfortable headroom. Coherent lighting, clean polished composition. "
        "No text, captions, names, age numbers, watermarks or logos. Banners and cake decorations must be unlettered."
    )


def validate_scene_dimensions(data: bytes) -> None:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.width < 450 or image.height < 800 or abs(image.width / image.height - 9 / 16) > 0.02:
            raise ValueError("scene must be a full-resolution portrait image")


async def review_scene(reference_urls: list[str], scene_url: str, *, context: dict | None = None) -> dict:
    settings = get_settings()
    content = [{"type": "text", "text": (
        "Compare the original character references (all images except the last) with the final scene (last image). "
        "Return JSON booleans identity_preserved, full_background, prominent_subjects, and a short reason. "
        "identity_preserved: every requested character keeps its face, species, colors, outfit and art style, with no extra character. "
        "full_background: a real, coherent setting fills ALL edges, with no blank white bands, checkerboard, collage or letterboxing. "
        "prominent_subjects: characters occupy a substantial part of the frame and faces are readable, not tiny figures. "
        "Be strict. An attractive but different character fails."
    )}]
    if context:
        content[0]["text"] += (
            " Also return occasion_appropriate: the following occasion setting and its primary props are visible, "
            "coherent and do not obscure the character; there are no generated names or lettering. "
            f"Expected scene: {scene_details(context)}"
        )
    content += [{"type": "image_url", "image_url": {"url": url, "detail": "low"}} for url in [*reference_urls, scene_url]]
    result = await AsyncOpenAI(api_key=settings.openai_api_key).chat.completions.create(
        model=settings.openai_model, messages=[{"role": "user", "content": content}],
        response_format={"type": "json_object"}, max_completion_tokens=500,
    )
    await record_openai_usage(getattr(result,'usage',None),settings.openai_model)
    verdict = json.loads(result.choices[0].message.content or "{}")
    required = ["identity_preserved", "full_background", "prominent_subjects"]
    if context:
        required.append("occasion_appropriate")
    if not all(verdict.get(key) is True for key in required):
        raise ValueError("scene failed character/background review; manual review required")
    return verdict
