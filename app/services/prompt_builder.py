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


def _build_shared_header(scene_description: str) -> list[str]:
    """Header lines shared by both single and multi-character prompts."""
    parts: list[str] = []
    parts.append(
        f"{scene_description.strip()} "
        "Reproduce the character(s) EXACTLY as shown in the reference image — "
        "same art style, same colors, same outfit, same proportions, same face. "
        "Do not redesign, reinterpret, or change any visual detail."
    )
    parts.append("")
    parts.append(
        "CRITICAL — REFERENCE IMAGE FIDELITY: "
        "The character's appearance, clothing, colors, art style and proportions must be "
        "IDENTICAL to the reference image throughout the entire video. "
        "Any deviation from the reference image is not acceptable."
    )
    parts.append("")
    parts.append(
        "IMPORTANT: The character must remain in the exact same position and orientation as the "
        "reference image, always facing directly toward the camera, no side view, no rotation, "
        "no change in body position."
    )
    parts.append("")
    parts.append(
        "Camera must be completely static and fixed. No zoom, no pan, no reframing, no movement at all."
    )
    parts.append("")
    return parts


def _build_single_character_prompt(inputs: VideoPromptInputs) -> str:
    """Simple monologue prompt for a single-character video."""
    char = inputs.characters[0]
    parts = _build_shared_header(inputs.scene_description)

    parts.append(
        f"The video is a continuous {inputs.duration_seconds}-second shot. "
        "The character speaks naturally throughout."
    )
    parts.append("")

    descriptor = f" ({char.descriptor})" if char.descriptor else ""
    parts.append(f"The character{descriptor} says in Brazilian Portuguese:")
    # Combine the individual line and the group line into one natural speech
    speech = f"{char.line_pt.strip()} {inputs.group_line_pt.strip()}"
    parts.append(speech)
    parts.append("")
    parts.append(
        f"Timing guidance: all speech must finish before the last 2 seconds to avoid being cut off."
    )
    parts.append("")
    parts.append("Animation: lips, blinking, natural body and facial expression")
    parts.append(
        "Audio: clear Brazilian Portuguese, natural voice matching the character, accurate lip sync"
    )

    return "\n".join(parts).strip()


def _build_multi_character_prompt(inputs: VideoPromptInputs) -> str:
    """Turn-based scene progression prompt for multi-character videos."""
    n = len(inputs.characters)
    labels = POSITIONAL_LABELS_BY_COUNT.get(n) or [f"#{i + 1}" for i in range(n)]

    parts = _build_shared_header(inputs.scene_description)
    # Override the "character" wording to plural
    parts[2] = (
        "IMPORTANT: All characters must remain in the exact same positions and orientation as the "
        "reference image, always facing directly toward the camera, no side view, no rotation, "
        "no change in body position."
    )

    parts.append(
        f"The video is a continuous {inputs.duration_seconds}-second shot. "
        "Only one character speaks at a time, while the others remain still with subtle idle "
        "animation (blinking only)."
    )
    parts.append("")
    parts.append("Scene progression (no camera movement, only speaking turns):")
    parts.append("")

    for idx, (label, char) in enumerate(zip(labels, inputs.characters, strict=True)):
        ordinal = "First" if idx == 0 else "Then"
        descriptor = f" ({char.descriptor})" if char.descriptor else ""
        parts.append(
            f"{ordinal}, the {label} character{descriptor} speaks, maintaining the same pose, "
            "only moving lips and slight facial expression."
        )
        parts.append("They say in Brazilian Portuguese:")
        parts.append(char.line_pt.strip())
        parts.append("")
        parts.append("Then the character becomes still.")
        parts.append("")

    # ── Final group line ──────────────────────────────────────────────
    parts.append(
        "Final moment: all characters speak together, still in the exact same pose, "
        "synchronized lip movement and natural facial expression."
    )
    parts.append("This final line must start early enough to finish before the video ends.")
    parts.append("All characters say together in sync:")
    parts.append(inputs.group_line_pt.strip())
    parts.append("")
    parts.append(
        "Timing guidance: the final group line must begin before the last 2 seconds to avoid being cut off."
    )
    parts.append("")
    parts.append("Animation: lips, blinking, natural body and facial expression")
    parts.append(
        "Audio: clear Brazilian Portuguese, natural voices matching the characters, accurate lip sync, no overlap except final line"
    )

    return "\n".join(parts).strip()


def build_video_prompt(inputs: VideoPromptInputs) -> str:
    n = len(inputs.characters)
    if n < 1:
        raise ValueError("at least one character is required")
    if n == 1:
        return _build_single_character_prompt(inputs)
    return _build_multi_character_prompt(inputs)
