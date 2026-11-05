"""AA-1035 R2 close-out (S269) — through the real app with a fake pool:

- the reviewer alias is gone (CIS 375): no role, no group, no directory value; the retired group is still removed;
- Tenant QA of the own book for customer_success (matrix "Tenant QA R (own)"): lists and stats filtered to the book,
  a piece / version of another tenant is a 404 before its full read runs; labels stay quality.write;
- the CRM L1 console relay for the desk roles: reads of the book only (the list filtered in CIS too), never a write,
  support never the account reads;
- a send again of the Dev-only E2E admin always names an address (never the CRM's kept one).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.core import staff_authz, staff_identity
from api.core import tenant_scope as ts
from api.routers import admin
from api.routers import admin_content_quality as cq
from api.services import crm_console
from api.services import staff_directory as sd
from services import staff_reconcile

SECRET = f"aa1035-r2c-{uuid.uuid4().hex}"   # a fresh value per run
STAFF = "1035d000-0000-4000-8000-000000000001"
MINE = "1035d000-0000-4000-8000-0000000000a1"
POOLED = "1035d000-0000-4000-8000-0000000000b2"
OTHER = "1035d000-0000-4000-8000-0000000000c3"
PIECE_MINE = "1035e000-0000-4000-8000-000000000001"
PIECE_OTHER = "1035e000-0000-4000-8000-000000000002"


# ── the reviewer alias is gone ───────────────────────────────────────────────────────────────────────────────────

def test_reviewer_is_no_role_and_cis_reviewer_no_group_role():
    assert "reviewer" not in staff_authz.ROLE_PERMS and "reviewer" not in ts.TENANT_SCOPE
    assert staff_identity.role_from_groups(["cis-reviewer"]) is None
    assert staff_identity.role_from_groups(["cis-reviewer", "cis-content"]) == "content"
    assert all(group != "cis-reviewer" for group, _ in staff_identity.GROUP_ROLES)
    assert not hasattr(staff_identity, "canonical_role") and not hasattr(staff_identity, "ROLE_ALIASES")


def test_the_directory_never_writes_reviewer_but_still_removes_its_group():
    with pytest.raises(sd.StaffDirectoryError):
        sd.require_cis_role("reviewer")
    assert sd.RETIRED_CIS_GROUPS == ("cis-reviewer",) and "cis-reviewer" in sd.CIS_GROUPS
    assert sd.E2E_TEST_STAFF_ROLE == "content"
    sd.require_e2e_test_scope(sd.E2E_TEST_STAFF_EMAIL, "content", None)
    with pytest.raises(sd.StaffDirectoryError):
        sd.require_e2e_test_scope(sd.E2E_TEST_STAFF_EMAIL, "reviewer", None)


def test_the_reconcile_knows_the_a10_account_as_content():
    known = staff_reconcile.KNOWN_E2E_STAFF[sd.E2E_TEST_STAFF_EMAIL]
    assert known.groups == frozenset({"cis-content"}) and known.directory_role == "content"
    assert "cis-reviewer" not in staff_reconcile.CIS_GROUPS


# ── the app with a fake pool ─────────────────────────────────────────────────────────────────────────────────────

class FakeDb:
    """Pool and connection at once: the scope SQL (STAFF's book = {MINE}, pool = {POOLED}), the Tenant QA reads
    (recorded, answered empty or with a tenant id) and audit inserts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def acquire(self, *a: Any, **k: Any):
        db = self

        @asynccontextmanager
        async def ctx():
            yield db

        return ctx()

    @asynccontextmanager
    async def transaction(self, *a: Any, **k: Any):
        yield self

    def ran(self, needle: str) -> list[tuple[str, tuple]]:
        return [(s, a) for s, a in self.calls if needle in s]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        if sql == ts.SQL_IN_BOOK:
            return args[0] == STAFF and args[1] == MINE
        if sql == ts.SQL_IN_POOL:
            return args[0] == POOLED
        if sql == cq._PIECE_TENANT_SQL:
            return {PIECE_MINE: uuid.UUID(MINE), PIECE_OTHER: uuid.UUID(OTHER)}.get(args[0])
        if sql == cq._VERSION_TENANT_SQL:
            return {PIECE_MINE: uuid.UUID(MINE), PIECE_OTHER: uuid.UUID(OTHER)}.get(args[0])
        if "to_regclass" in sql:
            return False
        if sql.lstrip().startswith("SELECT count(*)"):
            return 0
        raise AssertionError(f"unexpected fetchval {sql}")

    async def fetch(self, sql: str, *args: Any) -> list:
        self.calls.append((sql, args))
        if sql == ts.SQL_BOOK:
            return [{"tenant_id": MINE}] if args[0] == STAFF else []
        if sql == ts.SQL_POOL:
            return [{"tenant_id": POOLED}]
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"


@pytest.fixture
def app_client(monkeypatch):
    from api.core import staff_credential
    from api.main import app

    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    monkeypatch.setattr(staff_credential, "principal_verified", lambda: False)
    db = FakeDb()
    monkeypatch.setattr(app.state, "pool", db, raising=False)
    relays: list[tuple[str, str, Any]] = []

    async def fake_relay(method, path, authorization, *, json=None, params=None):
        relays.append((method, path, json))
        if method == "GET" and path == "/tenants":
            return 200, {"items": [{"tenant_id": t} for t in (MINE, POOLED, OTHER)], "next_cursor": None}
        return 200, {"tenant_id": path.split("/")[2] if path.startswith("/tenants/") else None}

    monkeypatch.setattr(crm_console, "crm_ops_call", fake_relay)
    return TestClient(app, raise_server_exceptions=False), db, relays


def _h(role: str) -> dict:
    return {"X-Admin-Secret": SECRET, "x-admin-role": role, "x-admin-user-id": STAFF}


# ── Tenant QA of the own book ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,needle", [
    ("/admin/content-quality/pieces", "cp.tenant_id = ANY("),
    ("/admin/content-quality/tour-versions", "ttv.tenant_id = ANY("),
    ("/admin/content-quality/stats", "cp.tenant_id = ANY("),
])
def test_customer_success_reads_the_lists_of_its_book_only(app_client, path, needle):
    c, db, _ = app_client
    res = c.get(path, headers=_h("customer_success"))
    assert res.status_code == 200, res.text
    scoped = db.ran(needle)
    assert scoped, "the read must be filtered to the book"
    assert all([MINE] in [list(a) for a in args if isinstance(a, list)] for _, args in scoped)


@pytest.mark.parametrize("role", ["admin", "content_lead", "tenant_qa"])
def test_the_tenant_content_roles_read_every_tenant(app_client, role):
    c, db, _ = app_client
    assert c.get("/admin/content-quality/pieces", headers=_h(role)).status_code == 200
    assert db.ran("= ANY(") == [] and db.ran(ts.SQL_BOOK) == []


@pytest.mark.parametrize("path,detail_sql", [
    (f"/admin/content-quality/pieces/{PIECE_OTHER}", cq._PIECE_DETAIL_SQL),
    (f"/admin/content-quality/tour-versions/{PIECE_OTHER}", cq._VERSION_DETAIL_SQL),
])
def test_another_tenants_piece_is_a_404_before_its_read(app_client, path, detail_sql):
    c, db, _ = app_client
    res = c.get(path, headers=_h("customer_success"))
    assert res.status_code == 404
    assert db.ran(detail_sql) == []


@pytest.mark.parametrize("path,detail_sql", [
    (f"/admin/content-quality/pieces/{PIECE_MINE}", cq._PIECE_DETAIL_SQL),
    (f"/admin/content-quality/tour-versions/{PIECE_MINE}", cq._VERSION_DETAIL_SQL),
])
def test_a_piece_of_the_book_reaches_its_read(app_client, path, detail_sql):
    c, db, _ = app_client
    c.get(path, headers=_h("customer_success"))   # the fake row is missing: 404 from the read itself
    assert db.ran(detail_sql) != []


@pytest.mark.parametrize("path", [
    f"/admin/content-quality/pieces/{PIECE_MINE}/label", "/admin/content-quality/place-cards",
    "/admin/tenant-qa/compliance", "/admin/content-quality/fallback-writes",
])
def test_customer_success_reads_no_label_card_or_compliance(app_client, path):
    c, _, _ = app_client
    assert c.get(path, headers=_h("customer_success")).status_code == 403


@pytest.mark.parametrize("role", ["support", "finance", "content", "viewer"])
def test_other_roles_still_get_no_tenant_qa(app_client, role):
    c, db, _ = app_client
    assert c.get("/admin/content-quality/pieces", headers=_h(role)).status_code == 403
    assert db.ran("acp_shared.content_piece") == []   # only the usage meter row of the refusal


# ── the CRM L1 console relay ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,expected", [
    ("customer_success", [MINE]), ("support", [MINE, POOLED]), ("admin", [MINE, POOLED, OTHER]),
])
def test_the_agency_list_is_the_callers_book(app_client, role, expected):
    c, _, relays = app_client
    res = c.get("/admin/platform/crm/agencies", headers=_h(role))
    assert res.status_code == 200, res.text
    assert [i["tenant_id"] for i in res.json()["items"]] == expected
    assert relays == [("GET", "/tenants", None)]


@pytest.mark.parametrize("role,tenant,status", [
    ("customer_success", MINE, 200), ("customer_success", POOLED, 403), ("customer_success", OTHER, 403),
    ("support", POOLED, 200), ("support", OTHER, 403),
])
def test_one_agency_is_scoped(app_client, role, tenant, status):
    c, _, relays = app_client
    res = c.get(f"/admin/platform/crm/agencies/{tenant}/members", headers=_h(role))
    assert res.status_code == status
    assert (relays != []) is (status == 200)


def test_support_never_reads_usage_or_entitlements(app_client):
    c, _, relays = app_client
    for leaf in ("usage", "entitlements"):
        assert c.get(f"/admin/platform/crm/agencies/{MINE}/{leaf}", headers=_h("support")).status_code == 403
        assert c.get(f"/admin/platform/crm/agencies/{MINE}/{leaf}",
                     headers=_h("customer_success")).status_code == 200
    assert all(r[0] == "GET" for r in relays) and len(relays) == 2


@pytest.mark.parametrize("role", ["customer_success", "support"])
def test_the_desk_roles_never_write_through_the_console(app_client, role):
    c, _, relays = app_client
    assert c.post(f"/admin/platform/crm/agencies/{MINE}/lock", headers=_h(role),
                  json={"reason_code": "billing", "ticket_ref": "AA-1035"}).status_code == 403
    assert c.post("/admin/platform/crm/agencies", headers=_h(role), json={}).status_code == 403
    assert relays == []


@pytest.mark.parametrize("role", ["finance", "content", "ai_ops", "tenant_qa", "viewer"])
def test_the_console_stays_closed_to_every_other_role(app_client, role):
    c, _, relays = app_client
    assert c.get("/admin/platform/crm/agencies", headers=_h(role)).status_code == 403
    assert c.get("/admin/platform/crm/console", headers=_h(role)).status_code == 403
    assert relays == []
