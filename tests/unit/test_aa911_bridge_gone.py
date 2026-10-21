"""AA-911 (root ADR 0006a A11): the CIS -> AA-Booking bridge is retired.

Done-when line 2 (code side): POST /auth/booking-token and GET /auth/.well-known/jwks.json are no longer
routes. Through the full app (every middleware), they answer 404 / 405, never 401 (a 401 would mean a route
or a middleware still treats them as an authenticated endpoint) and never 200 / 503.
"""
from __future__ import annotations

import importlib.util

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers.auth import _create_admin_jwt

ADMIN_ID = "6f1c2c4e-8a51-4c1e-9d0b-2f3c4d5e6f71"
GONE = {404, 405}


@pytest.fixture
def client():
    return TestClient(app)


def test_booking_token_route_is_gone(client):
    assert client.post("/auth/booking-token").status_code in GONE


def test_booking_token_with_a_staff_session_is_still_gone(client):
    session = _create_admin_jwt(ADMIN_ID, "admin", "admin")
    res = client.post("/auth/booking-token", headers={"Authorization": f"Bearer {session}"})
    assert res.status_code in GONE, res.text
    res = client.post("/auth/booking-token", cookies={"cis_admin_token": session})
    assert res.status_code in GONE, res.text


def test_booking_jwks_route_is_gone(client):
    assert client.get("/auth/.well-known/jwks.json").status_code in GONE


def test_no_route_or_module_left():
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/auth/booking-token" not in paths
    assert "/auth/.well-known/jwks.json" not in paths
    assert importlib.util.find_spec("api.core.booking_jwt") is None
