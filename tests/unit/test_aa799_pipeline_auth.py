"""AA-799 guard — every /v1/pipeline route is admin-only.

Before AA-799, POST /v1/pipeline/run had no auth at all, and review-queue / approve / reject /
execution / sources used _get_tenant, which accepted ANY valid JWT — so a tenant token could read
the AA master review queue and approve/reject masters for export.

The guard iterates the router's routes (so a route added later is covered automatically) and
checks, through a real FastAPI app:
  - no credentials             → 401
  - a valid tenant JWT         → 403
  - a valid staff JWT (admin)  → 403 (a JWT alone never authorizes these routes)
  - a wrong admin secret       → 403
and that the admin secret is accepted (at dependency level for every route, plus two end-to-end
calls whose handlers do no real work).
"""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.routers import v1_pipeline
from api.routers.auth import _create_admin_jwt, _create_jwt

ADMIN_SECRET = "aa799-test-admin-secret"
MASTER = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)


def _pool_with(conn):
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _client(conn=None):
    app = FastAPI()
    app.include_router(v1_pipeline.router)
    app.state.pool = _pool_with(conn or AsyncMock())
    return TestClient(app, raise_server_exceptions=False)


def _routes():
    out = []
    for r in v1_pipeline.router.routes:
        if not isinstance(r, APIRoute):
            continue
        path = re.sub(r"\{[^}]+\}", "x-aa799", r.path)
        for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return out


ROUTES = _routes()


def test_router_has_the_known_routes():
    paths = {p for _, p in ROUTES}
    for expected in ("/v1/pipeline/review-queue",
                     "/v1/pipeline/review-queue/x-aa799/approve", "/v1/pipeline/review-queue/x-aa799/reject",
                     "/v1/pipeline/execution/x-aa799", "/v1/pipeline/sources"):
        assert expected in paths


@pytest.mark.parametrize("method,path", ROUTES)
def test_no_auth_is_401(method, path):
    assert _client().request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_tenant_jwt_is_403(method, path):
    token = _create_jwt("11111111-1111-1111-1111-111111111111", "Tenant Co", "starter")
    res = _client().request(method, path, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403


@pytest.mark.parametrize("method,path", ROUTES)
def test_staff_jwt_without_secret_is_403(method, path):
    token = _create_admin_jwt("22222222-2222-2222-2222-222222222222", "someone", "admin")
    res = _client().request(method, path, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403


@pytest.mark.parametrize("method,path", ROUTES)
def test_bad_jwt_is_401(method, path):
    res = _client().request(method, path, headers={"Authorization": "Bearer not-a-jwt"})
    assert res.status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_wrong_admin_secret_is_403(method, path):
    res = _client().request(method, path, headers={"X-Admin-Secret": "wrong"})
    assert res.status_code == 403


def test_every_route_depends_on_require_admin():
    for r in v1_pipeline.router.routes:
        if isinstance(r, APIRoute):
            calls = [d.call for d in r.dependant.dependencies]
            assert v1_pipeline._require_admin in calls, r.path


def test_admin_secret_accepted_at_dependency_level():
    req = SimpleNamespace(headers={"x-admin-secret": ADMIN_SECRET})
    assert v1_pipeline._require_admin(req, None) == {"sub": MASTER, "role": "admin"}


def test_admin_secret_unconfigured_is_503(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", "")
    req = SimpleNamespace(headers={"x-admin-secret": "anything"})
    with pytest.raises(HTTPException) as e:
        v1_pipeline._require_admin(req, None)
    assert e.value.status_code == 503


def test_admin_secret_end_to_end_review_queue():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)
    res = _client(conn).get("/v1/pipeline/review-queue", headers={"X-Admin-Secret": ADMIN_SECRET})
    assert res.status_code == 200
    assert res.json()["pagination"]["total"] == 0


def test_run_endpoint_is_gone():
    # AA-765 (AA-799 follow-up): the caller-less Excel upload + rewrite endpoint was removed.
    assert "/v1/pipeline/run" not in {p for _, p in ROUTES}
    res = _client().post("/v1/pipeline/run", headers={"X-Admin-Secret": ADMIN_SECRET, "x-admin-role": "admin"},
                         files={"file": ("x.xlsx", b"nope", "application/octet-stream")})
    assert res.status_code in (404, 405)


def test_sources_admin_reads_master_tenant():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    res = _client(conn).get("/v1/pipeline/sources", headers={"X-Admin-Secret": ADMIN_SECRET})
    assert res.status_code == 200
    assert conn.fetch.await_args.args[1] == MASTER
