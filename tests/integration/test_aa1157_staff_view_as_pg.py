"""AA-1157 on real Postgres (CIS 470 table shape, guard trigger, audit chain): an owner's read-only "view as a lower
staff role".

- the owner starts a view on content: one session row (no tenant, view_role), one staff.view_as_started row whose
  details hold the target role's full capability list; reads run as the view role; a write is refused 403 and logged
  as a staff.request row naming the session; Back (the end route, as the owner) ends it with one staff.view_as_ended
  row and the token stops working (409);
- a new start ends the caller's open sessions of either kind, each with its own kind's ended row; the tenant session
  list never shows a role session;
- signing out / disabling the member closes its role session with one staff.view_as_ended row;
- the owner's read of an expired role session closes it with one staff.view_as_ended row (cause expired); two
  concurrent starts (two tabs) leave exactly one open session (the per-member advisory lock).

The Cognito verification is stubbed (staff_identity.authenticate returns the owner's identity for a fixed token); the
rest (bearer middleware, view-as resolve, gate, audit middleware, routes, SQL, triggers) is the real code on the
replayed schema (conftest applies every migration, 470 included).
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from _audit_cleanup import delete_audit_rows  # noqa: E402

DB_HOST = os.environ.get("CIS_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("CIS_TEST_DB_PORT", "5432"))
DB_NAME = os.environ.get("CIS_TEST_DB_NAME", "cis_integration_test")
DB_USER = os.environ.get("CIS_TEST_DB_USER", "cistest")
DB_PASS = os.environ.get("CIS_TEST_DB_PASS", "cistest")
DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = f"aa1157-it-{uuid.uuid4().hex}"
FAKE_TOKEN = "aa1157-cognito-access-token-stand-in"
REASON = "Check what this role sees"


@pytest_asyncio.fixture
async def world(monkeypatch):
    from api.core import staff_identity
    from api.core.staff_authz import ROLE_PERMS
    from api.main import app
    from api.routers import admin

    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    owner = str(uuid.uuid4())
    tag = owner[:8]
    await pool.execute(
        "INSERT INTO shared.admin_users (id, username, role, is_active, email, display_name, is_owner) "
        "VALUES ($1, $2, 'admin', true, $3, 'Owner Example', true)",
        uuid.UUID(owner), f"aa1157-{tag}", f"aa1157-{tag}@adventure.asia")
    tenant = str(uuid.uuid4())
    await pool.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week) "
                       "VALUES ($1, 'Fernwood Tours', $2, 'starter', 3)", uuid.UUID(tenant), f"aa1157-{tag}")
    identity = staff_identity.StaffIdentity(
        admin_id=owner, username=f"aa1157-{tag}", role="admin", db_role="admin", email=f"aa1157-{tag}@adventure.asia",
        display_name="Owner Example", iss="https://issuer.example", sub=f"sub-{tag}", client_id="cis-admin",
        is_owner=True, capabilities=frozenset(ROLE_PERMS["admin"]))

    async def authenticate(_pool, token, **_kw):
        assert token == FAKE_TOKEN
        return identity

    real_kind = staff_identity.token_kind
    monkeypatch.setattr(staff_identity, "authenticate", authenticate)
    monkeypatch.setattr(staff_identity, "token_kind",
                        lambda t: staff_identity.AUTH_COGNITO if t == FAKE_TOKEN else real_kind(t))
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, pool, owner, tenant
    await delete_audit_rows(pool, "actor = $1 OR details->>'session_staff' = $2 OR actor LIKE 'system:staff_view_as%'",
                            f"admin:{owner}", owner)
    # tenant rows go with the tenant (guard: a deleted tenant's rows); role rows are kept 24 months by the guard, so the
    # test removes them with the triggers skipped (test-only, like the audit cleanup)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL session_replication_role = replica")
        await conn.execute("DELETE FROM shared.staff_view_as_session WHERE admin_user_id = $1::uuid", owner)
        await conn.execute("DELETE FROM shared.tenants WHERE tenant_id = $1::uuid", tenant)
        await conn.execute("DELETE FROM shared.admin_users WHERE id = $1::uuid", owner)
    await pool.close()


def _owner() -> dict:
    return {"Authorization": f"Bearer {FAKE_TOKEN}"}


async def _start(client, role="content", minutes=15):
    r = await client.post("/admin/view-as/roles", headers={**_owner(), "x-view-as-client": "bff"},
                          json={"role": role, "reason": REASON, "minutes": minutes})
    assert r.status_code == 201, r.text
    return r.json()


async def _audit(pool, action, resource_id):
    rows = await pool.fetch("SELECT tenant_id, actor, status, details FROM acp_shared.audit_log "
                            "WHERE action = $1 AND resource_id = $2 ORDER BY chain_seq NULLS LAST, created_at",
                            action, resource_id)
    return [dict(r, details=json.loads(r["details"]) if isinstance(r["details"], str) else r["details"]) for r in rows]


@pytest.mark.asyncio
async def test_view_as_a_role_end_to_end(world):
    from api.core.staff_authz import ROLE_PERMS

    client, pool, owner, _ = world
    body = await _start(client)
    sid, tok = body["session_id"], body["token"]
    row = await pool.fetchrow(
        "SELECT tenant_id, view_role, reason, round(extract(epoch FROM expires_at - started_at)) AS secs, ended_at "
        "FROM shared.staff_view_as_session WHERE session_id = $1::uuid", sid)
    assert (row["tenant_id"], row["view_role"], row["reason"], row["secs"], row["ended_at"]) == (
        None, "content", REASON, 900, None)
    (started,) = await _audit(pool, "staff.view_as_started", sid)
    assert started["tenant_id"] is None and started["actor"] == f"admin:{owner}"
    assert started["details"]["capabilities"] == sorted(ROLE_PERMS["content"])
    assert started["details"]["view_role"] == "content" and REASON not in json.dumps(started["details"])

    view = {**_owner(), "x-staff-view-as": tok}
    hint = await client.get("/admin/session", headers=view)
    assert hint.json() == {"role": "content", "read_only": True, "reason": "view_as"}
    write = await client.post("/admin/platform/roles/cis", headers=view, json={})
    assert write.status_code == 403 and write.json()["detail"] == "view_as_read_only"
    refused = await pool.fetch(
        "SELECT status, details FROM acp_shared.audit_log WHERE action = 'staff.request' AND actor = $1 "
        "AND details ? 'view_as' ORDER BY created_at", f"admin:{owner}")
    assert refused and all(r["status"] == 403 for r in refused)
    assert json.loads(refused[-1]["details"])["view_as"] == sid

    # the session list of tenant views never shows it
    listed = await client.get("/admin/view-as/sessions", headers=_owner())
    assert sid not in [s["session_id"] for s in listed.json()["sessions"]]
    one = await client.get(f"/admin/view-as/sessions/{sid}", headers=_owner())
    assert one.json()["kind"] == "role" and one.json()["view_role"] == "content" and one.json()["open"] is True

    end = await client.post(f"/admin/view-as/sessions/{sid}/end", headers=_owner())
    assert end.status_code == 200 and end.json()["changed"] is True and end.json()["end_reason"] == "ended"
    (ended,) = await _audit(pool, "staff.view_as_ended", sid)
    assert ended["tenant_id"] is None and ended["details"]["cause"] == "ended_by_staff"
    gone = await client.get("/admin/session", headers=view)
    assert (gone.status_code, gone.json()["detail"]) == (409, "staff_view_as_ended")


@pytest.mark.asyncio
async def test_a_new_start_ends_both_kinds(world):
    client, pool, owner, tenant = world
    tenant_sid = await pool.fetchval(
        "INSERT INTO shared.staff_view_as_session (admin_user_id, tenant_id, reason, expires_at) "
        "VALUES ($1::uuid, $2::uuid, 'ticket follow-up', now() + interval '20 minutes') RETURNING session_id::text",
        owner, tenant)
    first = await _start(client, role="content")
    assert len(await _audit(pool, "tenant.view_as_ended", tenant_sid)) == 1
    second = await _start(client, role="custom_content")
    (ended,) = await _audit(pool, "staff.view_as_ended", first["session_id"])
    assert ended["details"]["cause"] == "replaced" and ended["details"]["replaced_by"] == second["session_id"]
    (started,) = await _audit(pool, "staff.view_as_started", second["session_id"])
    assert started["details"]["view_label"] == "Content"


@pytest.mark.asyncio
async def test_signing_out_closes_the_role_view(world):
    from api.core import view_as

    client, pool, owner, _ = world
    body = await _start(client)
    async with pool.acquire() as conn, conn.transaction():
        closed = await view_as.close_open_sessions(conn, actor=f"admin:{owner}", cause="staff_signed_out",
                                                   staff_id=owner)
    assert [c["session_id"] for c in closed] == [body["session_id"]]
    (ended,) = await _audit(pool, "staff.view_as_ended", body["session_id"])
    assert ended["details"]["cause"] == "staff_signed_out" and ended["details"]["view_role"] == "content"


@pytest.mark.asyncio
async def test_the_guard_keeps_a_role_row(world):
    client, pool, *_ = world
    body = await _start(client)
    with pytest.raises(asyncpg.RestrictViolationError):
        await pool.execute("DELETE FROM shared.staff_view_as_session WHERE session_id = $1::uuid", body["session_id"])


@pytest.mark.asyncio
async def test_the_owner_reading_an_expired_view_closes_it_with_its_end_row(world):
    """A view left to time out: the banner's last check (the owner's session read, through the BFF) writes the end row,
    exactly once."""
    client, pool, owner, _ = world
    sid = await pool.fetchval(
        "INSERT INTO shared.staff_view_as_session (admin_user_id, view_role, reason, started_at, expires_at) "
        "VALUES ($1::uuid, 'content', $2, now() - interval '20 minutes', now() - interval '5 minutes') "
        "RETURNING session_id::text", owner, REASON)
    one = await client.get(f"/admin/view-as/sessions/{sid}", headers=_owner())
    assert one.status_code == 200 and one.json()["open"] is False and one.json()["end_reason"] == "expired"
    again = await client.get(f"/admin/view-as/sessions/{sid}", headers=_owner())
    assert again.json()["end_reason"] == "expired"
    (ended,) = await _audit(pool, "staff.view_as_ended", sid)
    assert ended["details"]["cause"] == "expired" and ended["actor"] == f"admin:{owner}"


@pytest.mark.asyncio
async def test_two_concurrent_starts_leave_one_open_session(world):
    import asyncio

    client, pool, owner, _ = world
    a, b = await asyncio.gather(_start(client, role="content"), _start(client, role="custom_content"))
    open_rows = await pool.fetch(
        "SELECT session_id::text AS sid FROM shared.staff_view_as_session "
        "WHERE admin_user_id = $1::uuid AND ended_at IS NULL", owner)
    assert len(open_rows) == 1 and open_rows[0]["sid"] in (a["session_id"], b["session_id"])


@pytest.mark.asyncio
async def test_the_session_list_closes_an_expired_role_view_whose_cookie_is_gone(world):
    """The BFF's sign-out reads the member's session list first: an expired role view without an end (its cookie long
    gone) ends there with one staff.view_as_ended row; the list itself still shows tenant sessions only."""
    client, pool, owner, _ = world
    sid = await pool.fetchval(
        "INSERT INTO shared.staff_view_as_session (admin_user_id, view_role, reason, started_at, expires_at) "
        "VALUES ($1::uuid, 'content', $2, now() - interval '90 minutes', now() - interval '30 minutes') "
        "RETURNING session_id::text", owner, REASON)
    listed = await client.get("/admin/view-as/sessions?open_only=true", headers=_owner())
    assert listed.status_code == 200 and all(s["session_id"] != sid for s in listed.json()["sessions"])
    assert await pool.fetchval("SELECT end_reason FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                               sid) == "expired"
    await client.get("/admin/view-as/sessions", headers=_owner())
    (ended,) = await _audit(pool, "staff.view_as_ended", sid)
    assert ended["details"]["cause"] == "expired" and ended["actor"] == f"admin:{owner}"
