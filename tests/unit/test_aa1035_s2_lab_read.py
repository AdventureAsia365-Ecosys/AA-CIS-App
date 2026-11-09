"""AA-1035 R2 slice 2 — the logged Writing Lab read of tenant text (ai_ops) and the staff bell of every role.

Root ADR 0019, settled by the tech lead (S265) item 1: "ai_ops may read tenant text in Writing Lab: permission
tenant_content.read, Lab pages only, every view logged." Pinned here:
- lab.tenant_text.read is held by ai_ops only and appears ONLY on GETs under /admin/writing-memory/ (never Tenant QA,
  Content Trace, the A4 logs, Tenant 360 or the per-tenant voice of Tenant 360);
- every route carrying it declares LAB_TEXT_LOGGED, and the dependency writes one audit row for ai_ops BEFORE the read
  (a failed write refuses the read, 503); admin / content_lead / tenant_qa read in their own right, unlogged as before.
- the bell (notifications.read): every role; a tenant desk role sees only rows targeted at its role, and (S268 fix
  round 2) of a tenant row only those of a tenant its scope reaches (book; support also the pool; finance all).
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from api.core import lab_read_log, staff_authz
from api.core.staff_authz import LAB_TENANT_TEXT_READ, POLICY, ROLE_PERMS, policy_capabilities
from api.routers.admin import notification_scope
from tests.unit.test_s241_admin_routes_require_auth import _served_routes, _walk

SECRET = f"aa1035-s2-lab-{uuid.uuid4().hex}"
STAFF = "11111111-1111-4111-8111-111111111111"


def _lab_text_keys() -> set[tuple[str, str]]:
    return {k for k, v in POLICY.items() if LAB_TENANT_TEXT_READ in policy_capabilities(v)}


def test_only_ai_ops_holds_the_lab_text_read():
    assert [r for r, perms in ROLE_PERMS.items() if LAB_TENANT_TEXT_READ in perms] == ["ai_ops"]
    assert "tenant_content.read" not in ROLE_PERMS["ai_ops"]


def test_the_lab_text_read_opens_lab_gets_only():
    keys = _lab_text_keys()
    assert keys and all(m == "GET" and p.startswith("/admin/writing-memory/") for m, p in keys), keys
    assert ("GET", "/admin/writing-memory/tenants/{tenant_id}/voice") not in keys   # Tenant 360, not a Lab page


def test_every_lab_text_route_declares_the_logged_read():
    from api.main import app

    seen = set()
    for path, route, _ in _walk(_served_routes(app)):
        if not isinstance(route, APIRoute):
            continue
        for m in route.methods - {"HEAD", "OPTIONS"}:
            if (m, path) in _lab_text_keys():
                assert any(d.call is lab_read_log.log_lab_tenant_text_read for d in route.dependant.dependencies), path
                seen.add((m, path))
    assert seen == _lab_text_keys()


@pytest.mark.parametrize("prefix", ["/admin/content-quality", "/admin/a4", "/admin/tenant-qa", "/admin/tenants/"])
def test_ai_ops_still_never_reaches_other_tenant_text(prefix):
    p = staff_authz.principal_from_role("ai_ops")
    for (m, path) in POLICY:
        if path.startswith(prefix) and m == "GET" and "tenant_content.read" in policy_capabilities(POLICY[(m, path)]):
            assert staff_authz.denial_reason(p, m, path) is not None, path


class _Pool:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[tuple] = []
        self.fail = fail

    async def execute(self, sql: str, *args: Any) -> str:
        assert "acp_shared.audit_log" in sql
        if self.fail:
            raise OSError("audit store down")
        self.rows.append(args)
        return "INSERT 0 1"


def _request(role: str, pool: _Pool, *, path: str = "/admin/writing-memory/signals", query: str = "limit=200"):
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": path, "query_string": query.encode(),
        "headers": [(b"x-admin-secret", SECRET.encode()), (b"x-admin-role", role.encode()),
                    (b"x-admin-user-id", STAFF.encode())],
        "app": SimpleNamespace(state=SimpleNamespace(pool=pool)), "path_params": {},
        "route": SimpleNamespace(path=path),
    }
    return Request(scope)


@pytest.fixture
def secret(monkeypatch):
    from api.core import staff_credential
    from api.routers import admin

    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    monkeypatch.setattr(staff_credential, "principal_verified", lambda: False)


def test_an_ai_ops_read_is_logged_before_it_runs(secret):
    pool = _Pool()
    asyncio.run(lab_read_log.log_lab_tenant_text_read(_request("ai_ops", pool, query="tenant_id=" + STAFF)))
    (row,) = pool.rows
    # tenant, actor, action, resource_type, resource_id, details, actor_type, http_method, route, status
    assert row[1] == f"admin:{STAFF}" and row[2] == "lab.tenant_text_read" and row[3] == "http_route"
    assert row[4] == "/admin/writing-memory/signals" and row[6] == "staff" and row[7] is None
    assert row[8] == "/admin/writing-memory/signals" and row[0] == STAFF
    assert '"role": "ai_ops"' in row[5] and '"query_keys": ["tenant_id"]' in row[5]


def test_an_unlogged_read_is_refused(secret):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(lab_read_log.log_lab_tenant_text_read(_request("ai_ops", _Pool(fail=True))))
    assert exc.value.status_code == 503


@pytest.mark.parametrize("role", ["admin", "content_lead", "tenant_qa"])
def test_roles_with_tenant_text_in_their_own_right_are_not_logged_here(secret, role):
    pool = _Pool()
    asyncio.run(lab_read_log.log_lab_tenant_text_read(_request(role, pool)))
    assert pool.rows == []


def test_the_machine_and_a_call_without_staff_credential_are_not_logged(secret):
    from starlette.requests import Request

    pool = _Pool()
    no_cred = Request({"type": "http", "method": "GET", "path": "/admin/writing-memory/signals", "query_string": b"",
                       "headers": [], "app": SimpleNamespace(state=SimpleNamespace(pool=pool))})
    asyncio.run(lab_read_log.log_lab_tenant_text_read(no_cred))
    machine = Request({"type": "http", "method": "GET", "path": "/admin/writing-memory/signals", "query_string": b"",
                       "headers": [(b"x-admin-secret", SECRET.encode())],
                       "app": SimpleNamespace(state=SimpleNamespace(pool=pool))})
    asyncio.run(lab_read_log.log_lab_tenant_text_read(machine))
    assert pool.rows == []


# ── the bell ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_role_holds_the_bell():
    assert all(staff_authz.NOTIFICATIONS_READ in perms for perms in ROLE_PERMS.values())


@pytest.mark.parametrize("role,expected", [
    (None, "NOT (target_roles <@ ARRAY['tenant']::text[])"),
    ("admin", "NOT (target_roles <@ ARRAY['tenant']::text[])"),
    ("content", "ARRAY['content','reviewer']"), ("viewer", "ARRAY['content','reviewer']"),
    ("ai_ops", "ARRAY['content','reviewer']"),
    ("customer_success", "ARRAY['customer_success']"), ("support", "ARRAY['support']"),
    ("finance", "ARRAY['finance']"),
    ("nobody", "FALSE"), ("support'] OR true --", "FALSE"),
])
def test_notification_scope_per_role(role, expected):
    scope = notification_scope(role)
    assert expected in scope
    if role in ("customer_success", "support", "finance"):
        assert "platform.%" in scope and "spend_budget_alert" in scope   # never an admin alert


# ── the bell of a desk role is tenant scoped (S268 fix round 2) ───────────────────────────────────────────────────

def test_a_book_role_bell_only_reaches_its_book():
    from api.core.tenant_scope import AA_INTERNAL_TENANT

    scope = notification_scope("customer_success", STAFF)
    assert "ARRAY['customer_success']" in scope
    assert f"'{AA_INTERNAL_TENANT}'::uuid" in scope                        # platform rows stay role-targeted
    assert f"sa.admin_user_id = '{STAFF}'::uuid AND sa.tenant_id = notifications.tenant_id" in scope
    assert "st.kind IN ('agency', 'demo')" not in scope                    # no pool for customer_success


def test_support_bell_reaches_its_book_and_the_pool():
    scope = notification_scope("support", STAFF)
    assert f"sa.admin_user_id = '{STAFF}'::uuid" in scope and "st.kind IN ('agency', 'demo')" in scope


def test_finance_bell_reaches_every_tenant():
    assert notification_scope("finance", STAFF).endswith("OR TRUE))")


@pytest.mark.parametrize("staff", [None, "", "not-a-uuid", "x'::uuid OR TRUE --"])
def test_a_desk_role_without_a_valid_staff_id_sees_no_tenant_row(staff):
    scope = notification_scope("support", staff)
    assert scope.endswith("OR FALSE))") and "--" not in scope and "OR TRUE" not in scope


def test_tenant_scope_sql_takes_identifiers_only():
    from api.core.tenant_scope import tenant_scope_sql

    with pytest.raises(ValueError):
        tenant_scope_sql("book", STAFF, "tenant_id; DROP TABLE x")
    assert tenant_scope_sql("internal", None, "n.tenant_id").startswith("(n.tenant_id = ")
    assert tenant_scope_sql(None, STAFF, "tenant_id") == "FALSE"


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/notifications"), ("GET", "/admin/notifications/count"),
    ("PUT", "/admin/notifications/read-all"), ("PUT", "/admin/notifications/7/read"),
])
def test_every_bell_route_applies_the_callers_tenant_scope(secret, monkeypatch, method, path):
    """The four bell routes build their WHERE from the caller's role AND staff id (x-admin-user-id under the main
    secret; the verified Cognito id otherwise): a support member's list, count and marks never reach a row of a
    tenant outside its book and the pool."""
    from fastapi.testclient import TestClient

    from api.main import app

    seen: list[str] = []

    class _Db:
        def acquire(self):
            @asynccontextmanager
            async def ctx():
                yield self
            return ctx()

        async def fetch(self, sql, *a):
            seen.append(sql)
            return []

        async def fetchrow(self, sql, *a):
            seen.append(sql)
            return {"unread": 0}

        async def execute(self, sql, *a):
            if "shared.tenant_api_usage" in sql:
                return "INSERT 0 1"
            seen.append(sql)
            return "UPDATE 1"

    monkeypatch.setattr(app.state, "pool", _Db(), raising=False)
    res = TestClient(app, raise_server_exceptions=False).request(
        method, path, headers={"X-Admin-Secret": SECRET, "x-admin-role": "support", "x-admin-user-id": STAFF})
    assert res.status_code == 200, res.text
    (sql,) = [q for q in seen if "shared.notifications" in q]
    assert f"sa.admin_user_id = '{STAFF}'::uuid" in sql and "notifications.tenant_id" in sql
