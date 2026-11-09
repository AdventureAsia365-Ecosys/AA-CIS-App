"""AA-911 (S271, root ADR 0019 d23): the CIS -> AA-Booking sign-in bridge is retired.

Booking staff sign in with the Cognito WORKFORCE booking-staff client on the Booking admin's own host, and Booking
accepts only the WORKFORCE issuer. CIS therefore mints no Booking token and publishes no Booking signing key: both
routes answer 404, the key module is gone, and no code reads the signing-key secret any more.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app

REPO = Path(__file__).resolve().parents[2]


def test_booking_token_and_jwks_routes_are_gone():
    client = TestClient(app)
    assert client.post("/auth/booking-token", headers={"Authorization": "Bearer x"}).status_code == 404
    assert client.get("/auth/.well-known/jwks.json").status_code == 404
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/auth/booking-token" not in paths and "/auth/.well-known/jwks.json" not in paths


def test_no_code_mints_or_signs_a_booking_token():
    assert not (REPO / "api" / "core" / "booking_jwt.py").exists()
    for folder in ("api", "services", "shared", "worker"):
        for path in (REPO / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "booking_jwt" not in text, f"{path.relative_to(REPO)} still names booking_jwt"
            assert "booking-jwt-signing-key" not in text, f"{path.relative_to(REPO)} still reads the bridge key"
