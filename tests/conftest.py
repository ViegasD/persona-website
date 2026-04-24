"""Pure-function tests that don't require Postgres / Redis / network."""

from __future__ import annotations

import os

# Provide minimal env so settings load.
os.environ.setdefault("DATABASE_URL", "postgresql://x:y@localhost:5432/x")
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("S3_BUCKET", "x")
os.environ.setdefault("S3_ACCESS_KEY", "x")
os.environ.setdefault("S3_SECRET_KEY", "x")
os.environ.setdefault("MERCADOPAGO_ACCESS_TOKEN", "x")
os.environ.setdefault("OPENAI_API_KEY", "x")
os.environ.setdefault("JWT_SECRET", "x" * 32)
os.environ.setdefault("STOREFRONT_GUEST_COOKIE_SECRET", "y" * 32)
os.environ.setdefault("STOREFRONT_ADMIN_API_KEY", "admin-key")
