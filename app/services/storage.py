"""S3 / MinIO helpers (boto3 sync, called from a thread pool when needed)."""

from __future__ import annotations

from typing import IO

import boto3
from botocore.client import Config

from app.core.settings import get_settings


_settings = get_settings()


def _client():  # noqa: ANN202 - boto3 client is dynamic
    return boto3.client(
        "s3",
        endpoint_url=_settings.s3_endpoint,
        region_name=_settings.s3_region,
        aws_access_key_id=_settings.s3_access_key,
        aws_secret_access_key=_settings.s3_secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if _settings.s3_force_path_style else "auto"},
        ),
    )


def storefront_key(*parts: str) -> str:
    """Build an object key under the storefront prefix (e.g. ``web/orders/12/...``)."""
    return "/".join([_settings.s3_storefront_prefix.strip("/"), *[p.strip("/") for p in parts]])


def upload_bytes(key: str, data: bytes | IO[bytes], content_type: str = "application/octet-stream") -> str:
    """Upload raw bytes and return the object key."""
    body = data if isinstance(data, (bytes, bytearray)) else data.read()
    _client().put_object(
        Bucket=_settings.s3_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )
    return key


def presigned_get_url(key: str, expires_in: int = 3600) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": _settings.s3_bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def download_bytes(key: str) -> bytes:
    obj = _client().get_object(Bucket=_settings.s3_bucket, Key=key)
    return obj["Body"].read()
