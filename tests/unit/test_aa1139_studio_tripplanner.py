"""AA-1139 fix pass — the Trip planner module in the Content studio (``/v1/account/tripplanner``, ``.../lead-route``,
``.../appearance``; ``api/routers/v1_account_tripplanner.py``), for an agency WITHOUT the CRM add-on: it has no CRM
agency, so its owner sets the e-mail / webhook route and the look here with the studio's own tenant token.

The router runs behind the tenant role middleware only (as in ``test_aa1036_addon_requests.py``); the service calls
are faked (the database paths: ``tests/integration/test_aa1139_tripplanner_module_pg.py``). Invented names only.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core import view_as
from api.middleware import tenant_role
from api.routers import v1_account_tripplanner as studio
from api.routers.auth import _create_partner_session_jwt
from services.tenant_account import tripplanner_site as tp

TID = "1139ad00-0000-4000-8000-0000000000c1"
MID = "1139ad00-0000-4000-8000-0000000000c2"
MODULE = {
    "active": True, "in_service": True,
    "planner": {"url": "https://planner.example.invalid/quillbrook-tours/plan", "domains": [], "launcher": False},
    "domains": [],
    "crm_core": False,
    "lead_route": {"lead_channel": "webhook", "lead_webhook_url": "https://hooks.quillbrook.example.com/leads",
                   "lead_email": None, "effective_channel": "webhook", "updated_at": "2026-11-18T09:00:00+00:00"},
    "theme": {}, "locale": "en", "ai_sessions_monthly": None,
}


def _tenant(role: str = "owner") -> dict[str, str]:
    token, _ = _create_partner_session_jwt(TID, "Quillbrook Tours", "growth", membership_id=MID,
                                           content_studio_role=role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    app = FastAPI()
    app.middleware("http")(tenant_role.tenant_role_middleware)
    app.include_router(studio.router)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.pool = pool
    return TestClient(app)


@pytest.fixture
def service(monkeypatch):
    seen: dict[str, Any] = {"view": [], "route": [], "appearance": []}

    async def module_view(conn, tenant_id, **_kw):
        seen["view"].append(tenant_id)
        return dict(MODULE)

    async def set_route(conn, tenant_id, **kw):
        seen["route"].append((tenant_id, kw))
        return {}

    async def set_appearance(conn, tenant_id, **kw):
        seen["appearance"].append((tenant_id, kw))
        return {}

    monkeypatch.setattr(tp, "module_view", module_view)
    monkeypatch.setattr(tp, "set_agency_lead_route", set_route)
    monkeypatch.setattr(tp, "set_appearance", set_appearance)
    return seen


@pytest.mark.parametrize("role", ["owner", "editor", "viewer"])
def test_every_studio_role_reads_the_module_of_its_own_tenant(client, service, role):
    resp = client.get("/v1/account/tripplanner", headers=_tenant(role))
    assert resp.status_code == 200 and resp.json() == MODULE
    assert service["view"] == [TID]  # the tenant of the token, never a parameter


def test_the_owner_sets_the_route_as_its_membership(client, service):
    body = {"lead_channel": "email", "lead_email": "desk@quillbrook.example.com"}
    resp = client.put("/v1/account/tripplanner/lead-route", json=body, headers=_tenant())
    assert resp.status_code == 200 and resp.json()["lead_route"]["effective_channel"] == "webhook"
    tenant_id, kw = service["route"][0]
    assert tenant_id == TID
    assert kw == {"channel": "email", "webhook_url": None, "email": "desk@quillbrook.example.com",
                  "actor": f"membership:{MID}"}


def test_the_owner_sets_the_look_as_its_membership(client, service):
    resp = client.put("/v1/account/tripplanner/appearance", json={"theme": {"accent": "#2A6F97"}, "locale": "vi"},
                      headers=_tenant())
    assert resp.status_code == 200
    assert service["appearance"] == [(TID, {"theme": {"accent": "#2A6F97"}, "locale": "vi",
                                            "actor": f"membership:{MID}"})]


@pytest.mark.parametrize("role", ["editor", "viewer"])
@pytest.mark.parametrize("path,body", [
    ("/v1/account/tripplanner/lead-route", {"lead_channel": "none"}),
    ("/v1/account/tripplanner/appearance", {"theme": {}, "locale": "en"}),
])
def test_only_the_owner_writes(client, service, role, path, body):
    resp = client.put(path, json=body, headers=_tenant(role))
    assert resp.status_code == 403 and resp.json()["detail"] == "studio_role_forbidden"
    assert service["route"] == [] and service["appearance"] == []


def test_bad_bodies_and_no_token_are_refused(client, service):
    assert client.put("/v1/account/tripplanner/lead-route", json={"lead_channel": "crm"},
                      headers=_tenant()).status_code == 422
    assert client.put("/v1/account/tripplanner/lead-route", json={"lead_channel": "none", "tenant_id": TID},
                      headers=_tenant()).status_code == 422
    assert client.put("/v1/account/tripplanner/appearance", json={"theme": {}, "locale": "fr"},
                      headers=_tenant()).status_code == 422
    assert client.get("/v1/account/tripplanner").status_code in (401, 403)
    assert service["route"] == [] and service["appearance"] == []


@pytest.mark.parametrize("error,status", [
    (tp.LeadRouteError(tp.EMAIL_NOT_PUBLIC, "lead_email"), 422),
    (tp.LeadRouteError("tripplanner_not_active", status=409, message="no planner"), 409),
    (tp.LeadRouteError("tenant_offboarded", status=409, message="closing"), 409),
])
def test_editor_refusals_carry_the_code(client, monkeypatch, error, status):
    monkeypatch.setattr(tp, "set_agency_lead_route", AsyncMock(side_effect=error))
    resp = client.put("/v1/account/tripplanner/lead-route",
                      json={"lead_channel": "email", "lead_email": "desk@quillbrook.local"}, headers=_tenant())
    assert resp.status_code == status
    assert resp.headers["X-Error-Code"] == error.code and resp.json()["detail"]["detail"] == error.code


def test_unknown_tenant_and_missing_table(client, monkeypatch):
    monkeypatch.setattr(tp, "module_view", AsyncMock(return_value=None))
    resp = client.get("/v1/account/tripplanner", headers=_tenant())
    assert resp.status_code == 404 and resp.headers["X-Error-Code"] == "tenant_not_found"
    monkeypatch.setattr(tp, "module_view", AsyncMock(side_effect=asyncpg.UndefinedTableError("x")))
    assert client.get("/v1/account/tripplanner", headers=_tenant()).status_code == 503


def test_view_as_reaches_none_of_them():
    assert not any(r.startswith("/v1/account/tripplanner") for r in view_as.READ_ROUTES)


def test_the_real_app_serves_the_three_routes():
    from api.main import app

    paths = app.openapi()["paths"]
    assert "get" in paths["/v1/account/tripplanner"]
    assert "put" in paths["/v1/account/tripplanner/lead-route"]
    assert "put" in paths["/v1/account/tripplanner/appearance"]
