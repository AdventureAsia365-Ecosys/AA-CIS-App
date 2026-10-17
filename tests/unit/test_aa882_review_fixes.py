"""AA-882 (S241 GĐ3 review fixes) — the open reads that leaked admin-only data to every staff role.

- GET /admin/tenants/{tenant_id}/details: open for the aa_internal catalog (Master Content, every staff
  role); any other tenant needs tenant.read.
- GET /admin/search: the tenant group needs tenant.read and the job group ops.read; tours stay open.
- The notification bell: reviewer/content see, count and clear only content-targeted rows, never a
  platform.* or spend alert; admin and the machine keep the whole list.
- Admin-only GETs (Jev credit, shadow cost report, waves, A4 logs, content quality, atoms, unmapped
  market requests) are in POLICY.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.staff_authz import OPS_READ, POLICY, SPEND_READ, TENANT_READ
from api.main import app
from api.routers import admin, admin_settings
from api.routers.admin import AA_INTERNAL_TENANT, notification_scope

_SECRET = "aa882-review-test-admin-secret"
_OTHER_TENANT = "00000000-0000-0000-0000-0000000000a2"


class _Conn:
    """Records every statement; answers like an empty database."""

    def __init__(self, execute_result="UPDATE 0"):
        self.sql: list[str] = []
        self.execute_result = execute_result

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return []

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        return {"unread": 0}

    async def execute(self, sql, *args):
        self.sql.append(sql)
        return self.execute_result


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _bell_sql(conn) -> list[str]:
    """The statements on shared.notifications (the middleware may write an audit row too)."""
    return [sql for sql in conn.sql if "shared.notifications" in sql]


def _headers(role):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "aa882-review"}
    if role is not None:
        h["x-admin-role"] = role
    return h


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "redis", None, raising=False)


@pytest.fixture
def no_db_client(secret, monkeypatch):
    """A pool that fails the test if the request reaches the database."""
    from fastapi.testclient import TestClient

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=AssertionError("the DB must not be reached"))
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        setattr(pool, name, AsyncMock(side_effect=AssertionError("the DB must not be reached")))
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def recording(secret, monkeypatch):
    """(client, conn): a pool whose connection records the SQL each request runs."""
    from fastapi.testclient import TestClient

    def make(execute_result="UPDATE 0"):
        conn = _Conn(execute_result)
        monkeypatch.setattr(app.state, "pool", _Pool(conn), raising=False)
        return TestClient(app), conn

    return make


# ── GET /admin/tenants/{tenant_id}/details ───────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["reviewer", "content", None])
def test_tenant_details_of_another_tenant_needs_tenant_read(no_db_client, role):
    resp = no_db_client.get(f"/admin/tenants/{_OTHER_TENANT}/details", headers=_headers(role))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == f"Role '{role or 'machine'}' lacks the '{TENANT_READ}' permission"


@pytest.mark.parametrize("role", ["admin", "reviewer", "content"])
def test_tenant_details_of_aa_internal_stays_open(no_db_client, role):
    """Master Content (every staff role) reads the aa_internal catalog: past the check, into the DB."""
    resp = no_db_client.get(f"/admin/tenants/{AA_INTERNAL_TENANT}/details", headers=_headers(role))
    assert resp.status_code == 500  # the endpoint ran and hit the fail-the-test pool


def test_tenant_details_of_another_tenant_is_open_to_admin(no_db_client):
    resp = no_db_client.get(f"/admin/tenants/{_OTHER_TENANT}/details", headers=_headers("admin"))
    assert resp.status_code == 500  # past the check


# ── GET /admin/search ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["reviewer", "content"])
def test_search_hides_tenants_and_jobs_without_the_capability(recording, role):
    client, conn = recording()
    resp = client.get("/admin/search?q=trail", headers=_headers(role))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tours": [], "tenants": [], "jobs": []}
    joined = "\n".join(conn.sql)
    assert "raw_tours" in joined                                      # tours stay open
    assert "shared.tenants" not in joined and "shared.job" not in joined  # never queried


def test_search_runs_every_group_for_admin(recording):
    client, conn = recording()
    assert client.get("/admin/search?q=trail", headers=_headers("admin")).status_code == 200
    joined = "\n".join(conn.sql)
    assert "shared.tenants" in joined and "shared.job" in joined


# ── The notification bell ────────────────────────────────────────────────────────────────────────

def test_notification_scope_per_role():
    assert notification_scope("admin") == "TRUE"
    assert notification_scope(None) == "TRUE"          # the machine (holds the secret)
    for role in ("reviewer", "content", "tenant", ""):
        scope = notification_scope(role)
        assert "NOT LIKE 'platform.%'" in scope
        assert "spend_budget_alert" in scope
        assert "target_roles && ARRAY['content','reviewer']" in scope


@pytest.mark.parametrize("role", ["reviewer", "content"])
def test_reviewer_read_all_leaves_admin_alerts_unread(recording, role):
    client, conn = recording("UPDATE 3")
    resp = client.put("/admin/notifications/read-all", headers=_headers(role))
    assert resp.status_code == 200 and resp.json() == {"cleared": 3}
    bell = _bell_sql(conn)
    assert bell and all(notification_scope(role) in sql for sql in bell)


def test_admin_read_all_clears_everything(recording):
    client, conn = recording("UPDATE 5")
    assert client.put("/admin/notifications/read-all", headers=_headers("admin")).status_code == 200
    (sql,) = _bell_sql(conn)
    assert "platform." not in sql and sql.rstrip().endswith("AND TRUE")


@pytest.mark.parametrize("path", ["/admin/notifications", "/admin/notifications?unread_only=true",
                                  "/admin/notifications/count"])
def test_reviewer_bell_reads_are_scoped(recording, path):
    client, conn = recording()
    assert client.get(path, headers=_headers("reviewer")).status_code == 200
    (sql,) = _bell_sql(conn)
    assert notification_scope("reviewer") in sql


def test_marking_an_out_of_scope_row_read_is_a_404(recording):
    client, conn = recording("UPDATE 0")
    resp = client.put("/admin/notifications/7/read", headers=_headers("content"))
    assert resp.status_code == 404
    (sql,) = _bell_sql(conn)
    assert notification_scope("content") in sql
    client, _ = recording("UPDATE 1")
    assert client.put("/admin/notifications/7/read", headers=_headers("content")).status_code == 200


# ── Admin-only GETs are in POLICY ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,capability", [
    ("/admin/decisions/jev-credit", SPEND_READ),
    ("/admin/llm-shadow/report", SPEND_READ),
    ("/admin/waves", OPS_READ),
    ("/admin/waves/{wave_id}/report", OPS_READ),
    ("/admin/unmapped-market-requests", TENANT_READ),
    ("/admin/a4/content-log", TENANT_READ),
    ("/admin/content-quality/pieces", TENANT_READ),
    ("/admin/atoms", TENANT_READ),
])
def test_admin_only_reads_are_gated(path, capability):
    assert POLICY[("GET", path)] == capability


@pytest.mark.parametrize("path", ["/admin/decisions/jev-credit", "/admin/llm-shadow/report",
                                  "/admin/waves", "/admin/a4/content-log",
                                  "/admin/content-quality/stats", "/admin/atoms/summary"])
def test_reviewer_is_refused_the_admin_only_reads(no_db_client, path):
    resp = no_db_client.get(path, headers=_headers("reviewer"))
    assert resp.status_code == 403, (path, resp.text)
    assert resp.json()["detail"].startswith("Role 'reviewer' lacks the ")
