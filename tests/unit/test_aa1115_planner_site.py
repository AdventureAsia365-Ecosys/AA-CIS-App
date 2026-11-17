"""AA-1115 (root ADR 0019 d10, entitlement tp.launcher): the ``planner`` of the tripplanner add-on entry.

``GET /v1/account/add-ons`` adds ``planner`` {url, domains, launcher} to an ACTIVE tripplanner entry, so the Content
studio's Add-ons page (AA-Partner ``web/lib/plannerLauncher.ts``) can show the launcher snippet. Pure rules of
``services/tenant_account/planner_site.py`` and the router wiring; offline, invented names only. The SQL against real
Postgres: ``tests/integration/test_aa1115_planner_site_pg.py``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.tenant_account import addon_requests as ar
from services.tenant_account import planner_site as ps

ORIGIN = "https://planner.example.test"


def test_a_hosted_tenant_gets_its_planner_url_domains_and_launcher():
    out = ps.planner_site("quillmere-voyages", ["tp.hosted", "tp.launcher"],
                          ["trips.quillmere.example", "plan.quillmere.example", "trips.quillmere.example", None],
                          in_service=True, origin=ORIGIN)
    assert out == {"url": f"{ORIGIN}/quillmere-voyages/plan",
                   "domains": ["plan.quillmere.example", "trips.quillmere.example"], "launcher": True}


def test_no_launcher_entitlement_keeps_the_url_but_says_so():
    out = ps.planner_site("quillmere-voyages", ["tp.hosted"], [], in_service=True, origin=ORIGIN)
    assert out == {"url": f"{ORIGIN}/quillmere-voyages/plan", "domains": [], "launcher": False}


@pytest.mark.parametrize(("slug", "features", "in_service"), [
    ("quillmere-voyages", ["tp.hosted", "tp.launcher"], False),   # out of service: TripPlanner answers 404
    ("quillmere-voyages", ["tp.launcher"], True),                 # no tp.hosted: no hosted planner to open
    ("quillmere-voyages", [], True),
    ("Quillmere Voyages", ["tp.hosted", "tp.launcher"], True),    # a slug TripPlanner cannot serve
    ("-quillmere", ["tp.hosted", "tp.launcher"], True),
    (None, ["tp.hosted", "tp.launcher"], True),
])
def test_no_planner_when_the_planner_would_be_a_404(slug, features, in_service):
    assert ps.planner_site(slug, features, ["plan.quillmere.example"], in_service=in_service, origin=ORIGIN) is None


@pytest.mark.parametrize(("env", "origin"), [
    (None, ps.DEFAULT_ORIGIN),
    ("", ps.DEFAULT_ORIGIN),
    ("https://Planner.Example.Test/api/revalidate/catalog", "https://planner.example.test"),
    ("http://planner.example.test/api/revalidate/catalog", ps.DEFAULT_ORIGIN),        # https only
    ("https://user:pw@planner.example.test/api/revalidate/catalog", ps.DEFAULT_ORIGIN),
    ("not a url", ps.DEFAULT_ORIGIN),
])
def test_the_origin_comes_from_the_public_tripplanner_host(monkeypatch, env, origin):
    if env is None:
        monkeypatch.delenv(ps.ORIGIN_ENV, raising=False)
    else:
        monkeypatch.setenv(ps.ORIGIN_ENV, env)
    assert ps.planner_origin() == origin


@pytest.mark.asyncio
async def test_read_queries_the_tenant_once_and_answers_none_for_an_unknown_one(monkeypatch):
    monkeypatch.delenv(ps.ORIGIN_ENV, raising=False)
    tid = "2c1e4b7a-0000-4000-8000-00000000a115"
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"slug": "ottervale-journeys", "features": ["tp.hosted"],
                                            "domains": ["trips.ottervale.example"]})
    out = await ps.read(conn, tid, in_service=True)
    assert out == {"url": f"{ps.DEFAULT_ORIGIN}/ottervale-journeys/plan", "domains": ["trips.ottervale.example"],
                   "launcher": False}
    sql, tenant, features = conn.fetchrow.await_args.args
    assert tenant == tid and features == ["tp.hosted", "tp.launcher"]
    assert "d.status = 'active'" in sql and "shared.v_tenant_entitlement_resolved" in sql
    conn.fetchrow = AsyncMock(return_value=None)
    assert await ps.read(conn, tid, in_service=True) is None


# ── router ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tclient():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.middleware import tenant_role
    from api.routers import v1_account_addons

    app = FastAPI()
    app.middleware("http")(tenant_role.tenant_role_middleware)
    app.include_router(v1_account_addons.router)
    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.pool = pool
    return TestClient(app, raise_server_exceptions=True)


TENANT_ID = "4d8f2a61-0000-4000-8000-00000000a115"
MEMBERSHIP_ID = "4d8f2a61-0000-4000-8000-00000000b115"


def _tenant(role="viewer"):
    from api.routers.auth import _create_partner_session_jwt
    token, _ = _create_partner_session_jwt(TENANT_ID, "Quillmere Voyages", "growth", membership_id=MEMBERSHIP_ID,
                                           content_studio_role=role)
    return {"Authorization": f"Bearer {token}"}


def _view(tripplanner_state):
    states = {"crm": "available", "tripplanner": tripplanner_state}
    return {"in_service": True,
            "add_ons": [{"product": p, "state": s, "subscription_status": "active" if s == "active" else None,
                         "request": None, "last_decision": None} for p, s in states.items()]}


def test_an_active_tripplanner_entry_carries_its_planner(tclient, monkeypatch):
    monkeypatch.setattr(ar, "tenant_view", AsyncMock(return_value=_view("active")))
    site = {"url": f"{ORIGIN}/quillmere-voyages/plan", "domains": ["plan.quillmere.example"], "launcher": True}
    read = AsyncMock(return_value=site)
    monkeypatch.setattr(ps, "read", read)
    resp = tclient.get("/v1/account/add-ons", headers=_tenant())
    assert resp.status_code == 200
    body = {a["product"]: a for a in resp.json()["add_ons"]}
    assert body["tripplanner"]["planner"] == site
    assert body["crm"]["planner"] is None
    assert read.await_args.args[1] == TENANT_ID and read.await_args.kwargs == {"in_service": True}


@pytest.mark.parametrize("state", ["requested", "available"])
def test_a_tripplanner_that_is_not_active_has_no_planner_and_no_query(tclient, monkeypatch, state):
    monkeypatch.setattr(ar, "tenant_view", AsyncMock(return_value=_view(state)))
    read = AsyncMock()
    monkeypatch.setattr(ps, "read", read)
    resp = tclient.get("/v1/account/add-ons", headers=_tenant())
    assert resp.status_code == 200
    assert all(a["planner"] is None for a in resp.json()["add_ons"])
    read.assert_not_awaited()


def test_an_active_tripplanner_without_a_hosted_planner_answers_null(tclient, monkeypatch):
    monkeypatch.setattr(ar, "tenant_view", AsyncMock(return_value=_view("active")))
    monkeypatch.setattr(ps, "read", AsyncMock(return_value=None))
    resp = tclient.get("/v1/account/add-ons", headers=_tenant())
    assert resp.status_code == 200
    assert {a["product"]: a["planner"] for a in resp.json()["add_ons"]} == {"crm": None, "tripplanner": None}


def test_the_crm_relay_contract_does_not_carry_the_planner():
    """tenant.addons.v1 (the CRM relay, api/routers/tenant_addons.py) keeps its frozen shape."""
    from api.routers import v1_account_addons as r
    assert "planner" not in r.AddOnState.model_fields
    assert "planner" in r.StudioAddOnState.model_fields
