"""AA-941 (card CRM-13a, screen S-01) — the CIS relay to the partner CRM's L1 console (crm.ops.v1), with fakes only.

- the relay forwards the caller's OWN Cognito bearer and nothing else; a legacy HS256 session gets 401
  ``cognito_session_required`` without any call;
- only the L1 routes of crm.ops.v1 can be reached (a path outside ``ALLOWED`` is refused before any call);
- the CRM's status code and ``{detail, message}`` body pass through (``has_travellers`` reaches the page); a network
  error is 503 ``crm_unavailable``, a non-JSON or non-object body 502 ``crm_bad_response``;
- request and response bodies never reach a log line (the owner invite carries an e-mail address);
- every /admin/platform/crm route has its POLICY entry (GET tenant.read, writes tenant.write); the viewer is refused
  on writes and a content role on reads, before any call;
- with the real StaffBearerMiddleware, a verified WORKFORCE token reaches the CRM unchanged (the middleware drops the
  Authorization header and keeps the token in the scope state; before AA-941 the AA-936 relay saw no token).
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from api.core import identity_config, staff_identity
from api.core.staff_authz import POLICY, TENANT_READ, TENANT_WRITE
from api.main import app
from api.routers import admin, admin_crm_console, admin_settings
from api.services import crm_console

SECRET = "aa941-test-admin-secret"
TID = "0b6c3f1e-7a7d-4f7e-9a59-1c2d3e4f5a6b"
RID = "1c6c3f1e-7a7d-4f7e-9a59-1c2d3e4f5a6b"
EMAIL = "first.owner@agency.example"


def _jwt(claims: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"


# An access token from an issuer StaffBearerMiddleware does not verify: it passes through untouched and the route's
# relay sends it on (the CRM verifies it). The real WORKFORCE path is covered at the end of this file.
OTHER_ISS = _jwt({"iss": "https://issuer.example/other", "token_use": "access"})
LEGACY = _jwt({"sub": "admin", "role": "admin"})


class Recorder:
    """An httpx.MockTransport that records each call and answers with a scripted response."""

    def __init__(self, status: int = 200, body=None, raw: bytes | None = None, exc: Exception | None = None):
        self.calls: list[httpx.Request] = []
        self.status, self.body, self.raw, self.exc = status, body if body is not None else {"ok": True}, raw, exc

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            if self.exc is not None:
                raise self.exc
            if self.raw is not None:
                return httpx.Response(self.status, content=self.raw, headers={"content-type": "text/html"})
            return httpx.Response(self.status, json=self.body)
        return httpx.MockTransport(handler)


@pytest.fixture
def crm(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(crm_console, "_transport", rec.transport())
    return rec


def _script(monkeypatch, rec: Recorder) -> Recorder:
    monkeypatch.setattr(crm_console, "_transport", rec.transport())
    return rec


# ── service ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_relay_forwards_the_callers_own_bearer_and_the_body(crm):
    status, body = await crm_console.crm_ops_call(
        "POST", f"/tenants/{TID}/lock", f"Bearer {OTHER_ISS}", json={"ticket_ref": "AA-941", "reason_code": "test"})
    assert status == 200 and body == {"ok": True}
    (req,) = crm.calls
    assert req.headers["authorization"] == f"Bearer {OTHER_ISS}"
    assert str(req.url).endswith(f"/crm/ops/v1/tenants/{TID}/lock")
    assert json.loads(req.content) == {"ticket_ref": "AA-941", "reason_code": "test"}
    assert "x-admin-secret" not in req.headers


@pytest.mark.asyncio
async def test_legacy_session_is_refused_without_a_call(crm):
    for auth in (f"Bearer {LEGACY}", None, "Basic abc"):
        status, body = await crm_console.crm_ops_call("GET", "/console", auth)
        assert status == 401 and body["detail"] == "cognito_session_required"
    assert crm.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/jobs/summary"),                       # an ops read this console does not use
    ("GET", "/llm/usage"),
    ("DELETE", f"/tenants/{TID}"),
    ("GET", f"/tenants/{TID}/../../crm/v1/me"),
    ("GET", "/tenants/not-a-uuid"),
    ("POST", f"/tenants/{TID}/members/{RID}/role"),
    ("GET", f"/tenants/{TID}/usage?x=1"),
    ("PUT", f"/tenants/{TID}/lock"),
])
async def test_paths_outside_the_allow_list_never_leave_cis(crm, method, path):
    status, body = await crm_console.crm_ops_call(method, path, f"Bearer {OTHER_ISS}")
    assert status == 404 and body["detail"] == "crm_path_not_allowed"
    assert crm.calls == []


@pytest.mark.asyncio
async def test_crm_4xx_status_and_detail_pass_through(monkeypatch):
    _script(monkeypatch, Recorder(409, {"detail": "has_travellers", "message": "the agency holds travellers"}))
    status, body = await crm_console.crm_ops_call("POST", f"/tenants/{TID}/owner-invite", f"Bearer {OTHER_ISS}",
                                                  json={"owner_email": EMAIL, "ticket_ref": "AA-941"})
    assert status == 409 and body == {"detail": "has_travellers", "message": "the agency holds travellers"}


@pytest.mark.asyncio
async def test_network_error_is_503(monkeypatch):
    _script(monkeypatch, Recorder(exc=httpx.ConnectTimeout("slow")))
    status, body = await crm_console.crm_ops_call("GET", "/console", f"Bearer {OTHER_ISS}")
    assert status == 503 and body["detail"] == "crm_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("rec", [Recorder(200, raw=b"<html>proxy</html>"), Recorder(200, body=[1, 2]),
                                 Recorder(502, raw=b"Bad gateway")])
async def test_non_object_body_is_502(monkeypatch, rec):
    _script(monkeypatch, rec)
    status, body = await crm_console.crm_ops_call("GET", "/tenants", f"Bearer {OTHER_ISS}")
    assert status == 502 and body["detail"] == "crm_bad_response"


@pytest.mark.asyncio
async def test_bodies_are_never_logged(monkeypatch):
    _script(monkeypatch, Recorder(202, {"tenant_id": TID, "invitation_id": RID, "status": "pending",
                                        "echo": EMAIL}))
    with capture_logs() as logs:
        await crm_console.crm_ops_call("POST", f"/tenants/{TID}/owner-invite", f"Bearer {OTHER_ISS}",
                                       json={"owner_email": EMAIL, "ticket_ref": "AA-941"})
        _script(monkeypatch, Recorder(200, raw=EMAIL.encode()))
        await crm_console.crm_ops_call("POST", f"/tenants/{TID}/owner-invite", f"Bearer {OTHER_ISS}",
                                       json={"owner_email": EMAIL, "ticket_ref": "AA-941"})
    text = json.dumps(logs, default=str)
    assert logs and EMAIL not in text and OTHER_ISS not in text and TID not in text
    assert {e["event"] for e in logs} == {"crm_console_call", "crm_console_bad_response"}


@pytest.mark.asyncio
async def test_timeouts_reads_list_writes(monkeypatch):
    seen: list[float] = []
    real = httpx.AsyncClient

    def spy(*args, **kwargs):
        seen.append(kwargs["timeout"])
        return real(*args, **kwargs)

    monkeypatch.setattr(crm_console.httpx, "AsyncClient", spy)
    _script(monkeypatch, Recorder())
    await crm_console.crm_ops_call("GET", f"/tenants/{TID}/usage", f"Bearer {OTHER_ISS}")
    await crm_console.crm_ops_call("GET", "/tenants", f"Bearer {OTHER_ISS}")
    await crm_console.crm_ops_call("POST", f"/tenants/{TID}/owner-invite", f"Bearer {OTHER_ISS}", json={})
    assert seen == [crm_console.READ_TIMEOUT_S, crm_console.LIST_TIMEOUT_S, crm_console.WRITE_TIMEOUT_S]


# ── routes + policy ─────────────────────────────────────────────────────────────────────────────

def _console_routes() -> list[APIRoute]:
    return [r for r in admin_crm_console.router.routes if isinstance(r, APIRoute)]


def test_every_console_route_has_its_policy():
    routes = _console_routes()
    assert len(routes) == 19
    for r in routes:
        for m in r.methods:
            want = TENANT_READ if m == "GET" else TENANT_WRITE
            assert POLICY.get((m, r.path)) == want, (m, r.path)
    policy_paths = {(m, p) for (m, p) in POLICY if p.startswith("/admin/platform/crm/")}
    assert policy_paths == {(m, r.path) for r in routes for m in r.methods}


def _concrete(path: str) -> str:
    return re.sub(r"\{[a-z_]+\}", lambda _: str(uuid.uuid4()), path)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=AssertionError("the DB must not be reached"))
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        setattr(pool, name, AsyncMock(return_value=None))
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app)


def _h(role="admin", token=OTHER_ISS):
    h = {"X-Admin-Secret": SECRET, "x-admin-role": role, "x-admin-user-id": "aa941-test"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def test_every_route_reaches_an_allowed_crm_path(client, crm):
    """Each CIS route maps to a CRM path inside ALLOWED (else the relay would answer 404 crm_path_not_allowed)."""
    for r in _console_routes():
        (method,) = r.methods
        res = client.request(method, _concrete(r.path), headers=_h(), json={"ticket_ref": "AA-941"})
        assert res.status_code == 200, (method, r.path, res.text)
    assert len(crm.calls) == 19
    assert all(c.headers["authorization"] == f"Bearer {OTHER_ISS}" for c in crm.calls)
    assert all(str(c.url.path).startswith("/crm/ops/v1/") for c in crm.calls)


def test_status_and_body_pass_through_the_route(client, monkeypatch):
    _script(monkeypatch, Recorder(202, {"tenant_id": TID, "invitation_id": RID, "status": "pending",
                                        "expires_at": "2026-10-31T00:00:00Z"}))
    res = client.post(f"/admin/platform/crm/agencies/{TID}/owner-invite", headers=_h(),
                      json={"owner_email": EMAIL, "ticket_ref": "AA-941"})
    assert res.status_code == 202 and res.json()["status"] == "pending"
    _script(monkeypatch, Recorder(403, {"detail": "missing_group", "message": "crm-platform-admin"}))
    res = client.post(f"/admin/platform/crm/agencies/{TID}/lock", headers=_h(),
                      json={"ticket_ref": "AA-941", "reason_code": "test"})
    assert res.status_code == 403 and res.json()["detail"] == "missing_group"


def test_list_passes_cursor_and_limit(client, crm):
    res = client.get("/admin/platform/crm/agencies?limit=20&cursor=abc", headers=_h())
    assert res.status_code == 200
    assert dict(crm.calls[0].url.params) == {"cursor": "abc", "limit": "20"}
    assert client.get("/admin/platform/crm/agencies?limit=500", headers=_h()).status_code == 422


def test_bad_ids_are_422_without_a_call(client, crm):
    assert client.get("/admin/platform/crm/agencies/not-a-uuid/usage", headers=_h()).status_code == 422
    assert crm.calls == []


def test_console_adds_the_cis_environment(client, monkeypatch):
    _script(monkeypatch, Recorder(200, {"can_write": False}))
    monkeypatch.setenv("ENVIRONMENT", "dev")
    res = client.get("/admin/platform/crm/console", headers=_h())
    assert res.json() == {"can_write": False, "environment": "dev"}


def test_legacy_session_route_is_401(client, crm):
    res = client.get("/admin/platform/crm/agencies", headers=_h(token=None))
    assert res.status_code == 401 and res.json()["detail"] == "cognito_session_required"
    assert crm.calls == []


def test_viewer_refused_on_writes_content_on_reads(client, crm):
    res = client.post(f"/admin/platform/crm/agencies/{TID}/unlock", headers=_h("viewer"), json={"ticket_ref": "A-1"})
    assert res.status_code == 403
    res = client.get("/admin/platform/crm/agencies", headers=_h("content"))
    assert res.status_code == 403 and "tenant.read" in res.json()["detail"]
    res = client.put(f"/admin/platform/crm/agencies/{TID}/insights-opt-out", headers=_h("reviewer"),
                     json={"opt_out": True, "owner_request_ref": "AA-1"})
    assert res.status_code == 403
    assert crm.calls == []


# ── the real StaffBearerMiddleware: a verified WORKFORCE token reaches the CRM unchanged ───────────

@pytest.fixture
def workforce(monkeypatch):
    from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, install_issuer

    issuer, _ = install_issuer()
    directory = Directory()
    directory.add(role="admin", iss=ISS, sub=SUB)
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(app.state, "pool", directory, raising=False)
    yield issuer
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()


def test_verified_workforce_token_is_relayed(workforce, crm):
    token = workforce.token()
    res = TestClient(app).get("/admin/platform/crm/agencies", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    assert crm.calls[0].headers["authorization"] == f"Bearer {token}"


def test_verified_workforce_token_reaches_the_aa936_jobs_relay(workforce, monkeypatch):
    """The AA-936 jobs card used request.headers["authorization"], which the middleware drops after verifying."""
    seen = {}

    async def fake_jobs(authorization, client=None):
        seen["auth"] = authorization
        return {"status": "ok", "kinds": []}

    from api.services import platform_apps
    monkeypatch.setattr(platform_apps, "crm_jobs", fake_jobs)
    token = workforce.token()
    res = TestClient(app).get("/admin/platform/apps/crm/jobs", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    assert seen["auth"] == f"Bearer {token}"
