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
os.environ.setdefault("XAI_API_KEY", "offline-test-key")
os.environ.setdefault("STOREFRONT_WORKER_ENABLED", "true")
os.environ.setdefault("STOREFRONT_PROCESSING_START_AT", "2000-01-01T00:00:00+00:00")
os.environ.setdefault("STOREFRONT_RECOVERY_ENABLED", "true")

import pytest
from unittest.mock import Mock
from app.services import storefront_policy


@pytest.fixture(autouse=True)
def isolate_policy_for_component_mocks(monkeypatch, request):
    if request.node.path.name == "test_storefront_policy.py":
        return
    original = storefront_policy.read_policy
    async def read(session):
        if isinstance(session, Mock):
            return storefront_policy.PolicyValues()
        return await original(session)
    monkeypatch.setattr(storefront_policy, "read_policy", read)
