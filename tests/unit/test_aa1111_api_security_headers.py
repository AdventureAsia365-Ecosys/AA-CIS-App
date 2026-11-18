"""AA-1111 (S284, ZAP baseline) — nosniff + HSTS on every CIS API response, no Server header from uvicorn."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from api.middleware.request_id import RequestIdMiddleware, unhandled_error_handler
from api.middleware.security_headers import SECURITY_HEADERS, SecurityHeadersMiddleware

REPO = Path(__file__).resolve().parents[2]
HSTS = "max-age=63072000; includeSubDomains"


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    async def _probe() -> dict:
        return {"ok": True}

    @app.get("/svg")
    async def _svg() -> Response:
        return Response("<svg/>", media_type="image/svg+xml", headers={"X-Content-Type-Options": "custom"})

    @app.get("/boom")
    async def _boom() -> dict:
        raise RuntimeError("boom")

    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


def test_values():
    assert SECURITY_HEADERS == {"X-Content-Type-Options": "nosniff", "Strict-Transport-Security": HSTS}
    assert "preload" not in SECURITY_HEADERS["Strict-Transport-Security"]


def test_ok_404_and_405_carry_the_headers():
    client = TestClient(_probe_app())
    for method, path, status in (("GET", "/probe", 200), ("GET", "/no-such-path", 404), ("POST", "/probe", 405)):
        r = client.request(method, path)
        assert r.status_code == status
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["strict-transport-security"] == HSTS, path


def test_a_header_the_endpoint_set_wins():
    r = TestClient(_probe_app()).get("/svg")
    assert r.headers["x-content-type-options"] == "custom"
    assert r.headers["strict-transport-security"] == HSTS


def test_unhandled_500_carries_the_headers():
    r = TestClient(_probe_app(), raise_server_exceptions=False).get("/boom")
    assert r.status_code == 500
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["strict-transport-security"] == HSTS
    assert len(r.headers["x-request-id"]) == 32


def test_real_app_registers_it_just_inside_the_request_id():
    from api.main import app
    classes = [m.cls for m in app.user_middleware]
    assert classes[0] is RequestIdMiddleware
    assert classes[1] is SecurityHeadersMiddleware


def test_refused_call_on_the_real_app_carries_the_headers():
    from api.main import app
    r = TestClient(app).get("/admin/tenants")
    assert r.status_code in (401, 403, 503)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["strict-transport-security"] == HSTS


def test_image_starts_uvicorn_without_the_server_header():
    cmd = next(line for line in (REPO / "Dockerfile").read_text().splitlines() if line.startswith("CMD"))
    assert '"uvicorn"' in cmd and '"--no-server-header"' in cmd
