"""Availability flags are public booleans, never private credentials."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import config


@pytest.mark.parametrize("mail,processing", [(False, False), (True, False), (True, True)])
def test_public_config_reports_availability_without_secrets(monkeypatch, mail, processing):
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(mercadopago_public_key="public-key"))
    monkeypatch.setattr(config, "mail_is_configured", lambda: mail)
    monkeypatch.setattr(config, "processing_enabled", lambda: processing)
    app = FastAPI()
    app.include_router(config.router)
    with TestClient(app) as client:
        response = client.get("/config")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "mp_public_key": "public-key",
        "account_email_enabled": mail,
        "video_processing_enabled": processing,
    }
