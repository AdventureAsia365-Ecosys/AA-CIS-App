"""AA-680 — CIS issues RS256 staff JWTs for AA-Booking and publishes the JWKS that verifies them.

Contract (root ADR 0004, O1): POST /auth/booking-token swaps a CIS staff session for a 15-minute
token with iss=https://api-cis.lumiguides.it.com, aud=booking, sub=admin_users.id, cis_role and a
`kid` header; GET /auth/.well-known/jwks.json publishes the current + previous public keys.
"""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from api.core import booking_jwt
from api.main import app, get_pool
from api.routers.auth import JWT_ALG, JWT_SECRET, _create_admin_jwt

ADMIN_ID = "6f1c2c4e-8a51-4c1e-9d0b-2f3c4d5e6f70"
ISS = "https://api-cis.lumiguides.it.com"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_private(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _pem_public(key):
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


@pytest.fixture(scope="module")
def keypair():
    return {"current": _rsa_key(), "previous": _rsa_key()}


@pytest.fixture
def secret(monkeypatch, keypair):
    """Patch the Secrets Manager read with a generated key; yields the call log."""
    calls = []
    value = json.dumps({
        "kid": "bk-test-2",
        "private_key_pem": _pem_private(keypair["current"]),
        "previous": [{"kid": "bk-test-1", "public_key_pem": _pem_public(keypair["previous"])}],
    })

    def fake_fetch(name):
        calls.append(name)
        return value

    monkeypatch.setattr(booking_jwt, "_fetch_secret_string", fake_fetch)
    booking_jwt.reset_key_cache()
    yield calls
    booking_jwt.reset_key_cache()


@pytest.fixture
def missing_secret(monkeypatch):
    def fake_fetch(name):
        raise RuntimeError("ResourceNotFoundException")

    monkeypatch.setattr(booking_jwt, "_fetch_secret_string", fake_fetch)
    booking_jwt.reset_key_cache()
    yield
    booking_jwt.reset_key_cache()


class _FakeConn:
    def __init__(self, row):
        self.row = row
        self.args = None

    async def fetchrow(self, sql, *args):
        self.args = args
        return self.row


class _FakePool:
    def __init__(self, row):
        self.conn = _FakeConn(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _staff_row(role="reviewer"):
    return {"id": ADMIN_ID, "username": "linh.sales", "role": role}


@pytest.fixture
def client_with(request):
    def make(row):
        fake = _FakePool(row)
        app.dependency_overrides[get_pool] = lambda: fake
        return TestClient(app), fake

    yield make
    app.dependency_overrides.pop(get_pool, None)


def _jwks_key_for(token, jwks_doc):
    kid = jwt.get_unverified_header(token)["kid"]
    jwk = next(k for k in jwks_doc["keys"] if k["kid"] == kid)
    return jwt.PyJWK(jwk).key


# ── POST /auth/booking-token ─────────────────────────────────────────────────

def test_mints_rs256_token_with_contract_claims(client_with, secret):
    client, _ = client_with(_staff_row("reviewer"))
    session = _create_admin_jwt(ADMIN_ID, "linh.sales", "reviewer")
    res = client.post("/auth/booking-token", headers={"Authorization": f"Bearer {session}"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900

    token = body["access_token"]
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["kid"] == "bk-test-2"

    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == ISS
    assert claims["aud"] == "booking"
    assert claims["sub"] == ADMIN_ID
    assert claims["cis_role"] == "reviewer"
    assert claims["name"] == "linh.sales"
    assert claims["exp"] - claims["iat"] == 900
    uuid.UUID(claims["jti"])


def test_session_cookie_is_accepted(client_with, secret):
    client, _ = client_with(_staff_row("admin"))
    client.cookies.set("cis_admin_token", _create_admin_jwt(ADMIN_ID, "linh.sales", "admin"))
    try:
        res = client.post("/auth/booking-token")
    finally:
        client.cookies.clear()
    assert res.status_code == 200, res.text
    assert jwt.decode(res.json()["access_token"], options={"verify_signature": False})["cis_role"] == "admin"


def test_role_comes_from_the_db_not_the_session(client_with, secret):
    # Session still says admin; the row was demoted to content -> token carries content.
    client, fake = client_with(_staff_row("content"))
    session = _create_admin_jwt(ADMIN_ID, "linh.sales", "admin")
    res = client.post("/auth/booking-token", headers={"Authorization": f"Bearer {session}"})
    assert res.status_code == 200
    assert jwt.decode(res.json()["access_token"], options={"verify_signature": False})["cis_role"] == "content"
    assert fake.conn.args == (uuid.UUID(ADMIN_ID),)


def test_jwks_verifies_the_token(client_with, secret):
    client, _ = client_with(_staff_row("admin"))
    session = _create_admin_jwt(ADMIN_ID, "linh.sales", "admin")
    token = client.post("/auth/booking-token", headers={"Authorization": f"Bearer {session}"}).json()["access_token"]

    res = client.get("/auth/.well-known/jwks.json")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "public, max-age=600"
    doc = res.json()
    assert [k["kid"] for k in doc["keys"]] == ["bk-test-2", "bk-test-1"]
    for k in doc["keys"]:
        assert k["kty"] == "RSA" and k["use"] == "sig" and k["alg"] == "RS256"
        assert "d" not in k  # never leak the private exponent

    claims = jwt.decode(token, _jwks_key_for(token, doc), algorithms=["RS256"],
                        audience="booking", issuer=ISS)
    assert claims["sub"] == ADMIN_ID

    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(token, _jwks_key_for(token, doc), algorithms=["RS256"], audience="cis", issuer=ISS)


def test_previous_key_still_verifies_during_rotation(secret, keypair):
    old = jwt.encode(
        {"iss": ISS, "aud": "booking", "sub": ADMIN_ID, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        keypair["previous"], algorithm="RS256", headers={"kid": "bk-test-1"},
    )
    doc = booking_jwt.jwks()
    assert jwt.decode(old, _jwks_key_for(old, doc), algorithms=["RS256"], audience="booking")["sub"] == ADMIN_ID


def test_token_expires_after_15_minutes(secret):
    issued = datetime.now(timezone.utc) - timedelta(minutes=16)
    token, _ = booking_jwt.mint_booking_token(admin_id=ADMIN_ID, username="u", role="admin", now=issued)
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, _jwks_key_for(token, booking_jwt.jwks()), algorithms=["RS256"], audience="booking")


def test_no_session_is_401(client_with, secret):
    client, _ = client_with(_staff_row())
    assert client.post("/auth/booking-token").status_code == 401


def test_bad_session_is_401(client_with, secret):
    client, _ = client_with(_staff_row())
    res = client.post("/auth/booking-token", headers={"Authorization": "Bearer not-a-jwt"})
    assert res.status_code == 401


def test_tenant_session_is_401(client_with, secret):
    client, _ = client_with(_staff_row())
    now = datetime.now(timezone.utc)
    tenant = jwt.encode({"sub": "t1", "role": "tenant", "iat": now, "exp": now + timedelta(hours=1)},
                        JWT_SECRET, algorithm=JWT_ALG)
    res = client.post("/auth/booking-token", headers={"Authorization": f"Bearer {tenant}"})
    assert res.status_code == 401


def test_inactive_or_unknown_staff_is_401(client_with, secret):
    client, _ = client_with(None)  # the query filters is_active, so a deactivated user returns no row
    session = _create_admin_jwt(ADMIN_ID, "linh.sales", "admin")
    res = client.post("/auth/booking-token", headers={"Authorization": f"Bearer {session}"})
    assert res.status_code == 401


def test_non_uuid_sub_is_401_without_db_call(client_with, secret):
    client, fake = client_with(_staff_row())
    session = _create_admin_jwt("not-a-uuid", "x", "admin")
    res = client.post("/auth/booking-token", headers={"Authorization": f"Bearer {session}"})
    assert res.status_code == 401
    assert fake.conn.args is None


# ── missing / malformed secret → 503, never a crash ──────────────────────────

def test_missing_secret_is_503_on_both_endpoints(client_with, missing_secret):
    client, _ = client_with(_staff_row())
    session = _create_admin_jwt(ADMIN_ID, "linh.sales", "admin")
    res = client.post("/auth/booking-token", headers={"Authorization": f"Bearer {session}"})
    assert res.status_code == 503
    assert "signing key" in res.json()["detail"]
    assert client.get("/auth/.well-known/jwks.json").status_code == 503


def test_unauthenticated_beats_missing_secret(client_with, missing_secret):
    client, _ = client_with(_staff_row())
    assert client.post("/auth/booking-token").status_code == 401


@pytest.mark.parametrize("raw", [
    "not json",
    json.dumps({"kid": "k"}),
    json.dumps({"kid": "", "private_key_pem": "x"}),
    json.dumps({"kid": "k", "private_key_pem": "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----"}),
])
def test_malformed_secret_raises_unavailable(raw):
    with pytest.raises(booking_jwt.SigningKeyUnavailable):
        booking_jwt.parse_signing_secret(raw)


def test_secret_is_cached(secret):
    booking_jwt.jwks()
    booking_jwt.jwks()
    booking_jwt.mint_booking_token(admin_id=ADMIN_ID, username="u", role="admin")
    assert secret == ["aa-cis/dev/booking-jwt-signing-key"]


def test_secret_name_from_env(monkeypatch, secret):
    monkeypatch.setenv("BOOKING_JWT_SECRET_NAME", "aa-cis/test/other")
    booking_jwt.reset_key_cache()
    booking_jwt.jwks()
    assert secret[-1] == "aa-cis/test/other"


def test_failed_load_is_not_retried_every_request(monkeypatch):
    calls = []

    def fake_fetch(name):
        calls.append(name)
        raise RuntimeError("boom")

    monkeypatch.setattr(booking_jwt, "_fetch_secret_string", fake_fetch)
    booking_jwt.reset_key_cache()
    for _ in range(3):
        with pytest.raises(booking_jwt.SigningKeyUnavailable):
            booking_jwt.jwks()
    assert len(calls) == 1
    booking_jwt.reset_key_cache()
