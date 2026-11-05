"""AA-1035 (S267 fix pass) — the findings of the independent review of the R2 roles slice, through the real app.

1. support vs the owner seat: support holds only tenant.member.resend (send a non-owner invitation again); invite,
   role change, removal and an owner send-again need more, and every owner write (invite as owner, change to / from
   owner, remove an owner, send an owner invitation again) needs tenant.write, so customer_success never reaches it.
2. finance opens Tenant 360: /details answers billing.read with the header only (no tours, runs or brand text).
3. the shared tenant picker: GET /admin/tenant-names (names only, scoped) for every role whose pages pick a tenant.
5. customer_success writes the non-billing Tenant 360 of its book (tenant.book.write; PATCH is field-level).
6. schema-gate and the migration preflight see REQUIRED_CIS_ABOVE_GAP (362-364) of api/core/readiness.py.
(4, the Writing Lab page rule for ai_ops, is frontend/lib/adminIa.test.mjs.)

No network, no AWS, no DB: a fake connection answers exactly the SQL each route runs and fails the test on any other.
"""
from __future__ import annotations

import importlib
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.core import readiness, staff_authz
from api.core import tenant_scope as ts
from api.routers import admin
from api.routers import admin_tenant360 as t360
from api.services import crm_console
from services.tenant_membership import session as studio
from services.tenant_membership import writer

SECRET = f"aa1035-fix-{uuid.uuid4().hex}"   # a fresh value per run
STAFF = "11111111-1111-4111-8111-111111111111"
MINE = "22222222-2222-4222-8222-222222222222"
POOLED = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
OWNER_ROW = "55555555-5555-4555-8555-555555555555"
EDITOR_ROW = "66666666-6666-4666-8666-666666666666"
OWNER_INVITE = "77777777-7777-4777-8777-777777777777"
EDITOR_INVITE = "88888888-8888-4888-8888-888888888888"
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)
ADDRESS = "someone@agency.example"


def _member(mid: str, role: str, status: str) -> dict:
    return {"membership_id": uuid.UUID(mid), "cognito_iss": None, "cognito_sub": None, "content_studio_role": role,
            "status": status, "invitation_id": uuid.uuid4() if status == "invited" else None, "created_at": NOW,
            "created_by": "admin:x", "updated_at": NOW, "removed_at": None, "removed_by": None}


class FakeDb:
    """Pool and connection at once. Answers the scope SQL (STAFF's book = {MINE}, pool = {POOLED}), the tenant row,
    the membership reads and writes (recorded, never executed) and the audit rows; any other SQL fails the test."""

    def __init__(self) -> None:
        self.members = {mid: _member(mid, role, status) for mid, role, status in (
            (OWNER_ROW, "owner", "active"), (EDITOR_ROW, "editor", "active"),
            (OWNER_INVITE, "owner", "invited"), (EDITOR_INVITE, "editor", "invited"))}
        self.writes: list[tuple] = []
        self.sql: list[str] = []

    def acquire(self):
        db = self

        @asynccontextmanager
        async def ctx():
            yield db

        return ctx()

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        if sql == ts.SQL_IN_BOOK:
            return args[0] == STAFF and args[1] == MINE
        if sql == ts.SQL_IN_POOL:
            return args[0] == POOLED
        raise AssertionError(f"unexpected fetchval {sql}")

    async def fetch(self, sql: str, *args: Any) -> list:
        self.sql.append(sql)
        if sql == writer.LIST_SQL:
            return list(self.members.values())
        if sql == admin.TENANT_NAMES_SQL:
            return [{"tenant_id": uuid.UUID(t), "name": n, "slug": n.lower(), "is_active": t != OTHER,
                     "cancelled_at": NOW if t == OTHER else None}
                    for t, n in ((MINE, "Mine"), (POOLED, "Pooled"), (OTHER, "Other"),
                                 (ts.AA_INTERNAL_TENANT, "AA"))]
        if sql == ts.SQL_BOOK:
            return [{"tenant_id": MINE}] if args[0] == STAFF else []
        if sql == ts.SQL_POOL:
            return [{"tenant_id": POOLED}]
        raise AssertionError(f"unexpected fetch {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        if "FROM shared.tenants WHERE tenant_id = $1" in sql and "plan_tier::text AS plan_tier" in sql:
            return {"tenant_id": args[0], "name": "T", "plan_tier": "starter"}
        if sql == studio.TENANT_ACTIVE_SQL:
            return {"is_active": True, "cancelled_at": None}
        if sql == studio.INVITE_SQL:
            self.writes.append(("invite", args[1]))
            return _member(str(uuid.uuid4()), args[1], "invited")
        if sql == writer.LOCK_SQL:
            return self.members.get(str(args[1]))
        if sql == writer.ROLE_SQL:
            self.writes.append(("role", str(args[1]), args[2]))
            return dict(self.members[str(args[1])], content_studio_role=args[2])
        if sql == writer.REMOVE_SQL:
            self.writes.append(("remove", str(args[1])))
            return dict(self.members[str(args[1])], status="removed", removed_at=NOW, removed_by=args[2])
        raise AssertionError(f"unexpected fetchrow {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        if "acp_shared.audit_log" in sql or "shared.tenant_api_usage" in sql:
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute {sql}")


@pytest.fixture
def app_client(monkeypatch):
    from api.core import staff_credential
    from api.main import app

    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    monkeypatch.setattr(staff_credential, "principal_verified", lambda: False)
    db = FakeDb()
    monkeypatch.setattr(app.state, "pool", db, raising=False)
    relays: list[dict] = []

    async def fake_relay(method, path, authorization, *, json=None, params=None):
        relays.append(json)
        return 202, {"status": "pending", "expires_at": "2026-11-11T00:00:00Z"}

    monkeypatch.setattr(crm_console, "crm_ops_call", fake_relay)
    return TestClient(app, raise_server_exceptions=False), db, relays


def _h(role: str) -> dict:
    return {"X-Admin-Secret": SECRET, "x-admin-role": role, "x-admin-user-id": STAFF}


def _invite(c, role: str, studio_role: str, tenant: str = MINE):
    return c.post(f"/admin/tenants/{tenant}/members/invitations", headers=_h(role),
                  json={"email": ADDRESS, "content_studio_role": studio_role})


def _patch(c, role: str, mid: str, studio_role: str):
    return c.patch(f"/admin/tenants/{MINE}/members/{mid}", headers=_h(role), json={"content_studio_role": studio_role})


def _remove(c, role: str, mid: str):
    return c.post(f"/admin/tenants/{MINE}/members/{mid}/remove", headers=_h(role))


def _resend(c, role: str, mid: str, tenant: str = MINE, email: "str | None" = ADDRESS):
    """AA-1035 close-out: support sends again to the kept address only (no ``email``); pass email=None for it."""
    body = {"email": email} if email is not None else {}
    return c.post(f"/admin/tenants/{tenant}/members/{mid}/invitation/send", headers=_h(role), json=body)


def _owner_refused(res) -> bool:
    return res.status_code == 403 and "owner role needs" in res.json()["detail"]


# ── 1. support / customer_success vs the owner seat ──────────────────────────────────────────────────────────────

def test_support_holds_resend_only():
    # slice 2 adds the read-only view-as and the bell, no write of a tenant
    assert staff_authz.ROLE_PERMS["support"] == {staff_authz.TENANT_BOOK_READ, staff_authz.TENANT_MEMBER_RESEND,
                                                 staff_authz.TENANT_VIEW_AS, staff_authz.NOTIFICATIONS_READ}
    assert staff_authz.TENANT_MEMBER_RESEND in staff_authz.ROLE_PERMS["customer_success"]


@pytest.mark.parametrize("studio_role", ["owner", "editor", "viewer"])
def test_support_never_invites(app_client, studio_role):
    c, db, relays = app_client
    res = _invite(c, "support", studio_role)
    assert res.status_code == 403 and res.json()["detail"].startswith("Role 'support' lacks"), res.text
    assert db.writes == [] and relays == []


@pytest.mark.parametrize("mid,studio_role", [(OWNER_ROW, "editor"), (EDITOR_ROW, "owner"), (EDITOR_ROW, "viewer")])
def test_support_never_changes_a_role(app_client, mid, studio_role):
    c, db, _ = app_client
    res = _patch(c, "support", mid, studio_role)
    assert res.status_code == 403 and res.json()["detail"].startswith("Role 'support' lacks"), res.text
    assert db.writes == []


@pytest.mark.parametrize("mid", [OWNER_ROW, EDITOR_ROW])
def test_support_never_removes(app_client, mid):
    c, db, _ = app_client
    assert _remove(c, "support", mid).status_code == 403
    assert db.writes == []


@pytest.mark.parametrize("tenant", [MINE, POOLED])
def test_support_sends_an_editor_invitation_again_in_its_book_and_the_pool(app_client, tenant):
    c, _, relays = app_client
    res = _resend(c, "support", EDITOR_INVITE, tenant, email=None)
    assert res.status_code == 200, res.text
    assert res.json()["delivery"]["status"] == "sent" and len(relays) == 1
    # AA-1035 close-out: to the address the CRM kept (crm.ops.v1 1.12), never an address support types
    assert relays[0].get("keep_address") is True and "email" not in relays[0]


def test_support_never_sends_an_invitation_to_a_new_address(app_client):
    c, _, relays = app_client
    res = _resend(c, "support", EDITOR_INVITE)
    assert res.status_code == 403 and res.json()["detail"] == t360.READDRESS_REFUSED
    assert relays == []


@pytest.mark.parametrize("role", ["support", "customer_success"])
def test_an_owner_invitation_is_never_sent_again_without_tenant_write(app_client, role):
    """The send-again re-seals the invitation to the body's address: it would hand the owner seat to anyone."""
    c, _, relays = app_client
    assert _owner_refused(_resend(c, role, OWNER_INVITE, email=ADDRESS if role == "customer_success" else None))
    assert relays == []


def test_support_outside_its_book_and_the_pool_is_refused(app_client):
    c, _, relays = app_client
    res = _resend(c, "support", EDITOR_INVITE, OTHER)
    assert res.status_code == 403 and res.json()["detail"] == ts.OUT_OF_SCOPE
    assert relays == []


def test_customer_success_never_invites_an_owner(app_client):
    c, db, relays = app_client
    assert _owner_refused(_invite(c, "customer_success", "owner"))
    assert db.writes == [] and relays == []


def test_customer_success_invites_an_editor(app_client):
    c, db, relays = app_client
    res = _invite(c, "customer_success", "editor")
    assert res.status_code == 201, res.text
    assert db.writes == [("invite", "editor")] and len(relays) == 1


@pytest.mark.parametrize("mid,studio_role", [(EDITOR_ROW, "owner"), (OWNER_ROW, "editor"), (OWNER_ROW, "viewer")])
def test_customer_success_never_changes_to_or_from_owner(app_client, mid, studio_role):
    c, db, _ = app_client
    assert _owner_refused(_patch(c, "customer_success", mid, studio_role))
    assert db.writes == []


def test_customer_success_changes_a_non_owner_role(app_client):
    c, db, _ = app_client
    res = _patch(c, "customer_success", EDITOR_ROW, "viewer")
    assert res.status_code == 200, res.text
    assert db.writes == [("role", EDITOR_ROW, "viewer")]


def test_customer_success_never_removes_an_owner_but_removes_an_editor(app_client):
    c, db, _ = app_client
    assert _owner_refused(_remove(c, "customer_success", OWNER_ROW))
    assert db.writes == []
    assert _remove(c, "customer_success", EDITOR_ROW).status_code == 200
    assert db.writes == [("remove", EDITOR_ROW)]


def test_admin_keeps_every_owner_write(app_client):
    c, db, relays = app_client
    assert _invite(c, "admin", "owner").status_code == 201
    assert _resend(c, "admin", OWNER_INVITE).status_code == 200
    assert _patch(c, "admin", EDITOR_ROW, "owner").status_code == 200
    assert _remove(c, "admin", OWNER_ROW).status_code == 200
    assert db.writes == [("invite", "owner"), ("role", EDITOR_ROW, "owner"), ("remove", OWNER_ROW)]
    assert len(relays) == 2


def _principal(role: str) -> staff_authz.StaffPrincipal:
    return staff_authz.principal_from_role(role)


def test_members_access_matches_the_routes():
    cs = t360.members_access(_principal("customer_success"), synthetic_tenant=False)
    assert cs == {"read_only": False, "actions": dict.fromkeys(t360.MEMBER_WRITES, True),
                  "invite_roles": ["editor", "viewer"], "owner_writes": False,
                  "send_again_to": {"same_address": True, "new_address": True}}
    sup = t360.members_access(_principal("support"), synthetic_tenant=False)
    assert sup == {"read_only": False,
                   "actions": {"invite": False, "send_again": True, "change_role": False, "remove": False},
                   "invite_roles": ["editor", "viewer"], "owner_writes": False,
                   "send_again_to": {"same_address": True, "new_address": False}}
    adm = t360.members_access(_principal("admin"), synthetic_tenant=False)
    assert adm["owner_writes"] is True and adm["invite_roles"] == ["owner", "editor", "viewer"]


@pytest.mark.asyncio
async def test_the_writer_checks_the_owner_on_the_locked_row():
    db = FakeDb()
    with pytest.raises(writer.OwnerNeedsTenantWrite):
        await writer.change_role(db, MINE, OWNER_ROW, "viewer", "admin:x", allow_owner=False)
    with pytest.raises(writer.OwnerNeedsTenantWrite):
        await writer.change_role(db, MINE, EDITOR_ROW, "owner", "admin:x", allow_owner=False)
    with pytest.raises(writer.OwnerNeedsTenantWrite):
        await writer.remove(db, MINE, OWNER_ROW, "admin:x", allow_owner=False)
    assert db.writes == []
    # the default keeps every other caller (the tenant-side routes) unchanged
    row, changed = await writer.change_role(db, MINE, OWNER_ROW, "editor", "admin:x")
    assert changed and row["content_studio_role"] == "editor"


# ── 2. finance opens Tenant 360 (/details, header only) ──────────────────────────────────────────────────────────

class DetailsDb(FakeDb):
    """The header reads of /details only: any tours, runs or brand read (fetch) fails the test."""

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        if "SELECT name, slug, plan_tier::text, rate_limit_rpm, created_at" in sql:
            return {"name": "T", "slug": "t", "plan_tier": "starter", "rate_limit_rpm": 60, "created_at": NOW}
        if "shared.v_tenant_monthly_usage" in sql:
            return {"api_calls_used": 5, "quota_calls_pct": 10, "api_calls_quota_monthly": 50}
        raise AssertionError(f"unexpected fetchrow {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        return 3

    async def fetch(self, sql: str, *args: Any) -> list:
        raise AssertionError(f"finance must not read tenant text or the catalog: {sql}")


def test_finance_reads_the_details_header_without_text(app_client, monkeypatch):
    from api.main import app

    c, _, _ = app_client
    monkeypatch.setattr(app.state, "pool", DetailsDb(), raising=False)
    res = c.get(f"/admin/tenants/{OTHER}/details", headers=_h("finance"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["summary"]["total_llm_cost_usd"] == 3.0          # finance sees money
    assert body["rewritten_tours"] == [] and body["pipeline_runs"] == []
    assert body["brand_rules"]["system_prompt"] is None and body["brand_rules"]["version_count"] == 0
    assert body["api_usage"]["total_calls"] == 5


@pytest.mark.parametrize("role", ["content", "ai_ops", "tenant_qa"])
def test_details_of_a_tenant_stays_closed_to_the_content_side(app_client, role):
    c, _, _ = app_client
    assert c.get(f"/admin/tenants/{OTHER}/details", headers=_h(role)).status_code == 403


# ── 3. the shared tenant picker ──────────────────────────────────────────────────────────────────────────────────

def _names(c, role: str):
    return c.get("/admin/tenant-names", headers=_h(role))


@pytest.mark.parametrize("role", ["admin", "content_lead", "tenant_qa", "ai_ops", "finance"])
def test_every_tenant_for_the_all_scope_roles(app_client, role):
    c, _, _ = app_client
    res = _names(c, role)
    assert res.status_code == 200, res.text
    rows = res.json()["tenants"]
    assert {r["tenant_id"] for r in rows} == {MINE, POOLED, OTHER, ts.AA_INTERNAL_TENANT}
    assert set(rows[0]) == {"tenant_id", "name", "slug", "status", "cancelled_at"}    # names only, no money
    assert next(r for r in rows if r["tenant_id"] == OTHER)["status"] == "offboarded"


def test_the_picker_is_scoped_for_the_book_roles(app_client):
    c, _, _ = app_client
    assert {r["tenant_id"] for r in _names(c, "customer_success").json()["tenants"]} == {MINE}
    assert {r["tenant_id"] for r in _names(c, "support").json()["tenants"]} == {MINE, POOLED}


@pytest.mark.parametrize("role", ["content", "viewer"])
def test_the_pure_content_roles_get_no_picker(app_client, role):
    c, _, _ = app_client
    assert _names(c, role).status_code == 403


def test_the_frontend_picker_reads_the_names_route():
    src = (Path(__file__).resolve().parents[2] / "frontend" / "lib" / "tenants.ts").read_text(encoding="utf-8")
    assert 'export const TENANTS_API = "/api/admin/tenant-names";' in src


# ── 5. customer_success writes the non-billing Tenant 360 of its book ────────────────────────────────────────────

@pytest.mark.parametrize("role,fields,ok", [
    ("customer_success", {"rate_limit_rpm"}, True),
    ("customer_success", {"daily_publish_limit"}, True),
    ("customer_success", {"rate_limit_rpm", "daily_publish_limit"}, True),
    ("customer_success", {"plan_tier"}, False),
    ("customer_success", {"is_active", "rate_limit_rpm"}, False),
    ("support", {"rate_limit_rpm"}, False),
    ("finance", {"rate_limit_rpm"}, False),
])
def test_tenant_patch_fields_for_the_book(role, fields, ok):
    from fastapi import HTTPException

    if ok:
        admin._require_tenant_patch(role, fields)
    else:
        with pytest.raises(HTTPException):
            admin._require_tenant_patch(role, fields)


@pytest.mark.parametrize("role,ok", [("admin", True), ("customer_success", True), ("support", False),
                                     ("finance", False), ("content_lead", False), (None, False)])
def test_config_and_brand_identity_guard(role, ok):
    from fastapi import HTTPException

    if ok:
        admin._require_tenant_book_write(role)
    else:
        with pytest.raises(HTTPException):
            admin._require_tenant_book_write(role)


@pytest.mark.parametrize("path,body", [
    ("/admin/tenants/{t}/config", {"markets": [], "channels": [], "capacity_posts_per_week": 1}),
    ("/admin/tenants/{t}/brand-identity", {"brand_name": "x"}),
])
def test_customer_success_writes_stay_in_its_book(app_client, path, body):
    c, _, _ = app_client
    res = c.put(path.format(t=OTHER), headers=_h("customer_success"), json=body)
    assert res.status_code == 403 and res.json()["detail"] == ts.OUT_OF_SCOPE


@pytest.mark.parametrize("path", ["/admin/tenants/{t}/offboard", "/admin/tenants/{t}/restore"])
def test_customer_success_never_offboards_or_restores(app_client, path):
    c, _, _ = app_client
    res = c.post(path.format(t=MINE), headers=_h("customer_success"), json={})
    assert res.status_code == 403 and res.json()["detail"].startswith("Role 'customer_success' lacks"), res.text


# ── 6. schema-gate and the preflight read REQUIRED_CIS_ABOVE_GAP ─────────────────────────────────────────────────

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / ".github" / "scripts"))
sg = importlib.import_module("schema_gate")
mp = importlib.import_module("migration_preflight")


def _source() -> str:
    return (REPO / "api" / "core" / "readiness.py").read_text(encoding="utf-8")


def test_schema_gate_requires_the_above_gap_keys_and_not_the_gap():
    names = sorted(f.name for f in (REPO / "api" / "migrations").glob("*.sql"))
    required = sg.required_keys("cis", _source(), names)
    above = [str(k) for k in readiness.REQUIRED_CIS_ABOVE_GAP]
    assert above and set(above) <= set(required)
    assert set(required) == set(map(str, readiness.REQUIRED_CIS_KEYS))   # the gate and readiness agree
    gap = [n for n in range(readiness.REQUIRED_CIS_SCHEMA + 1, min(readiness.REQUIRED_CIS_ABOVE_GAP))
           if str(n) not in required]
    assert "336" not in required and gap                                # the unapplied gap is never required


def test_schema_gate_holds_the_deploy_until_362_to_364_are_registered():
    required = sg.required_keys("cis", _source(), [])
    below = [str(k) for k in range(readiness.LEGACY_CIS_BASELINE + 1, readiness.REQUIRED_CIS_SCHEMA + 1)]
    above = [str(k) for k in readiness.REQUIRED_CIS_ABOVE_GAP]   # 362, 363, 364, then later raises
    assert above[:3] == ["362", "363", "364"]
    assert sg.evaluate("cis", required, {"registered_keys": below}).missing == above
    assert sg.evaluate("cis", required, {"registered_keys": below + ["362", "363", "364"]}).missing == above[3:]
    assert sg.evaluate("cis", required, {"registered_keys": below + above}).missing == []


def test_preflight_rule_3_sees_an_above_gap_raise():
    """A code PR that raises readiness to a contract file it also adds is refused (MIG-4), above the gap too."""
    literal = "REQUIRED_CIS_ABOVE_GAP = (" + ", ".join(map(str, readiness.REQUIRED_CIS_ABOVE_GAP)) + ")"
    base_src = _source().replace(literal, "REQUIRED_CIS_ABOVE_GAP = ()")
    assert base_src != _source()
    base_req, head_req = sg.readiness_keys("cis", base_src), sg.readiness_keys("cis", _source())
    rep = mp.FileReport("364_x.sql", "364", header="none")
    r = mp.assess("cis", ["362_a.sql", "363_b.sql"], ["362_a.sql", "363_b.sql", "364_x.sql"],
                  {"A": ["364_x.sql"], "M": [], "D": []}, base_req, head_req, {"364_x.sql": rep},
                  {"362_a.sql", "363_b.sql"})
    assert r.raised == [str(k) for k in readiness.REQUIRED_CIS_ABOVE_GAP]
    assert any("keeps the MIG-4 order" in e for e in r.errors)
