"""AA-1155 spec task T11 (R5.5) — the AI budget and AI gateway routes need a verified Cognito session.

Every route under /admin/budgets and /admin/ai-gateway runs api/core/cognito_session.require_cognito_session: with the
admin secret and a forged x-admin-role (any role, admin included) and no verified session the answer is 403 before
the endpoint or the database is reached. The before / after audit of budget changes is tested in
tests/unit/test_aa1025_admin_central_budgets.py. No database, no network.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.core import cognito_session
from api.core.cognito_session import AI_CONTROLS_NEED_COGNITO, require_cognito_session
from api.routers import admin
from tests.unit.test_s241_admin_routes_require_auth import _dependency_calls, _inherited_calls, _served_routes, _walk

SECRET = "t11-secret"
PREFIXES = ("/admin/budgets", "/admin/ai-gateway")


def _ai_routes():
    from api.main import app

    for path, route, inherited in _walk(_served_routes(app)):
        if isinstance(route, APIRoute) and any(path == p or path.startswith(p + "/") for p in PREFIXES):
            yield path, route, inherited


def test_every_budget_and_ai_gateway_route_needs_a_cognito_session():
    routes = list(_ai_routes())
    assert len(routes) > 20     # the six routers are mounted
    missing = [(sorted(route.methods), path) for path, route, inherited in routes
               if require_cognito_session not in (set(_dependency_calls(route.dependant))
                                                  | set(_inherited_calls(inherited, path)))]
    assert missing == []


@pytest.fixture
def client(monkeypatch):
    from api.main import app

    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=AssertionError("the DB must not be reached"))
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        setattr(pool, name, AsyncMock(side_effect=AssertionError("the DB must not be reached")))
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("method,path,role", [
    ("GET", "/admin/budgets", "admin"),
    ("PUT", "/admin/budgets/dfs/global", "admin"),
    ("DELETE", "/admin/budgets/llm/eval", "admin"),
    ("GET", "/admin/ai-gateway/providers", "admin"),
    ("GET", "/admin/ai-gateway/budgets", "finance"),
    ("PUT", "/admin/ai-gateway/connection-budgets/zz-conn", "admin"),
    ("POST", "/admin/ai-gateway/anomaly-breaker/cis/reset", "ai_ops"),
    ("GET", "/admin/ai-gateway/route-control/versions", "admin"),
])
def test_a_forged_role_header_without_a_session_is_refused(client, method, path, role):
    headers = {"X-Admin-Secret": SECRET, "x-admin-role": role,
               "x-admin-user-id": "00000000-0000-4000-8000-000000000001"}
    res = client.request(method, path, headers=headers, json={"per_day_usd": 1.0})
    assert res.status_code in (401, 403), res.text
    if res.status_code == 403 and path != "/admin/ai-gateway/route-control/versions":
        assert res.json()["detail"] == AI_CONTROLS_NEED_COGNITO


def test_the_machine_secret_without_a_role_is_refused_too(client):
    res = client.get("/admin/ai-gateway/providers", headers={"X-Admin-Secret": SECRET})
    assert res.status_code in (401, 403)


def _request(identity):
    state = {"staff_identity": identity} if identity is not None else {}
    return SimpleNamespace(scope={"state": state}, url=SimpleNamespace(path="/admin/budgets"), method="GET",
                           headers={})


def test_the_dependency_returns_a_verified_cognito_identity():
    ident = SimpleNamespace(auth="cognito", admin_id="00000000-0000-4000-8000-000000000002")
    assert require_cognito_session(_request(ident)) is ident


@pytest.mark.parametrize("ident", [None, SimpleNamespace(auth="legacy", admin_id="x"),
                                   SimpleNamespace(auth="cognito", admin_id=None)])
def test_the_dependency_refuses_anything_else(monkeypatch, ident):
    from api.core import staff_identity

    token = staff_identity.current_staff.set(None)
    try:
        with pytest.raises(HTTPException) as e:
            require_cognito_session(_request(ident))
    finally:
        staff_identity.current_staff.reset(token)
    assert e.value.status_code == 403 and e.value.detail == cognito_session.AI_CONTROLS_NEED_COGNITO
