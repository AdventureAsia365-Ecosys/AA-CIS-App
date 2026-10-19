"""AA-884 — StaffAuditMiddleware: one acp_shared.audit_log row per non-GET staff call, never more, never in the way.

A small app with the middleware and a few routes; write_audit_log is replaced by a recorder (the SQL itself runs
against Postgres in tests/integration/test_aa884_audit_rows.py).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.middleware import staff_audit

SECRET = "aa884-test-secret"


@pytest.fixture
def rows(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)
    recorded = []

    async def fake_write(conn, **kw):
        recorded.append(kw)

    monkeypatch.setattr("services.acp_shared.audit_log.write_audit_log", fake_write)
    return recorded


def _app():
    app = FastAPI()
    app.state.pool = object()  # never touched by the fake writer

    @app.post("/admin/things/{thing_id}")
    async def post_thing(thing_id: str):
        return {"id": thing_id}

    @app.get("/admin/things")
    async def get_things():
        return []

    @app.patch("/admin/denied")
    async def denied():
        raise HTTPException(status_code=403, detail="Role 'reviewer' lacks the 'x' permission")

    @app.post("/admin/boom")
    async def boom():
        raise RuntimeError("boom")

    @app.post("/v1/tours/rewrite")
    async def tenant_route():
        return {"ok": True}

    app.add_middleware(staff_audit.StaffAuditMiddleware)
    return app


def _h(role="admin", user="11111111-1111-1111-1111-111111111111", secret=SECRET):
    h = {}
    if secret:
        h["x-admin-secret"] = secret
    if role:
        h["x-admin-role"] = role
    if user:
        h["x-admin-user-id"] = user
    return h


def test_staff_post_writes_one_row_with_method_route_status(rows):
    resp = TestClient(_app()).post("/admin/things/42?dry_run=1&b=2", headers=_h())
    assert resp.status_code == 200 and resp.json() == {"id": "42"}
    assert len(rows) == 1
    r = rows[0]
    assert r["http_method"] == "POST" and r["route"] == "/admin/things/{thing_id}" and r["status"] == 200
    assert r["resource_type"] == "http_route" and r["resource_id"] == "/admin/things/42"
    assert r["actor"] == "admin:11111111-1111-1111-1111-111111111111" and r["actor_type"] == "staff"
    assert r["action"] == "staff.request" and r["tenant_id"] is None
    assert r["details"] == {"auth": "legacy_unverified", "role": "admin", "query_keys": ["b", "dry_run"]}


def test_get_is_never_logged(rows):
    assert TestClient(_app()).get("/admin/things", headers=_h()).status_code == 200
    assert rows == []


def test_without_the_secret_nothing_is_logged(rows):
    client = TestClient(_app())
    client.post("/admin/things/1", headers={"Authorization": "Bearer tenant.jwt.here"})
    client.post("/admin/things/1", headers=_h(secret="wrong"))
    client.post("/v1/tours/rewrite", headers={"Authorization": "Bearer tenant.jwt.here"})
    assert rows == []


def test_non_staff_path_is_not_logged_even_with_the_secret(rows):
    TestClient(_app()).post("/v1/tours/rewrite", headers=_h())
    assert rows == []


def test_denied_call_is_logged_with_its_status(rows):
    resp = TestClient(_app()).patch("/admin/denied", headers=_h(role="reviewer"))
    assert resp.status_code == 403
    assert rows[0]["status"] == 403 and rows[0]["route"] == "/admin/denied"
    assert rows[0]["details"]["role"] == "reviewer"


def test_machine_call(rows):
    TestClient(_app()).post("/admin/things/1", headers=_h(role=None, user=None))
    assert rows[0]["actor"] == "machine" and rows[0]["actor_type"] == "machine"
    assert rows[0]["action"] == "machine.request" and rows[0]["details"]["auth"] == "secret"


def test_role_without_user_id(rows):
    TestClient(_app()).post("/admin/things/1", headers=_h(user=None))
    assert rows[0]["actor"] == "staff:unknown" and rows[0]["actor_type"] == "staff"


def test_cognito_identity_wins_over_headers(rows):
    identity = SimpleNamespace(auth="cognito", admin_id="22222222-2222-2222-2222-222222222222")
    scope = {"type": "http", "method": "POST", "path": "/admin/things/1", "query_string": b"",
             "headers": [(b"x-admin-secret", SECRET.encode()), (b"x-admin-role", b"admin"),
                         (b"x-admin-user-id", b"forged")],
             "state": {"staff_identity": identity}}
    row = staff_audit.build_row(scope, 201)
    assert row["actor"] == "admin:22222222-2222-2222-2222-222222222222"
    assert row["details"]["auth"] == "cognito" and row["status"] == 201


def test_unhandled_error_is_logged_as_500(rows):
    resp = TestClient(_app(), raise_server_exceptions=False).post("/admin/boom", headers=_h())
    assert resp.status_code == 500
    assert rows[0]["status"] == 500 and rows[0]["route"] == "/admin/boom"


def test_write_failure_never_changes_the_response(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)
    monkeypatch.setattr("services.acp_shared.audit_log.write_audit_log",
                        AsyncMock(side_effect=OSError("db down")))
    resp = TestClient(_app()).post("/admin/things/7", headers=_h())
    assert resp.status_code == 200 and resp.json() == {"id": "7"}


def test_long_path_and_actor_are_truncated(rows):
    TestClient(_app()).post("/admin/things/" + "x" * 900, headers=_h(user="u" * 200))
    assert len(rows[0]["resource_id"]) == staff_audit.MAX_RESOURCE_ID
    assert len(rows[0]["actor"]) == staff_audit.MAX_ACTOR


def test_registered_inside_staff_bearer_in_the_real_app():
    """StaffAudit must sit INSIDE StaffBearer (it reads the identity StaffBearer puts on the scope it passes down)."""
    from api.main import app
    from api.middleware.staff_bearer import StaffBearerMiddleware

    order = [m.cls for m in app.user_middleware]  # outermost first
    assert order.index(StaffBearerMiddleware) < order.index(staff_audit.StaffAuditMiddleware)
