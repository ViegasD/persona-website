"""Runtime configuration loaded from environment variables.

Values follow the same names as the Node backend whenever a service is shared
(Postgres, Redis, S3, MercadoPago, OpenAI, Evolution API). Storefront-specific
values use the ``STOREFRONT_`` prefix or distinct names.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    env: str = Field("development", alias="STOREFRONT_ENV")
    api_base_url: str = Field("http://localhost:8000", alias="STOREFRONT_API_BASE_URL")
    # Comma-separated list of allowed CORS origins, e.g. "https://persona.com,http://localhost:3000"
    web_base_url: str = Field("http://localhost:3000", alias="STOREFRONT_WEB_BASE_URL")
    log_level: str = Field("INFO", alias="STOREFRONT_LOG_LEVEL")

    # --- Database (shared with Node backend; we only write to schema "web") ---
    database_url: str = Field(..., alias="DATABASE_URL")
    db_schema: str = Field("web", alias="STOREFRONT_DB_SCHEMA")

    # --- Redis (separate DB index from Node BullMQ) ---
    redis_url: str = Field("redis://redis:6379/1", alias="STOREFRONT_REDIS_URL")

    # --- S3 / MinIO (shared; storefront uses prefix "web/") ---
    s3_endpoint: str = Field(..., alias="S3_ENDPOINT")
    s3_region: str = Field("us-east-1", alias="S3_REGION")
    s3_bucket: str = Field(..., alias="S3_BUCKET")
    s3_access_key: str = Field(..., alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(..., alias="S3_SECRET_KEY")
    s3_force_path_style: bool = Field(True, alias="S3_FORCE_PATH_STYLE")
    s3_storefront_prefix: str = Field("web", alias="STOREFRONT_S3_PREFIX")

    # --- Mercado Pago (shared account; external_reference prefixed "web_") ---
    mercadopago_access_token: str = Field(..., alias="MERCADOPAGO_ACCESS_TOKEN")
    mercadopago_webhook_secret: str | None = Field(None, alias="MERCADOPAGO_WEBHOOK_SECRET")

    # --- OpenAI (script writer; mirrors Node backend) ---
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")

    # --- kie.ai (Nano Banana Pro composite) ---
    kie_api_key: str | None = Field(None, alias="KIE_API_KEY")
    kie_api_base: str = Field("https://api.kie.ai/api/v1", alias="KIE_API_BASE")
    kie_nano_banana_model: str = Field(
        "google/nano-banana-pro", alias="KIE_NANO_BANANA_MODEL"
    )
    kie_callback_url: str | None = Field(None, alias="KIE_CALLBACK_URL")

    # --- xAI Grok Imagine (image-to-video) ---
    xai_api_key: str | None = Field(None, alias="XAI_API_KEY")
    xai_api_base: str = Field("https://api.x.ai/v1", alias="XAI_API_BASE")
    xai_video_model: str = Field("grok-imagine-video", alias="XAI_VIDEO_MODEL")
    xai_video_duration_seconds: int = Field(10, alias="XAI_VIDEO_DURATION_SECONDS")
    xai_video_aspect_ratio: str = Field("9:16", alias="XAI_VIDEO_ASPECT_RATIO")
    xai_video_resolution: str = Field("720p", alias="XAI_VIDEO_RESOLUTION")

    # --- Batch policy ---
    batch_auto_threshold: int = Field(5, alias="BATCH_AUTO_THRESHOLD")
    batch_max_age_minutes: int = Field(60, alias="BATCH_MAX_AGE_MINUTES")
    batch_concurrency: int = Field(4, alias="BATCH_CONCURRENCY")

    # --- Auth ---
    jwt_secret: str = Field(..., alias="JWT_SECRET")
    jwt_expiry_hours: int = Field(24 * 30, alias="STOREFRONT_JWT_EXPIRY_HOURS")
    guest_cookie_secret: str = Field(..., alias="STOREFRONT_GUEST_COOKIE_SECRET")
    admin_api_key: str = Field(..., alias="STOREFRONT_ADMIN_API_KEY")

    # --- WhatsApp delivery via Evolution API ---
    evolution_api_url: str | None = Field(None, alias="EVOLUTION_API_URL")
    evolution_api_key: str | None = Field(None, alias="EVOLUTION_API_KEY")
    evolution_instance_name: str | None = Field(None, alias="EVOLUTION_INSTANCE_NAME")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
