"""AA-883 (S241 GĐ2) — api/routers/admin_platform.py: the read-only staff directory.

Covers:
- access: admin reads; reviewer / content / an unknown role / the machine principal (secret, no
  role) / no secret are refused before the DB is touched;
- shape: only the existing shared.admin_users columns, never password_hash (the SQL does not even
  select it), datetimes as ISO strings, counts of active accounts per role;
- detail: a malformed id and an unknown id are 404, a known id returns the one row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.staff_authz import POLICY, STAFF_READ
from api.main import app
from api.routers import admin, admin_platform, admin_settings

_SECRET = "aa883-test-admin-secret"
_ID_A = "11111111-1111-1111-1111-111111111111"
_ID_B = "22222222-2222-2222-2222-222222222222"
_T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 10, 17, 9, 30, tzinfo=timezone.utc)

_ROWS = [
    {"id": _ID_A, "username": "nghiep", "role": "admin", "is_active": True, "created_at": _T0, "updated_at": _T1},
    {"id": _ID_B, "username": "reviewer-1", "role": "reviewer", "is_active": False,
     "created_at": _T0, "updated_at": _T0},
]


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.sql: list[str] = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return self.rows

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        wanted = str(args[0])
        return next((r for r in self.rows if r["id"] == wanted), None)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _client(monkeypatch, pool):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def conn():
    return _Conn(_ROWS)


@pytest.fixture
def client(monkeypatch, conn):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire(conn))
    return _client(monkeypatch, pool)


@pytest.fixture
def no_db_client(monkeypatch):
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=AssertionError("the DB must not be reached"))
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        setattr(pool, name, AsyncMock(side_effect=AssertionError("the DB must not be reached")))
    return _client(monkeypatch, pool)


def _headers(role):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "aa883-test"}
    if role is not None:
        h["x-admin-role"] = role
    return h


# ── Policy ───────────────────────────────────────────────────────────────────────────────────────

def test_both_routes_need_staff_read():
    assert POLICY[("GET", "/admin/platform/staff")] == STAFF_READ
    assert POLICY[("GET", "/admin/platform/staff/{staff_id}")] == STAFF_READ


@pytest.mark.parametrize("path", ["/admin/platform/staff", f"/admin/platform/staff/{_ID_A}"])
@pytest.mark.parametrize("role", ["reviewer", "content", "tenant", "", None])
def test_non_admin_and_machine_are_refused_before_the_db(no_db_client, role, path):
    resp = no_db_client.get(path, headers=_headers(role))
    assert resp.status_code == 403, (role, path, resp.text)


@pytest.mark.parametrize("path", ["/admin/platform/staff", f"/admin/platform/staff/{_ID_A}"])
def test_the_secret_is_checked_first(no_db_client, path):
    anon = no_db_client.get(path, headers={"x-admin-role": "admin"})
    wrong = no_db_client.get(path, headers={"X-Admin-Secret": _SECRET + "x", "x-admin-role": "admin"})
    assert anon.status_code == 403 and wrong.status_code == 403
    assert anon.json()["detail"] == wrong.json()["detail"] == "Invalid admin secret"


def test_machine_detail_names_the_reason(no_db_client):
    resp = no_db_client.get("/admin/platform/staff", headers=_headers(None))
    assert resp.status_code == 403
    assert "machine" in resp.json()["detail"]


def test_reviewer_detail_names_the_capability(no_db_client):
    resp = no_db_client.get("/admin/platform/staff", headers=_headers("reviewer"))
    assert resp.json()["detail"] == "Role 'reviewer' lacks the 'staff.read' permission"


# ── List ─────────────────────────────────────────────────────────────────────────────────────────

def test_admin_lists_staff_with_counts(client):
    resp = client.get("/admin/platform/staff", headers=_headers("admin"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2 and body["active"] == 1
    assert body["active_by_role"] == {"admin": 1, "reviewer": 0, "content": 0, "viewer": 0}   # viewer: AA-878
    first = body["staff"][0]
    assert first == {
        "id": _ID_A, "username": "nghiep", "role": "admin", "is_active": True,
        "created_at": _T0.isoformat(), "updated_at": _T1.isoformat(),
        # AA-816 C6 directory fields (absent from these fake rows): falsy / None, never a KeyError
        "email": None, "display_name": None, "is_owner": False, "last_login_at": None, "linked": False,
        "booking_role": None, "breakglass": False,
        # S242 invitation e-mail fields (from the audit log; never invited -> None)
        "last_invited_at": None, "last_invite_requested_at": None, "last_invite_not_sent_at": None,
        "invite_status": None,
    }
    # A legacy (secret + x-admin-role) session may read but not write (AA-816 C6).
    assert body["viewer"] == {"auth": "legacy", "is_owner": False, "can_write": False, "admin_id": None}


def test_list_never_returns_or_selects_the_password_hash(monkeypatch):
    rows = [{**_ROWS[0], "password_hash": "$2b$12$should-never-leave"}]
    conn = _Conn(rows)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire(conn))
    client = _client(monkeypatch, pool)
    resp = client.get("/admin/platform/staff", headers=_headers("admin"))
    assert resp.status_code == 200
    assert "password" not in resp.text and "should-never-leave" not in resp.text
    assert all("password" not in sql for sql in conn.sql)
    assert "*" not in admin_platform._SELECT


def test_response_keys_are_the_known_columns_only(client):
    body = client.get("/admin/platform/staff", headers=_headers("admin")).json()
    assert all(set(s) == set(admin_platform.STAFF_COLUMNS) for s in body["staff"])


# ── Detail ───────────────────────────────────────────────────────────────────────────────────────

def test_admin_reads_one_account(client):
    resp = client.get(f"/admin/platform/staff/{_ID_B}", headers=_headers("admin"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "reviewer-1"
    assert resp.json()["is_active"] is False


@pytest.mark.parametrize("staff_id", ["not-a-uuid", "33333333-3333-3333-3333-333333333333"])
def test_unknown_or_malformed_id_is_404(client, staff_id):
    resp = client.get(f"/admin/platform/staff/{staff_id}", headers=_headers("admin"))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Staff account not found"
