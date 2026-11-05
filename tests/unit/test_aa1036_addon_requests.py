"""AA-1036 R3 (root ADR 0019 d7) — add-on requests without a database: the pure state rules, the text checks, the staff
route policy, and both route sets over HTTP on the real app (tenant role middleware, staff policy, error mapping).
The database paths are in tests/integration/test_aa1036_addon_requests_pg.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from api.core import tenant_scope as ts
from api.core.staff_authz import POLICY, TENANT_BOOK_WRITE, TENANT_WRITE
from api.routers.auth import _create_partner_session_jwt
from services.tenant_account import addon_requests as ar
from services.tenant_account.plans import PlanError

TID = "3c9e1a7b-5d2f-4e8a-9b1c-0d2e3f4a5b6c"
RID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)


def _req(product="crm", status="pending", note=None, decision_note=None):
    decided = None if status == "pending" else T0
    return {"request_id": str(uuid.uuid4()), "product": product, "status": status, "note": note,
            "requested_by": "membership:x", "requested_at": T0, "decided_at": decided,
            "decided_by": None if status == "pending" else "admin:y", "decision_note": decision_note,
            "plan_key": "crm.core" if status == "approved" else None, "provision_job_id": None}


# ── pure rules ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_states_active_wins_then_pending_then_available():
    reqs = [_req("crm", "pending"), _req("tripplanner", "declined", decision_note="Later")]
    out = ar.add_on_states({}, reqs)
    assert [(a["product"], a["state"]) for a in out] == [("crm", "requested"), ("tripplanner", "available")]
    assert out[1]["last_decision"]["decision_note"] == "Later" and out[1]["request"] is None
    live = ar.add_on_states({"crm": "trialing"}, reqs)
    assert live[0]["state"] == "active" and live[0]["subscription_status"] == "trialing"


def test_the_tenant_row_hides_the_staff_actor_and_the_job():
    row = ar.tenant_row(_req("crm", "approved"))
    assert set(row) == {"request_id", "product", "status", "note", "requested_at", "decided_at", "decision_note",
                        "setup"}
    assert set(ar.staff_row(_req())) >= {"requested_by", "decided_by", "plan_key", "provision_job_id", "setup"}


@pytest.mark.parametrize("status,job_status,setup", [
    ("approved", "queued", "in_progress"), ("approved", "running", "in_progress"), ("approved", "succeeded", "done"),
    ("approved", "failed", "failed"), ("approved", "cancelled", "failed"), ("approved", "stopped_budget", "failed"),
    ("approved", None, None), ("declined", "failed", None), ("pending", None, None),
])
def test_an_approval_shows_how_its_setup_went(status, job_status, setup):
    """Review AA-1036: an approved request whose attach saga failed is not a silent "approved"."""
    assert ar.setup_state(status, job_status) == setup
    r = {**_req("crm", status), "job_status": job_status}
    assert ar.staff_row(r)["setup"] == setup and ar.tenant_row(r)["setup"] == setup


@pytest.mark.parametrize("status,is_active,cancelled,ok", [
    ("active", True, None, True), ("trial", True, None, True), ("past_due", True, None, True),
    ("suspended", False, None, False), ("active", False, None, False), ("active", True, T0, False),
    ("offboarding", False, T0, False), ("closed", False, T0, False), ("lead", True, None, False),
])
def test_in_service_reads_every_lifecycle_column(status, is_active, cancelled, ok):
    assert ar.in_service({"account_status": status, "is_active": is_active, "cancelled_at": cancelled}) is ok


@pytest.mark.parametrize("value,required,code", [("  ", True, "reason_required"), ("x" * 501, False, "note_too_long")])
def test_text_rules(value, required, code):
    with pytest.raises(PlanError) as e:
        ar._text(value, "reason" if required else "note", required=required)
    assert e.value.code == code


def test_text_trims_and_empty_note_is_none():
    assert ar._text("  hi  ", "note", required=False) == "hi"
    assert ar._text("", "note", required=False) is None and ar._text(None, "note", required=False) is None


def test_unknown_product_is_refused():
    with pytest.raises(PlanError) as e:
        ar._product("booking")
    assert e.value.code == "unknown_product"


def test_message_names_the_agency_and_the_add_on():
    assert ar._message("Marigold Ferry Tours", "marigold", "tripplanner", None) == \
        "Marigold Ferry Tours (marigold) asks for the Trip planner add-on"
    assert ar._message("M", "m", "crm", "please").endswith('CRM add-on: "please"')


def test_policy_entries():
    assert POLICY[("GET", "/admin/tenants/{tenant_id}/addon-requests")] == \
        POLICY[("GET", "/admin/tenants/{tenant_id}/subscription")]
    assert POLICY[("GET", "/admin/tenants/{tenant_id}/addon-requests/{request_id}")] == \
        POLICY[("GET", "/admin/tenants/{tenant_id}/subscription")]
    # clarifications TL-7 step 4: admin, or the account manager (tenant.book.write, its book through the scope)
    for verb in ("approve", "decline"):
        cap = POLICY[("POST", f"/admin/tenants/{{tenant_id}}/addon-requests/{{request_id}}/{verb}")]
        assert cap == POLICY[("PUT", "/admin/tenants/{tenant_id}/config")]
        assert set(cap) == {TENANT_WRITE, TENANT_BOOK_WRITE}


def test_the_staff_bell_event_targets_admin_and_the_account_manager():
    from services.notifications import _DEFAULT_ROLES, EventType
    assert _DEFAULT_ROLES[EventType.TENANT_ADDON_REQUESTED] == ["admin", "customer_success"]


# ── HTTP ───────────────────────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "aa1036-addon-test-secret"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"status": "queued", "error": None})
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=True), conn


STAFF_ID = "5b0c7d2e-1a3f-4c6d-8e9f-0a1b2c3d4e5f"


def _staff(role="admin"):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": STAFF_ID}
    if role is not None:
        h["x-admin-role"] = role
    return h


MID = str(uuid.uuid4())


@pytest.fixture
def tclient(monkeypatch):
    """The tenant router behind the tenant role middleware only (the real app adds the per-tenant rate limiter on
    Redis, which a unit test does not run)."""
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
    return TestClient(app, raise_server_exceptions=True), conn


def _tenant(role="owner"):
    token, _ = _create_partner_session_jwt(TID, "Marigold Ferry Tours", "growth", membership_id=MID,
                                           content_studio_role=role)
    return {"Authorization": f"Bearer {token}"}


def test_owner_asks_and_the_actor_is_the_membership(tclient, monkeypatch):
    c, _ = tclient
    row = ar.tenant_row(_req())
    create = AsyncMock(return_value=(row, True))
    monkeypatch.setattr(ar, "create_request", create)
    resp = c.post("/v1/account/add-on-requests", json={"product": "crm", "note": "for sales"}, headers=_tenant())
    assert resp.status_code == 201 and resp.json() == {"created": True, "request": row}
    assert create.await_args.args[1:] == (TID, "crm", "for sales")
    assert create.await_args.kwargs["actor"] == f"membership:{MID}"
    create.return_value = (row, False)
    again = c.post("/v1/account/add-on-requests", json={"product": "crm"}, headers=_tenant())
    assert again.status_code == 200 and again.json()["created"] is False


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_the_owner_may_ask(tclient, monkeypatch, role):
    c, _ = tclient
    create = AsyncMock()
    monkeypatch.setattr(ar, "create_request", create)
    resp = c.post("/v1/account/add-on-requests", json={"product": "crm"}, headers=_tenant(role))
    assert resp.status_code == 403
    create.assert_not_awaited()


def test_every_role_reads_the_list(tclient, monkeypatch):
    c, _ = tclient
    view = {"in_service": True, "add_ons": ar.add_on_states({}, [])}
    monkeypatch.setattr(ar, "tenant_view", AsyncMock(return_value=view))
    for role in ("owner", "editor", "viewer"):
        resp = c.get("/v1/account/add-ons", headers=_tenant(role))
        assert resp.status_code == 200 and resp.json()["add_ons"][0]["state"] == "available"


def test_tenant_routes_refuse_bad_input_and_map_refusals(tclient, monkeypatch):
    c, _ = tclient
    assert c.post("/v1/account/add-on-requests", json={"product": "booking"}, headers=_tenant()).status_code == 422
    assert c.post("/v1/account/add-on-requests", json={"product": "crm", "x": 1}, headers=_tenant()).status_code == 422
    assert c.post("/v1/account/add-on-requests", json={"product": "crm"}).status_code in (401, 403)
    monkeypatch.setattr(ar, "create_request",
                        AsyncMock(side_effect=PlanError("already_subscribed", "on", 409)))
    resp = c.post("/v1/account/add-on-requests", json={"product": "crm"}, headers=_tenant())
    assert resp.status_code == 409 and resp.headers["X-Error-Code"] == "already_subscribed"
    monkeypatch.setattr(ar, "create_request", AsyncMock(side_effect=asyncpg.UndefinedTableError("x")))
    assert c.post("/v1/account/add-on-requests", json={"product": "crm"}, headers=_tenant()).status_code == 503
    monkeypatch.setattr(ar, "tenant_view", AsyncMock(side_effect=asyncpg.UndefinedTableError("x")))
    assert c.get("/v1/account/add-ons", headers=_tenant()).status_code == 503


@pytest.mark.parametrize("role", ["content", "support", "finance", "tenant_qa", None])
def test_decisions_refuse_roles_without_a_book_write(client, monkeypatch, role):
    c, _ = client
    approve, decline = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ar, "approve", approve)
    monkeypatch.setattr(ar, "decline", decline)
    base = f"/admin/tenants/{TID}/addon-requests/{RID}"
    assert c.post(f"{base}/approve", json={"plan_key": "crm.core"}, headers=_staff(role)).status_code == 403
    assert c.post(f"{base}/decline", json={"reason": "no"}, headers=_staff(role)).status_code == 403
    approve.assert_not_awaited()
    decline.assert_not_awaited()


def _book(monkeypatch, ok: bool):
    """The account manager's book: require_tenant_scope's lookup answers ``ok`` for any tenant."""
    seen = []

    async def in_scope(conn, scope, staff_id, tenant_id):
        seen.append((scope, staff_id, tenant_id))
        return ok
    monkeypatch.setattr(ts, "in_scope", in_scope)
    return seen


def test_the_account_manager_decides_for_its_own_book(client, monkeypatch):
    """Review AA-1036 (clarifications TL-7 step 4): customer_success approves and declines on a tenant of its book."""
    c, _ = client
    seen = _book(monkeypatch, True)
    job = str(uuid.uuid4())
    approve = AsyncMock(return_value=(ar.staff_row(_req("crm", "approved")), job))
    decline = AsyncMock(return_value=ar.staff_row(_req("crm", "declined", decision_note="Later")))
    monkeypatch.setattr(ar, "approve", approve)
    monkeypatch.setattr(ar, "decline", decline)
    base = f"/admin/tenants/{TID}/addon-requests/{RID}"
    resp = c.post(f"{base}/approve", json={"plan_key": "crm.core"}, headers=_staff("customer_success"))
    assert resp.status_code == 202 and resp.json()["job_id"] == job
    assert approve.await_args.kwargs["actor"] == f"admin:{STAFF_ID}"
    assert c.post(f"{base}/decline", json={"reason": "Later"}, headers=_staff("customer_success")).status_code == 200
    assert seen and all(s[0] == ts.SCOPE_BOOK and s[2] == TID for s in seen)


def test_the_account_manager_never_decides_outside_its_book(client, monkeypatch):
    c, _ = client
    _book(monkeypatch, False)
    approve, decline = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ar, "approve", approve)
    monkeypatch.setattr(ar, "decline", decline)
    base = f"/admin/tenants/{TID}/addon-requests/{RID}"
    resp = c.post(f"{base}/approve", json={"plan_key": "crm.core"}, headers=_staff("customer_success"))
    assert resp.status_code == 403 and resp.json()["detail"] == ts.OUT_OF_SCOPE
    assert c.post(f"{base}/decline", json={"reason": "no"}, headers=_staff("customer_success")).status_code == 403
    approve.assert_not_awaited()
    decline.assert_not_awaited()


def test_one_request_answers_the_job_shape_for_the_poll(client, monkeypatch):
    c, _ = client
    _book(monkeypatch, True)
    job = str(uuid.uuid4())
    row = {**ar.staff_row({**_req("crm", "approved"), "job_status": "failed"}), "provision_job_id": job}
    one = AsyncMock(return_value=(row, "failed", "unknown_plan: no live plan crm.gone"))
    monkeypatch.setattr(ar, "staff_one", one)
    resp = c.get(f"/admin/tenants/{TID}/addon-requests/{RID}", headers=_staff("customer_success"))
    assert resp.status_code == 200
    body = resp.json()
    assert (body["job_id"], body["status"], body["request"]["setup"]) == (job, "failed", "failed")
    assert body["error"].startswith("unknown_plan")
    assert one.await_args.args[1:] == (TID, RID)
    monkeypatch.setattr(ar, "staff_one", AsyncMock(side_effect=PlanError("request_not_found", "no", 404)))
    missing = c.get(f"/admin/tenants/{TID}/addon-requests/{RID}", headers=_staff())
    assert missing.status_code == 404 and missing.headers["X-Error-Code"] == "request_not_found"
    assert c.get(f"/admin/tenants/{TID}/addon-requests/{RID}", headers=_staff("content")).status_code == 403


def test_approve_answers_the_provision_job_shape(client, monkeypatch):
    c, _ = client
    job = str(uuid.uuid4())
    approve = AsyncMock(return_value=(ar.staff_row(_req("crm", "approved")), job))
    monkeypatch.setattr(ar, "approve", approve)
    resp = c.post(f"/admin/tenants/{TID}/addon-requests/{RID}/approve",
                  json={"plan_key": "crm.core", "seats": 3, "note": "ok"}, headers=_staff())
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == job and body["status"] == "queued" and body["request"]["status"] == "approved"
    assert approve.await_args.args[1:] == (TID, RID)
    kw = approve.await_args.kwargs
    assert (kw["plan_key"], kw["seats"], kw["note"], kw["actor"]) == ("crm.core", 3, "ok", f"admin:{STAFF_ID}")
    bad = c.post(f"/admin/tenants/{TID}/addon-requests/{RID}/approve", json={"plan_key": "CRM"}, headers=_staff())
    assert bad.status_code == 422


def test_decision_refusals_are_mapped(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(ar, "decline", AsyncMock(side_effect=PlanError("already_decided", "done", 409)))
    resp = c.post(f"/admin/tenants/{TID}/addon-requests/{RID}/decline", json={"reason": "no"}, headers=_staff())
    assert resp.status_code == 409 and resp.json()["detail"]["detail"] == "already_decided"
    assert c.post(f"/admin/tenants/{TID}/addon-requests/{RID}/decline", json={"reason": ""},
                  headers=_staff()).status_code == 422
    monkeypatch.setattr(ar, "staff_list", AsyncMock(return_value=[ar.staff_row(_req())]))
    listed = c.get(f"/admin/tenants/{TID}/addon-requests", headers=_staff())
    assert listed.status_code == 200 and listed.json()["pending"] == 1
    monkeypatch.setattr(ar, "staff_list", AsyncMock(side_effect=asyncpg.UndefinedTableError("x")))
    assert c.get(f"/admin/tenants/{TID}/addon-requests", headers=_staff()).status_code == 503


def test_the_tenant_activity_shows_the_request_and_the_decision_with_the_add_on_only():
    from api.services import tenant_account as ta
    for action in ("tenant.addon_requested", "tenant.addon_request_approved", "tenant.addon_request_declined"):
        assert ta.TENANT_VISIBLE_ACTIONS[action][0] == "account"
    assert ta.detail_text("tenant.addon_requested", {"product": "crm", "note": True}) == "CRM"
    assert ta.detail_text("tenant.addon_request_approved",
                          {"product": "tripplanner", "plan_key": "tripplanner.hosted",
                           "provision_job_id": "j"}) == "Trip planner"     # never the plan or the job
