"""Unit tests for the Mercado Pago helpers."""

from __future__ import annotations

import hashlib
import hmac

from app.services import mercadopago_client


def test_external_reference_roundtrip() -> None:
    ref = mercadopago_client.make_external_reference(123)
    assert ref == "web_123"
    assert mercadopago_client.parse_external_reference(ref) == 123
    assert mercadopago_client.parse_external_reference("persona_999") is None
    assert mercadopago_client.parse_external_reference(None) is None


def test_verify_signature_no_secret_returns_true() -> None:
    assert mercadopago_client.verify_webhook_signature(
        secret=None, x_signature="ts=1,v1=abc", x_request_id="r", data_id="42"
    )


def test_verify_signature_matches_known_manifest() -> None:
    secret = "shhh"
    ts = "1700000000"
    data_id = "abc"  # already lowercase
    request_id = "req-1"
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    v1 = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    assert mercadopago_client.verify_webhook_signature(
        secret=secret,
        x_signature=f"ts={ts},v1={v1}",
        x_request_id=request_id,
        data_id=data_id,
    )


def test_verify_signature_rejects_bad_v1() -> None:
    assert not mercadopago_client.verify_webhook_signature(
        secret="shhh",
        x_signature="ts=1700000000,v1=" + "0" * 64,
        x_request_id="req-1",
        data_id="abc",
    )


def test_qr_png_renders() -> None:
    png = mercadopago_client.render_qr_png("00020126360014BR.GOV.BCB.PIX...")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
