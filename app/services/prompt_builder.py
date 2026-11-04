"""Reference-preserving cameos with paced dialogue and controlled performance."""

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
    voice_direction: str = ""  # Curated direction, never spoken dialogue.


def character_voice_direction(character_id: str, gender: str | None = None) -> str:
    """Use explicit catalog metadata rather than guessing a voice from the image."""
    if character_id == "gato-de-botas":
        return ("An adult male Brazilian Portuguese speaking voice: warm, resonant, lower-register, "
                "confident and playfully heroic. Clearly masculine, never feminine or childlike. "
                "Do not imitate a specific real actor.")
    value = (gender or "").strip().lower()
    if value in {"masculino", "male", "masculine"}:
        return "A masculine Brazilian Portuguese speaking voice appropriate to this character's age and personality."
    if value in {"feminino", "female", "feminine"}:
        return "A feminine Brazilian Portuguese speaking voice appropriate to this character's age and personality."
    return ""


@dataclass(slots=True)
class VideoPromptInputs:
    scene_description: str
    """One-sentence scene setup, e.g. "Three stylized 3D K-pop warrior girls
    standing side by side on a luxurious golden theater stage..."""

    characters: list[CharacterLine]
    group_line_pt: str
    duration_seconds: int = 10
    action_direction: str = ''


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
        "IMPORTANT: Keep the character anchored in the reference composition; preserve existing viewing angles and anatomy. "
        "Allow small natural head nods and safe gestures without walking, turning away, changing outfits or changing the scene."
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
    if char.voice_direction:
        parts.append(f"Voice direction (not spoken dialogue): {char.voice_direction}")
    parts.append(f"The character{descriptor} says in Brazilian Portuguese:")
    # Combine the individual line and the group line into one natural speech
    speech = f"{char.line_pt.strip()} {inputs.group_line_pt.strip()}"
    parts.append(speech)
    parts.append("")
    parts.append(
        f"Timing guidance: begin speaking within the first 0.7 seconds. Spread the dialogue naturally across the shot, finishing around {max(1,inputs.duration_seconds-1.5):g}s. "
        "Do not rush every line into the first half and then freeze. Do not stretch or repeat syllables to fill time."
    )
    parts.append("")
    parts.append('Performance: '+(inputs.action_direction or 'Warm eye contact, small conversational nods and expressive facial reactions, followed by a gentle closing smile.'))
    parts.append('Keep props supported, faces and outfits unchanged, and all lettering stable. No invented fingers, added limbs, prop teleportation or long frozen tail. No new dialogue beyond the supplied lines.')
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
    parts[4] = (
        "IMPORTANT: All characters remain in their reference positions with existing viewing angles and unchanged anatomy. "
        "Allow small head nods and gentle reactions without swapping positions, walking or turning away."
    )

    parts.append(
        f"The video is a continuous {inputs.duration_seconds}-second shot. "
        "Only one character speaks at a time; the others listen with gentle facial reactions and small nods. "
        "Begin speech within 0.7s and distribute turns across the shot, finishing around "
        f"{max(1,inputs.duration_seconds-1.5):g}s. Avoid rushing the entire dialogue into the first half."
    )
    parts.append("")
    parts.append("Scene progression (no camera movement, only speaking turns):")
    parts.append("")

    for idx, (label, char) in enumerate(zip(labels, inputs.characters, strict=True)):
        ordinal = "First" if idx == 0 else "Then"
        descriptor = f" ({char.descriptor})" if char.descriptor else ""
        parts.append(
            f"{ordinal}, the {label} character{descriptor} speaks with accurate lip sync, warm eye contact and small conversational nods."
        )
        if char.voice_direction:
            parts.append(f"Voice for the {label} speaker (not spoken dialogue): {char.voice_direction}")
        parts.append("They say in Brazilian Portuguese:")
        parts.append(char.line_pt.strip())
        parts.append("")
        parts.append("Then the character listens with a gentle facial reaction while the next speaker begins.")
        parts.append("")

    # ── Final group line ──────────────────────────────────────────────
    if not inputs.group_line_pt.strip():
        parts.append('Performance: '+(inputs.action_direction or 'Small natural nods, attentive expressions and a warm closing smile.'))
        parts.append('Keep all props supported, outfits, faces and lettering stable. No added fingers, prop teleportation, repeated dialogue or long frozen tail.')
        parts.append("Audio: clear Brazilian Portuguese, natural voices matching the characters, accurate lip sync, no overlap.")
        return "\n".join(parts).strip()
    parts.append(
        "Final moment: all characters speak together, remaining anchored in their reference positions, "
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
    parts.append('Performance: '+(inputs.action_direction or 'Natural small head nods, warm expressions and a gentle closing smile.'))
    parts.append('Keep props supported, anatomy, outfits, faces and lettering stable; no long frozen tail.')
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
