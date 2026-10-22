"""AA-816 S2: the API legacy login flag is off, so /auth/verify-admin refuses HS256 staff sessions.

With LEGACY_ADMIN_LOGIN_ENABLED unset (the deployed api task definition does not set it):
- an HS256 staff session from /auth/admin-login is refused by /auth/verify-admin (401), so no BFF session exists;
- a WORKFORCE Cognito access token is still accepted;
- AA-Booking is unaffected: /auth/admin-login still answers a token and /auth/booking-token still mints from it
  (neither calls the flag; the bridge is retired later, AA-911 P6b);
- the BFF parses the flag with the same "on" values (frontend/lib/staffSession.ts), so its password form is off too.
No network, no AWS, no DB.
"""
from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

import bcrypt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import api.main as main_mod
from api.core import booking_jwt, identity_config, staff_identity
from api.main import app, get_pool
from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, install_issuer

REPO = Path(__file__).resolve().parents[2]
ADMIN_ID = "6f1c2c4e-8a51-4c1e-9d0b-2f3c4d5e6f71"
USERNAME = "aa816-s2-staff"
PASSWORD = "aa816-s2-test-password"


class _Conn:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, sql, *args):
        return self.row

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        return "UPDATE 1"


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def legacy_unset(monkeypatch):
    monkeypatch.delenv("LEGACY_ADMIN_LOGIN_ENABLED", raising=False)
    main_mod._staff_state_cache.clear()
    staff_identity.clear_cache()
    yield
    app.dependency_overrides.pop(get_pool, None)
    main_mod._staff_state_cache.clear()
    staff_identity.clear_cache()


@pytest.fixture
def password_row():
    return {"id": ADMIN_ID, "username": USERNAME, "role": "admin", "is_active": True,
            "password_hash": bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
            "email": None, "display_name": None}


@pytest.fixture
def booking_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    monkeypatch.setattr(booking_jwt, "_fetch_secret_string",
                        lambda name: json.dumps({"kid": "bk-s2-test", "private_key_pem": pem, "previous": []}))
    booking_jwt.reset_key_cache()
    yield
    booking_jwt.reset_key_cache()


def test_the_flag_is_off_when_unset(legacy_unset):
    assert staff_identity.legacy_login_enabled() is False


def test_hs256_session_from_admin_login_is_refused_by_verify_admin(legacy_unset, password_row):
    app.dependency_overrides[get_pool] = lambda: _Pool(password_row)
    client = TestClient(app)
    login = client.post("/auth/admin-login", json={"username": USERNAME, "password": PASSWORD})
    assert login.status_code == 200, login.text  # Booking still signs in through this route (not gated)
    res = client.post("/auth/verify-admin", json={"token": login.json()["token"]})
    assert res.status_code == 401
    assert res.json()["detail"] == "Password sessions are switched off; sign in again"


def test_cognito_access_token_is_still_accepted(legacy_unset):
    issuer, _ = install_issuer()
    try:
        directory = Directory()
        uid = directory.add(iss=ISS, sub=SUB)
        app.dependency_overrides[get_pool] = lambda: directory
        res = TestClient(app).post("/auth/verify-admin", json={"token": issuer.token()})
        assert res.status_code == 200, res.text
        assert (res.json()["admin_id"], res.json()["auth"]) == (uid, "cognito")
    finally:
        identity_config.reset_cis_staff_verifier()


def test_booking_bridge_still_mints_from_a_password_session(legacy_unset, password_row, booking_key):
    """AA-Booking's legacy sign-in (admin-login → booking-token) never calls verify-admin, so S2 leaves it working
    until the bridge is retired (AA-911 P6b)."""
    app.dependency_overrides[get_pool] = lambda: _Pool(password_row)
    client = TestClient(app)
    login = client.post("/auth/admin-login", json={"username": USERNAME, "password": PASSWORD})
    assert login.status_code == 200, login.text
    res = client.post("/auth/booking-token", headers={"Authorization": f"Bearer {login.json()['token']}"})
    assert res.status_code == 200, res.text
    assert res.json()["token_type"] == "Bearer" and res.json()["access_token"]


def test_bff_parses_the_flag_with_the_same_on_values():
    """The BFF password form and /api/auth/login follow the same rule: off unless explicitly on."""
    ts = (REPO / "frontend" / "lib" / "staffSession.ts").read_text()
    body = ts[ts.index("export function legacyAdminLoginEnabled"):]
    body = body[:body.index("\n}\n")]
    values = re.search(r"\[([^\]]*)\]\.includes\(raw\)", body)
    assert values, body
    assert "!" not in body.split("return", 1)[1].split("[", 1)[0]  # an allow-list, not a deny-list
    bff = {v.strip().strip('"\'') for v in values.group(1).split(",")}
    assert bff == set(staff_identity._LEGACY_ON_VALUES)
