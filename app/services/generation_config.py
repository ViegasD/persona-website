"""Snapshot admin generation settings once per video, retaining environment defaults."""
from dataclasses import dataclass
from app.core.settings import get_settings
from app.services.storefront_policy import read_policy


@dataclass(frozen=True)
class GenerationConfig:
    duration: int
    model: str


async def read_generation_config(session) -> GenerationConfig:
    policy = await read_policy(session)
    settings = get_settings()
    return GenerationConfig(
        policy.video_duration_seconds or settings.xai_video_duration_seconds,
        policy.video_model or settings.xai_video_model,
    )
