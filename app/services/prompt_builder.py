"""Builds the Grok Imagine Video prompt.

The text follows the **strict template** that the user proved works well:
locked camera, locked body/hand pose, only lip-sync + blink + micro head
movement, one character speaks at a time, final group line that starts
before the last 2 s. We keep every constraint identical and only inject:

* a short scene description,
* per-character speaker lines (with positional ordinal),
* the final group line.

The structure was tuned to remove degrees of freedom from the model — do
**not** loosen any of these constraints when changing this file.
"""

from __future__ import annotations

from dataclasses import dataclass

POSITIONAL_LABELS_BY_COUNT: dict[int, list[str]] = {
    1: ["CENTER"],
    2: ["LEFT", "RIGHT"],
    3: ["LEFT", "CENTER", "RIGHT"],
    4: ["FAR LEFT", "LEFT", "RIGHT", "FAR RIGHT"],
    5: ["FAR LEFT", "LEFT", "CENTER", "RIGHT", "FAR RIGHT"],
}


@dataclass(slots=True)
class CharacterLine:
    """One spoken line from one character."""

    descriptor: str  # short visual hint, e.g. "short black hair in two buns"
    line_pt: str  # the Portuguese line itself


@dataclass(slots=True)
class VideoPromptInputs:
    scene_description: str
    """One-sentence scene setup, e.g. "Three stylized 3D K-pop warrior girls
    standing side by side on a luxurious golden theater stage..."""

    characters: list[CharacterLine]
    group_line_pt: str
    duration_seconds: int = 10


def build_video_prompt(inputs: VideoPromptInputs) -> str:
    n = len(inputs.characters)
    if n < 1:
        raise ValueError("at least one character is required")
    labels = POSITIONAL_LABELS_BY_COUNT.get(n) or [f"#{i + 1}" for i in range(n)]

    # ── Header ─────────────────────────────────────────────────────────
    parts: list[str] = []
    parts.append(
        f"{inputs.scene_description.strip()} "
        "high detail, vibrant colors, cinematic lighting, perfectly consistent with the reference image."
    )
    parts.append("")
    parts.append(
        "IMPORTANT: All characters must remain in the exact same positions and orientation as the "
        "reference image, always facing directly toward the camera, no side view, no rotation, "
        "no change in body position."
    )
    parts.append("")
    parts.append(
        "Camera must be completely static and fixed. No zoom, no pan, no reframing, no movement at all."
    )
    parts.append("")
    parts.append("STRICT ANIMATION RULES:")
    parts.append("")
    parts.append("No hand movement at all")
    parts.append("No arm movement")
    parts.append("Hands must remain completely still in their original position")
    parts.append("No waving, no gestures, no pose changes")
    parts.append(
        "Only allowed movement: mouth (lip sync), blinking, very subtle head micro-movements"
    )
    parts.append("")

    # ── Body / scene progression ──────────────────────────────────────
    parts.append(
        f"The video is a continuous {inputs.duration_seconds}-second shot. "
        "Only one character speaks at a time, while the others remain still with subtle idle "
        "animation (blinking only)."
    )
    parts.append("")
    parts.append("Scene progression (no camera movement, only speaking turns):")
    parts.append("")

    for idx, (label, char) in enumerate(zip(labels, inputs.characters, strict=True)):
        ordinal = "First" if idx == 0 else ("Then" if idx < n - 1 else "Then")
        descriptor = f" ({char.descriptor})" if char.descriptor else ""
        parts.append(
            f"{ordinal}, the {label} character{descriptor} speaks, maintaining the same pose, "
            "only moving lips and slight facial expression."
        )
        parts.append("She says in Brazilian Portuguese:")
        parts.append(char.line_pt.strip())
        parts.append("")
        parts.append("Then she becomes still.")
        parts.append("")

    # ── Final group line ──────────────────────────────────────────────
    parts.append(
        "Final moment: all characters speak together, still in the exact same pose, "
        "no body or hand movement, only synchronized lip movement and subtle facial expression."
    )
    parts.append("This final line must start early enough to finish before the video ends.")
    if n > 1:
        parts.append("All characters say together in sync:")
    else:
        parts.append("She says:")
    parts.append(inputs.group_line_pt.strip())
    parts.append("")
    parts.append(
        f"Timing guidance: the final group line must begin before the last 2 seconds to avoid being cut off."
    )
    parts.append("")
    parts.append("Animation: lips, blinking, minimal facial expression only — no body movement")
    parts.append(
        "Audio: clear Brazilian Portuguese, natural female voices, accurate lip sync, no overlap except final line"
    )
    parts.append(
        "Style: cute, vibrant, polished animation, no distortion, no style drift"
    )

    return "\n".join(parts).strip()
