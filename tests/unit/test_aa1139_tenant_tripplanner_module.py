"""AA-1139 — the TripPlanner module of the agency app, CIS side: tenant.addons.v1 1.1 (``/tenant/v1/tenants/{id}/
tripplanner``, ``.../lead-route``, ``.../appearance``; ``api/routers/tenant_addons.py``) and the editor rules of
``services/tenant_account/tripplanner_site.py``.

The bodies are validated against the pinned contract copy (``docs/contracts/tenant.addons.v1.openapi.yaml``). The
service calls are faked here; the database paths are in ``tests/integration/test_aa1139_tripplanner_module_pg.py``.
Invented names only.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import re
from typing import Any, Optional

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.core import m2m_auth
from api.routers import tenant_addons
from api.routers.tenant_addons import router
from services.tenant_account import tripplanner_site as tp
from tests.unit.aa976_m2m import install_m2m

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "tenant.addons.v1.openapi.yaml"
DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
TENANT = "1139ad00-0000-4000-8000-000000000001"
MEMBER = "1139ad00-0000-4000-8000-0000000000aa"
MODULE = {
    "active": True, "in_service": True,
    "planner": {"url": "https://planner.example.invalid/fernhollow-trails/plan",
                "domains": ["plan.fernhollow.example.com"], "launcher": True},
    "domains": [{"domain": "plan.fernhollow.example.com", "status": "active"},
                {"domain": "trips.fernhollow.example.com", "status": "validating"}],
    "crm_core": False,
    "lead_route": {"lead_channel": "email", "lead_webhook_url": None, "lead_email": "desk@fernhollow.example.com",
                   "effective_channel": "email", "updated_at": "2026-11-18T09:00:00+00:00"},
    "theme": {"accent": "#2a6f97"}, "locale": "vi", "ai_sessions_monthly": 300,
}


class FakePool:
    @asynccontextmanager
    async def acquire(self, timeout: Optional[float] = None):
        yield object()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(m2m_auth.ENV_M2M_ISSUER, raising=False)
    monkeypatch.delenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, raising=False)
    yield
    m2m_auth.set_m2m_verifier(None)


@pytest.fixture
def issuer():
    issuer, _ = install_m2m()
    return issuer


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    app.state.pool = FakePool()
    return TestClient(app)


@pytest.fixture
def service(monkeypatch):
    seen: dict[str, Any] = {"view": [], "route": [], "appearance": []}
    state: dict[str, Any] = {"module": dict(MODULE), "raise": None}

    async def module_view(conn, tenant_id, **_kw):
        seen["view"].append(tenant_id)
        return state["module"]

    async def set_agency_lead_route(conn, tenant_id, *, channel, webhook_url, email, actor):
        seen["route"].append((tenant_id, channel, webhook_url, email, actor))
        if state["raise"]:
            raise state["raise"]
        tp.validate_route(channel, webhook_url, email)

    async def set_appearance(conn, tenant_id, *, theme, locale, actor):
        seen["appearance"].append((tenant_id, theme, locale, actor))
        if state["raise"]:
            raise state["raise"]
        tp.validate_appearance(theme, locale)

    monkeypatch.setattr(tenant_addons.tp_site, "module_view", module_view)
    monkeypatch.setattr(tenant_addons.tp_site, "set_agency_lead_route", set_agency_lead_route)
    monkeypatch.setattr(tenant_addons.tp_site, "set_appearance", set_appearance)
    return seen, state


def _auth(issuer, scope=m2m_auth.SCOPE_TENANT_ADDONS) -> dict:
    return {"Authorization": f"Bearer {issuer.token(scope=scope)}"}


def _valid(name: str, body: Any) -> None:
    schema = Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": DOC["components"]})
    errors = sorted(schema.iter_errors(body), key=str)
    assert not errors, [e.message for e in errors]


# ── the contract ─────────────────────────────────────────────────────────────

def test_contract_is_1_1_and_the_models_are_its_schemas():
    assert DOC["info"]["version"] == "1.1"
    schemas = DOC["components"]["schemas"]
    assert set(tenant_addons.TripPlannerModule.model_fields) == set(schemas["TripPlannerModule"]["required"])
    assert set(tenant_addons.TenantLeadRouteIn.model_fields) == set(schemas["TripPlannerLeadRouteIn"]["properties"])
    assert set(tenant_addons.TenantAppearanceIn.model_fields) == set(schemas["TripPlannerAppearanceIn"]["properties"])
    assert schemas["BrandToken"]["enum"] == list(tp.THEME_TOKENS)
    assert set(schemas["PlannerLeadRoute"]["properties"]["lead_channel"]["enum"]) == set(tp.CHANNELS)


def test_every_editor_code_fits_the_problem_reason():
    pattern = DOC["components"]["schemas"]["Problem"]["properties"]["reason"]["pattern"]
    codes = [tp.URL_REQUIRED, tp.URL_TOO_LONG, tp.URL_NOT_HTTPS, tp.URL_INVALID, tp.URL_NOT_PUBLIC, tp.EMAIL_REQUIRED,
             tp.EMAIL_INVALID, tp.EMAIL_NOT_PUBLIC, "lead_channel_invalid", "theme_invalid", "theme_token_unknown",
             "theme_colour_invalid", "locale_invalid", "tripplanner_not_active", "tenant_offboarded"]
    assert all(re.fullmatch(pattern, c) for c in codes)
    described = DOC["components"]["schemas"]["Problem"]["properties"]["reason"]["description"]
    assert all(c.split("lead_webhook_url_")[-1].split("lead_email_")[-1] in described for c in codes)


# ── the routes ───────────────────────────────────────────────────────────────

def test_get_serves_the_module_in_the_contract_shape(client, issuer, service):
    r = client.get(f"/tenant/v1/tenants/{TENANT}/tripplanner", headers=_auth(issuer))
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    _valid("TripPlannerModule", r.json())
    assert r.json()["lead_route"]["effective_channel"] == "email" and service[0]["view"] == [TENANT]


def test_no_planner_and_no_quota_are_null(client, issuer, service):
    service[1]["module"] = {**MODULE, "active": False, "planner": None, "domains": [], "ai_sessions_monthly": None,
                            "theme": {}, "locale": "en"}
    r = client.get(f"/tenant/v1/tenants/{TENANT}/tripplanner", headers=_auth(issuer))
    assert r.status_code == 200
    _valid("TripPlannerModule", r.json())


def test_unknown_tenant_is_404(client, issuer, service):
    service[1]["module"] = None
    r = client.get(f"/tenant/v1/tenants/{TENANT}/tripplanner", headers=_auth(issuer))
    assert r.status_code == 404 and r.json()["reason"] == "tenant_not_found"
    r = client.get("/tenant/v1/tenants/not-a-uuid/tripplanner", headers=_auth(issuer))
    assert r.status_code == 404


@pytest.mark.parametrize("method,path,body", [
    ("GET", "tripplanner", None),
    ("PUT", "tripplanner/lead-route", {"lead_channel": "none", "crm_member_id": MEMBER}),
    ("PUT", "tripplanner/appearance", {"theme": {}, "locale": "en", "crm_member_id": MEMBER}),
])
def test_the_scope_is_required(client, issuer, service, method, path, body):
    r = client.request(method, f"/tenant/v1/tenants/{TENANT}/{path}", json=body)
    assert r.status_code == 401
    r = client.request(method, f"/tenant/v1/tenants/{TENANT}/{path}", json=body,
                       headers=_auth(issuer, scope=m2m_auth.SCOPE_TENANT_READ))
    assert r.status_code == 403
    assert service[0] == {"view": [], "route": [], "appearance": []}


def test_route_write_names_the_member_and_answers_the_module(client, issuer, service):
    body = {"lead_channel": "webhook", "lead_webhook_url": "https://hooks.fernhollow.example.com/tp",
            "lead_email": None, "crm_member_id": MEMBER}
    r = client.put(f"/tenant/v1/tenants/{TENANT}/tripplanner/lead-route", json=body, headers=_auth(issuer))
    assert r.status_code == 200
    _valid("TripPlannerModule", r.json())
    assert service[0]["route"] == [(TENANT, "webhook", body["lead_webhook_url"], None, f"crm-member:{MEMBER}")]


@pytest.mark.parametrize("url,code", [
    ("https://10.0.3.7/leads", tp.URL_NOT_PUBLIC),
    ("https://crm.corp.internal/h", tp.URL_NOT_PUBLIC),
    ("http://hooks.fernhollow.example.com/tp", tp.URL_NOT_HTTPS),
])
def test_route_refusals_are_422_with_the_code(client, issuer, service, url, code):
    body = {"lead_channel": "webhook", "lead_webhook_url": url, "crm_member_id": MEMBER}
    r = client.put(f"/tenant/v1/tenants/{TENANT}/tripplanner/lead-route", json=body, headers=_auth(issuer))
    assert r.status_code == 422 and r.json()["reason"] == code and r.json()["error"] == "invalid"
    _valid("Problem", r.json())


def test_conflicts_are_409(client, issuer, service):
    service[1]["raise"] = tp.LeadRouteError("tripplanner_not_active", status=409, message="no planner")
    r = client.put(f"/tenant/v1/tenants/{TENANT}/tripplanner/appearance",
                   json={"theme": {}, "locale": "en", "crm_member_id": MEMBER}, headers=_auth(issuer))
    assert r.status_code == 409 and r.json() == {"error": "conflict", "message": "no planner",
                                                 "reason": "tripplanner_not_active"}


@pytest.mark.parametrize("body", [
    {"lead_channel": "crm", "crm_member_id": MEMBER},
    {"lead_channel": "none"},
    {"lead_channel": "none", "crm_member_id": MEMBER, "extra": 1},
])
def test_bad_route_bodies_are_400(client, issuer, service, body):
    r = client.put(f"/tenant/v1/tenants/{TENANT}/tripplanner/lead-route", json=body, headers=_auth(issuer))
    assert r.status_code == 400 and service[0]["route"] == []


def test_appearance_write(client, issuer, service):
    body = {"theme": {"accent": "#2A6F97", "on-accent": "#ffffff"}, "locale": "vi", "crm_member_id": MEMBER}
    r = client.put(f"/tenant/v1/tenants/{TENANT}/tripplanner/appearance", json=body, headers=_auth(issuer))
    assert r.status_code == 200
    assert service[0]["appearance"] == [(TENANT, body["theme"], "vi", f"crm-member:{MEMBER}")]


@pytest.mark.parametrize("theme,code", [({"background": "#ffffff"}, "theme_token_unknown"),
                                        ({"accent": "red"}, "theme_colour_invalid")])
def test_appearance_refusals(client, issuer, service, theme, code):
    r = client.put(f"/tenant/v1/tenants/{TENANT}/tripplanner/appearance",
                   json={"theme": theme, "locale": "en", "crm_member_id": MEMBER}, headers=_auth(issuer))
    assert r.status_code == 422 and r.json()["reason"] == code


# ── the editor rules ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("email,code", [
    ("desk@fernhollow.example.com", None),
    ("desk@printer.local", tp.EMAIL_NOT_PUBLIC),
    ("desk@crm.corp.internal", tp.EMAIL_NOT_PUBLIC),
    ("desk@mail.home.arpa", tp.EMAIL_NOT_PUBLIC),
    ("desk@localhost", tp.EMAIL_INVALID),
    ("not-an-address", tp.EMAIL_INVALID),
])
def test_email_domain_rule(email, code):
    assert tp.email_problem(email) == code


def test_appearance_is_normalised():
    assert tp.validate_appearance({"accent": "#2A6F97"}, "vi") == ({"accent": "#2a6f97"}, "vi")
    assert tp.validate_appearance({}, "en") == ({}, "en")
    for theme, locale in (([], "en"), ({"accent": "#2a6f9"}, "en"), ({}, "fr"), ({"accent": 1}, "en")):
        with pytest.raises(tp.LeadRouteError) as e:
            tp.validate_appearance(theme, locale)
        assert e.value.status == 422



# ── the agency's route edit checks the planner inside the write's transaction (review finding, no TOCTOU) ─────────

class _OrderConn:
    """Records the order of transaction entry, the tenant lock, the planner check and the writes."""

    def __init__(self, active=True, status="active"):
        self.active, self.status = active, status
        self.events: list[str] = []
        self.in_tx = False

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.in_tx = True
                conn.events.append("begin")

            async def __aexit__(self, *exc):
                conn.in_tx = False
                conn.events.append("end")
                return False

        return _Tx()

    async def execute(self, sql, *args):
        if "INSERT INTO shared.tenant_tripplanner_site" in sql:
            self.events.append("upsert" if self.in_tx else "upsert-outside")

    async def fetchval(self, sql, *args):
        if sql == tp.TENANT_LOCK_SQL:
            self.events.append("lock" if self.in_tx else "lock-outside")
            return self.status
        if sql == tp.ACTIVE_TP_SQL:
            self.events.append("planner" if self.in_tx else "planner-outside")
            return self.active
        if sql in (tp.TENANT_SQL, tp.HAS_TRIPPLANNER_SQL):
            return self.status if sql == tp.TENANT_SQL else self.active
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        return None


def test_agency_route_edit_checks_the_planner_after_the_lock_in_one_transaction(monkeypatch):
    import asyncio

    async def audit(conn, **_kw):
        conn.events.append("audit")

    monkeypatch.setattr(tp, "write_audit_log", audit)
    conn = _OrderConn()
    asyncio.run(tp.set_agency_lead_route(conn, TENANT, channel="email", webhook_url=None,
                                         email="desk@fernhollow.example.com", actor=f"membership:{MEMBER}"))
    assert conn.events == ["begin", "lock", "planner", "upsert", "audit", "end"]


@pytest.mark.parametrize("active,status,code", [(False, "active", "tripplanner_not_active"),
                                                (True, "offboarding", "tenant_offboarded"),
                                                (True, None, "tenant_not_found")])
def test_agency_route_edit_refusals_happen_inside_the_transaction_before_any_write(monkeypatch, active, status, code):
    import asyncio

    monkeypatch.setattr(tp, "write_audit_log", lambda *a, **k: pytest.fail("no audit on a refusal"))
    conn = _OrderConn(active=active, status=status)
    with pytest.raises(tp.LeadRouteError) as e:
        asyncio.run(tp.set_agency_lead_route(conn, TENANT, channel="none", webhook_url=None, email=None,
                                             actor=f"membership:{MEMBER}"))
    assert e.value.code == code
    assert "upsert" not in conn.events and not any(ev.endswith("-outside") for ev in conn.events)
    assert conn.events[0] == "begin" and conn.events[-1] == "end"
