import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import HTTPException
from google.auth.exceptions import TransportError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CreditTransactionType, User
from app.services import google_auth


@pytest.mark.asyncio
async def test_google_disabled_fails_closed(monkeypatch):
    monkeypatch.setattr(google_auth.get_settings(), "google_client_id", None)
    with pytest.raises(HTTPException) as error:
        await google_auth.verify_credential("credential", "nonce")
    assert error.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"nonce": "other"}, {"email_verified": False}, {"sub": ""}, {"email": "bad"}])
async def test_google_rejects_invalid_claims(monkeypatch, change):
    monkeypatch.setattr(google_auth.get_settings(), "google_client_id", "test.apps.googleusercontent.com")
    claims = {"sub": "123", "email": "person@gmail.com", "email_verified": True, "nonce": "nonce"} | change
    monkeypatch.setattr(google_auth.id_token, "verify_oauth2_token", MagicMock(return_value=claims))
    with pytest.raises(HTTPException) as error:
        await google_auth.verify_credential("credential", "nonce")
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_google_library_validates_signature_and_audience(monkeypatch):
    monkeypatch.setattr(google_auth.get_settings(), "google_client_id", "test.apps.googleusercontent.com")
    verifier = MagicMock(side_effect=ValueError("bad audience or signature"))
    monkeypatch.setattr(google_auth.id_token, "verify_oauth2_token", verifier)
    with pytest.raises(HTTPException):
        await google_auth.verify_credential("credential", "nonce")
    assert verifier.call_args.args[2] == "test.apps.googleusercontent.com"


def test_google_email_authority():
    assert google_auth.email_is_authoritative({"email": "a@gmail.com", "email_verified": True})
    assert google_auth.email_is_authoritative({"email": "a@company.com", "email_verified": True, "hd": "company.com"})
    assert not google_auth.email_is_authoritative({"email": "a@example.com", "email_verified": True})


@pytest.fixture(scope="module")
def signing_keys():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Offline Google auth test")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1)).sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{}, {"aud": "wrong-client"}, {"iss": "https://attacker.invalid"}, {"exp": 1}, {"bad_signature": True},
                                    {"iat": None}, {"iat": "invalid"}, {"exp": None}, {"exp": "invalid"}])
async def test_offline_signed_tokens_use_real_google_verifier(monkeypatch, signing_keys, change):
    from cryptography.hazmat.primitives.asymmetric import rsa

    key, cert = signing_keys
    audience = "test.apps.googleusercontent.com"
    monkeypatch.setattr(google_auth.get_settings(), "google_client_id", audience)
    response = SimpleNamespace(status=200, data=json.dumps({"test-key": cert}).encode())
    monkeypatch.setattr(google_auth, "GoogleRequest", lambda: lambda *args, **kwargs: response)
    claims = {"iss": "https://accounts.google.com", "aud": audience, "sub": "12345",
              "email": "person@gmail.com", "email_verified": True, "nonce": "nonce",
              "iat": int(time.time()), "exp": int(time.time()) + 300} | change
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if change.get("bad_signature") else key
    credential = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key"})
    if change:
        with pytest.raises(HTTPException) as rejected:
            await google_auth.verify_credential(credential, "nonce")
        assert rejected.value.status_code == 401
    else:
        verified = await google_auth.verify_credential(credential, "nonce")
        assert verified["sub"] == "12345"


@pytest.mark.asyncio
async def test_google_transport_failure_remains_temporarily_unavailable(monkeypatch):
    monkeypatch.setattr(google_auth.get_settings(), "google_client_id", "test.apps.googleusercontent.com")
    monkeypatch.setattr(google_auth.id_token, "verify_oauth2_token",
                        MagicMock(side_effect=TransportError("offline")))
    with pytest.raises(HTTPException) as error:
        await google_auth.verify_credential("credential", "nonce")
    assert error.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("link", [True, False], ids=["explicit-link", "stable-sub-login"])
@pytest.mark.parametrize("domain,authority", [("gmail.com", {}), ("company.com", {"hd": "company.com"})])
async def test_legacy_mixed_case_email_gets_verified_and_one_welcome_credit(monkeypatch, link, domain, authority):
    stored_email = "Legacy.Person@" + domain.upper()
    user = User(id=17, email=stored_email, google_sub=None if link else "stable-sub",
                token_version=0, credit_balance=0, credit_debt=0)
    claims = {"sub": "stable-sub", "email": "legacy.person@" + domain,
              "email_verified": True, "nonce": "nonce"} | authority
    monkeypatch.setattr(google_auth, "verify_credential", AsyncMock(return_value=claims))
    session = MagicMock(spec=AsyncSession)
    added = []
    session.add.side_effect = added.append
    values = ["nonce-hash", None, None, None if link else user]
    if link:
        values.append(user)
    values += [None, user.id, "next-nonce-hash", None, None, user, 23, None]
    session.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda value=value: value,
                                                  scalar_one=lambda value=value: value) for value in values]

    result = await google_auth.authenticate(session, "credential", "nonce", user.id if link else None,
                                            expected_token_version=0 if link else None)
    assert result is user
    assert user.email_verified_at is not None
    assert user.credit_balance == 1
    assert user.email == stored_email and user.google_sub == "stable-sub"
    verified_at = user.email_verified_at

    await google_auth.authenticate(session, "credential", "next-nonce")
    assert user.email_verified_at == verified_at
    assert user.credit_balance == 1
    assert len(added) == 1
    transaction = added[0]
    assert transaction.user_id == 17 and transaction.kind == CreditTransactionType.SIGNUP_BONUS
    assert transaction.amount == 1 and transaction.balance_after == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_email,google_email,authority", [
    ("Original@GMAIL.COM", "changed@gmail.com", {}),
    ("Original@GMAIL.COM", "original+alias@gmail.com", {}),
    (None, "original@gmail.com", {}),
    ("Original@EXAMPLE.COM", "original@example.com", {}),
])
async def test_google_login_does_not_verify_or_credit_an_unmatched_or_non_authoritative_email(
        monkeypatch, stored_email, google_email, authority):
    user = User(id=17, email=stored_email, google_sub="stable-sub", token_version=0,
                credit_balance=0, credit_debt=0)
    claims = {"sub": "stable-sub", "email": google_email, "email_verified": True, "nonce": "nonce"} | authority
    monkeypatch.setattr(google_auth, "verify_credential", AsyncMock(return_value=claims))
    session = MagicMock(spec=AsyncSession)
    session.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda value=value: value)
                                  for value in ["nonce-hash", None, None, user]]

    result = await google_auth.authenticate(session, "credential", "nonce")
    assert result is user and user.google_sub == "stable-sub"
    assert user.email == stored_email
    assert user.email_verified_at is None and user.credit_balance == 0
