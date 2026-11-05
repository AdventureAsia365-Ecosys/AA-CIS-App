"""AA-1035 (R2 slice 1, root ADR 0019 d4) — api/core/tenant_scope.py without a database.

Covers who the staff member is (verified identity, the main secret's x-admin-user-id, nothing else), the decision per
scope kind with a fake connection, the Tenants directory filter, the PATCH /admin/tenants/{id} field rule for finance,
and the dependency through the real app (a refusal never reaches the endpoint).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.core import tenant_scope as ts
from api.routers import admin

SECRET = f"aa1035-unit-{uuid.uuid4().hex}"   # a fresh value per run
STAFF = "11111111-1111-4111-8111-111111111111"
MINE = "22222222-2222-4222-8222-222222222222"
POOLED = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"


class _Conn:
    """Answers the scope SQL from a book (staff -> tenants) and a pool set; anything else fails the test."""

    def __init__(self, book=None, pool=()):
        self.book = book or {STAFF: {MINE}}
        self.pool = set(pool)
        self.sql: list[str] = []

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        if sql == ts.SQL_IN_BOOK:
            return args[1] in self.book.get(args[0], set())
        if sql == ts.SQL_IN_POOL:
            return args[0] in self.pool
        raise AssertionError(f"unexpected SQL {sql}")

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if sql == ts.SQL_BOOK:
            return [{"tenant_id": t} for t in sorted(self.book.get(args[0], set()))]
        if sql == ts.SQL_POOL:
            return [{"tenant_id": t} for t in sorted(self.pool)]
        raise AssertionError(f"unexpected SQL {sql}")


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def _request(role=None, tenant=MINE, user=STAFF, secret=SECRET, identity=None, conn=None):
    headers = {}
    if secret:
        headers["x-admin-secret"] = secret
    if role is not None:
        headers["x-admin-role"] = role
    if user:
        headers["x-admin-user-id"] = user
    state = {"staff_identity": identity} if identity is not None else {}
    app = SimpleNamespace(state=SimpleNamespace(pool=_Pool(conn or _Conn())))
    return SimpleNamespace(headers=headers, path_params={"tenant_id": tenant}, scope={"state": state},
                           method="GET", app=app)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    from api.core import staff_credential

    monkeypatch.setattr(staff_credential, "principal_verified", lambda: False)


async def _check(**kw):
    try:
        await ts.require_tenant_scope(_request(**kw))
        return None
    except HTTPException as exc:
        return (exc.status_code, exc.detail)


# ── scope per role ───────────────────────────────────────────────────────────────────────────────────────────────

def test_every_staff_role_has_a_scope_kind():
    from api.core.staff_authz import ROLE_PERMS

    assert set(ts.TENANT_SCOPE) == set(ROLE_PERMS)
    assert ts.scope_for_role("admin") == ts.SCOPE_ALL
    assert ts.scope_for_role("customer_success") == ts.SCOPE_BOOK
    assert ts.scope_for_role("support") == ts.SCOPE_BOOK_POOL
    assert ts.scope_for_role("viewer") == ts.SCOPE_INTERNAL
    assert ts.scope_for_role("tenant") is None and ts.scope_for_role(None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "finance", "ai_ops", "content_lead", "tenant_qa"])
async def test_all_scope_roles_never_touch_the_database(role):
    conn = _Conn()
    assert await _check(role=role, tenant=OTHER, conn=conn) is None
    assert conn.sql == []


@pytest.mark.asyncio
async def test_customer_success_reaches_its_book_only():
    assert await _check(role="customer_success", tenant=MINE) is None
    assert await _check(role="customer_success", tenant=OTHER) == (403, ts.OUT_OF_SCOPE)
    assert await _check(role="customer_success", tenant=POOLED, conn=_Conn(pool={POOLED})) == (403, ts.OUT_OF_SCOPE)


@pytest.mark.asyncio
async def test_support_reaches_its_book_and_the_pool():
    conn = _Conn(pool={POOLED})
    assert await _check(role="support", tenant=MINE, conn=conn) is None
    assert await _check(role="support", tenant=POOLED, conn=conn) is None
    assert await _check(role="support", tenant=OTHER, conn=conn) == (403, ts.OUT_OF_SCOPE)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["content", "viewer"])
async def test_content_roles_reach_the_aa_internal_tenant_only(role):
    assert await _check(role=role, tenant=ts.AA_INTERNAL_TENANT) is None
    assert await _check(role=role, tenant=MINE) == (403, ts.OUT_OF_SCOPE)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["", "tenant", "Admin"])
async def test_an_unknown_role_reaches_no_tenant(role):
    assert await _check(role=role, tenant=ts.AA_INTERNAL_TENANT) == (403, ts.OUT_OF_SCOPE)


@pytest.mark.asyncio
async def test_a_malformed_tenant_id_is_refused_for_a_scoped_role():
    assert await _check(role="customer_success", tenant="not-a-uuid") == (403, ts.OUT_OF_SCOPE)


@pytest.mark.asyncio
async def test_the_machine_and_non_staff_calls_are_left_to_their_guards():
    assert await _check(role=None, tenant=OTHER) is None                 # machine: POLICY decides
    assert await _check(role="customer_success", secret=None) is None    # no staff credential: router's guard
    assert await _check(role="customer_success", secret="wrong") is None


# ── who the staff member is ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_verified_identity_wins_over_the_header():
    identity = SimpleNamespace(auth="cognito", admin_id=STAFF)
    assert await _check(role="customer_success", user=OTHER, identity=identity) is None
    other = SimpleNamespace(auth="cognito", admin_id=OTHER)
    assert await _check(role="customer_success", user=STAFF, identity=other) == (403, ts.OUT_OF_SCOPE)


@pytest.mark.asyncio
async def test_without_the_main_secret_the_header_id_is_not_trusted(monkeypatch):
    from api.core import staff_credential

    # e.g. the Preview credential or a Cognito Bearer without a resolved row: verified, but no main secret
    monkeypatch.setattr(staff_credential, "principal_verified", lambda: True)
    assert await _check(role="customer_success", secret="preview-credential") == (403, ts.NO_STAFF_ID)
    assert await _check(role="customer_success", user=None) == (403, ts.NO_STAFF_ID)
    assert await _check(role="customer_success", user="not-a-uuid") == (403, ts.NO_STAFF_ID)


@pytest.mark.asyncio
async def test_a_missing_table_is_503_not_open(monkeypatch):
    import asyncpg

    conn = _Conn()
    conn.fetchval = AsyncMock(side_effect=asyncpg.UndefinedTableError("staff_tenant_assignment"))
    assert await _check(role="customer_success", conn=conn) == (503, ts.SCOPE_UNAVAILABLE)


# ── the Tenants directory filter ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_directory_filter_per_role():
    conn = _Conn(pool={POOLED})

    async def flt(**kw):
        return await ts.tenant_scope_filter(_request(**kw), conn)

    assert await flt(role="admin") is None and await flt(role="finance") is None and await flt(role=None) is None
    assert await flt(role="customer_success") == {MINE}
    assert await flt(role="support") == {MINE, POOLED}
    assert await flt(role="content") == {ts.AA_INTERNAL_TENANT}
    assert await flt(role="customer_success", user=None) == frozenset()
    assert await flt(role="tenant") == frozenset()


# ── PATCH /admin/tenants/{id}: billing fields only for finance ───────────────────────────────────────────────────

@pytest.mark.parametrize("role,fields,ok", [
    ("admin", {"plan_tier", "rate_limit_rpm", "daily_publish_limit", "is_active"}, True),
    ("finance", {"plan_tier"}, True),
    ("finance", {"is_active"}, True),
    ("finance", {"plan_tier", "is_active"}, True),
    ("finance", {"rate_limit_rpm"}, False),
    ("finance", {"plan_tier", "daily_publish_limit"}, False),
    ("customer_success", {"is_active"}, False),
    ("content", {"plan_tier"}, False),
    (None, {"plan_tier"}, False),
])
def test_tenant_patch_fields(role, fields, ok):
    if ok:
        admin._require_tenant_patch(role, fields)
    else:
        with pytest.raises(HTTPException) as exc:
            admin._require_tenant_patch(role, fields)
        assert exc.value.status_code == 403


# ── through the real app ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app

    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    conn = _Conn()
    # the scope queries answer; anything the endpoint runs afterwards marks "reached" (500)
    pool = _Pool(conn)
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        setattr(pool, name, AsyncMock(side_effect=AssertionError("endpoint reached")))
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=False), conn


def _headers(role, user=STAFF):
    return {"X-Admin-Secret": SECRET, "x-admin-role": role, "x-admin-user-id": user}


@pytest.mark.parametrize("path", ["/admin/tenants/{t}/usage", "/admin/tenants/{t}/members",
                                  "/admin/ai-gateway/tenant-budgets/{t}", "/admin/tenants/{t}/assignments"])
def test_app_refuses_another_tenant_before_the_endpoint(client, path):
    c, conn = client
    res = c.get(path.format(t=OTHER), headers=_headers("customer_success"))
    assert res.status_code == 403 and res.json()["detail"] == ts.OUT_OF_SCOPE, res.text
    assert conn.sql == [ts.SQL_IN_BOOK]           # only the scope lookup ran


def test_app_lets_the_book_through_to_the_endpoint(client):
    c, conn = client
    res = c.get(f"/admin/tenants/{MINE}/members", headers=_headers("customer_success"))
    assert res.status_code != 403, res.text       # past both checks; the fake pool stops the endpoint


def test_finance_patch_of_a_non_billing_field_is_refused_in_the_route(client):
    c, _ = client
    res = c.patch(f"/admin/tenants/{OTHER}", json={"rate_limit_rpm": 30}, headers=_headers("finance"))
    assert res.status_code == 403 and "plan_tier, is_active" in res.json()["detail"]


def test_scope_helpers_use_the_request_identity_helper():
    """The identity comes from staff_authz.request_staff_identity (the bearer middleware's scope state)."""
    req = _request(role="customer_success", identity=SimpleNamespace(auth="legacy", admin_id=OTHER))
    assert ts.staff_id_of(req) == STAFF        # a non-Cognito identity is ignored; the main secret's header counts
    req2 = _request(role="customer_success", secret=None, identity=None)
    assert ts.staff_id_of(req2) is None


def test_principal_of_reads_the_role_header():
    p = ts.principal_of(_request(role="support"))
    assert p.role == "support" and not p.is_machine
    assert ts.principal_of(_request(role=None)).is_machine


def test_tenant_scoped_dependency_list_shape():
    assert isinstance(ts.TENANT_SCOPED, list) and len(ts.TENANT_SCOPED) == 1
