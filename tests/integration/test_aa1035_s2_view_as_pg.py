"""AA-1035 R2 slice 2 on real Postgres (CIS 364 tables, guard trigger, audit chain): view-as, the Assignments page.

- a customer_success member starts a view-as session on its book: the session row (reason, 60-minute window), the
  start audit row; reads with the token are served and logged (tenant.view_as_request, route, outcome served); a write
  with the token is refused and logged; ending the session closes it (guard: once) and the token stops working;
- a tenant outside the member's book: 403, no row; removing the assignment ends the view at the next request;
- an expired session reads as ended and is closed as 'expired';
- the Assignments overview is scoped per role and the PUT refuses a role that works no tenant book.

Calls carry the main admin secret + x-admin-role + x-admin-user-id (the machine / CI identity path of
api/core/tenant_scope.py), as tests/integration/test_aa1035_tenant_scope_pg.py does.
"""
from __future__ import annotations

import json
import os
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

from _audit_cleanup import delete_audit_rows  # noqa: E402  (audit_log is append-only, CIS 248)

DB_HOST = os.environ.get("CIS_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("CIS_TEST_DB_PORT", "5432"))
DB_NAME = os.environ.get("CIS_TEST_DB_NAME", "cis_integration_test")
DB_USER = os.environ.get("CIS_TEST_DB_USER", "cistest")
DB_PASS = os.environ.get("CIS_TEST_DB_PASS", "cistest")
DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = f"aa1035-s2-it-{uuid.uuid4().hex}"   # a fresh value per run, never a fixed string
_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
REASON = "Partner reports a missing tour on the Published list"


def _sql(name):
    with open(os.path.join(_MIG, name), encoding="utf-8") as f:
        return f.read()


@pytest_asyncio.fixture
async def world(monkeypatch):
    from api.main import app
    from api.routers import admin

    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    for name in ("362_admin_role_r2_staff_roles.sql", "363_staff_roles_reviewer_merge_finance_grant.sql",
                 "364_staff_tenant_assignment.sql"):
        await pool.execute(_sql(name))
    tag = uuid.uuid4().hex[:8]
    t = {k: str(uuid.uuid4()) for k in ("mine", "other", "pooled")}
    for key, tid in t.items():
        await pool.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, kind, segment) "
            "VALUES ($1, $2, $3, 'starter', 3, 'agency', $4)",
            uuid.UUID(tid), f"Lantern Cove {key}", f"aa1035s2-{tag}-{key}",
            "tech_touch" if key == "pooled" else "high_touch")
    s = {k: str(uuid.uuid4()) for k in ("admin", "csm", "support", "writer")}
    roles = {"admin": "admin", "csm": "customer_success", "support": "support", "writer": "content"}
    for key, sid in s.items():
        await pool.execute(
            "INSERT INTO shared.admin_users (id, username, role, is_active, email, display_name) "
            "VALUES ($1, $2, $3::shared.admin_role, true, $4, $5)",
            uuid.UUID(sid), f"aa1035s2-{tag}-{key}", roles[key], f"aa1035s2-{tag}-{key}@adventure.asia",
            f"Staff {key.title()}")
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    # the tenant rate limiter on /v1 reads the shared Redis (revocation + RPM bucket)
    import fakeredis.aioredis
    monkeypatch.setattr(app.state, "redis", fakeredis.aioredis.FakeRedis(decode_responses=True), raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, pool, t, s, roles
    staff_ids = [uuid.UUID(v) for v in s.values()]
    await delete_audit_rows(pool, "tenant_id = ANY($1::text[]) OR actor = ANY($2::text[])", list(t.values()),
                            [f"admin:{v}" for v in s.values()])
    await pool.execute("DELETE FROM shared.tenant_api_usage WHERE admin_user_id = ANY($1::uuid[])", staff_ids)
    # tenants first: the FK cascade removes their view-as rows (the guard lets a deleted tenant's rows go), then staff
    await pool.execute("DELETE FROM shared.tenants WHERE slug LIKE $1", f"aa1035s2-{tag}-%")
    await pool.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", staff_ids)
    await pool.close()


def _as(s, roles, who):
    return {"X-Admin-Secret": SECRET, "x-admin-role": roles[who], "x-admin-user-id": s[who]}


async def _assign(client, s, roles, tenant, who, kind):
    return await client.put(f"/admin/tenants/{tenant}/assignments/{s[who]}", json={"kind": kind},
                            headers=_as(s, roles, "admin"))


async def _audit(pool, action, resource_id):
    rows = await pool.fetch(
        "SELECT tenant_id, actor, route, status, http_method, details FROM acp_shared.audit_log "
        "WHERE action = $1 AND resource_id = $2 ORDER BY chain_seq NULLS LAST, created_at", action, resource_id)
    return [dict(r, details=json.loads(r["details"]) if isinstance(r["details"], str) else r["details"])
            for r in rows]


@pytest.mark.asyncio
async def test_view_as_end_to_end_on_the_book(world):
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["mine"], "csm", "owner")).status_code == 200

    res = await client.post(f"/admin/tenants/{t['mine']}/view-as", headers=_as(s, roles, "csm"),
                            json={"reason": REASON, "ticket_ref": "SUP-77", "minutes": 20})
    assert res.status_code == 201, res.text
    body = res.json()
    sid, tok = body["session_id"], body["token"]
    row = await pool.fetchrow(
        "SELECT admin_user_id::text AS staff, tenant_id::text AS tenant, reason, ticket_ref, "
        "round(extract(epoch FROM expires_at - started_at)) AS secs, ended_at FROM shared.staff_view_as_session "
        "WHERE session_id = $1::uuid", sid)
    assert (row["staff"], row["tenant"], row["reason"], row["ticket_ref"], row["secs"], row["ended_at"]) == (
        s["csm"], t["mine"], REASON, "SUP-77", 1200, None)
    started = await _audit(pool, "tenant.view_as_started", sid)
    assert len(started) == 1 and started[0]["actor"] == f"admin:{s['csm']}" and REASON not in json.dumps(
        started[0]["details"])

    bearer = {"Authorization": f"Bearer {tok}"}
    read = await client.get("/v1/account/members", headers=bearer)
    assert read.status_code == 200, read.text
    write = await client.post(f"/v1/tours/pool/{uuid.uuid4()}/rewrite", headers=bearer, json={})
    assert write.status_code == 403 and write.json()["detail"] == "view_as_read_only"
    slate = await client.get("/v1/slate", headers=bearer)                 # a read that starts work: refused
    assert slate.status_code == 403
    logged = await _audit(pool, "tenant.view_as_request", sid)
    assert [(r["details"]["outcome"], r["route"], r["status"]) for r in logged] == [
        ("served", "/v1/account/members", None), ("refused_read_only", None, 403), ("refused_read_only", None, 403)]
    assert all(r["tenant_id"] == t["mine"] and r["actor"] == f"admin:{s['csm']}" and r["http_method"] is None
               for r in logged)

    other = await client.get(f"/admin/view-as/sessions/{sid}", headers=_as(s, roles, "support"))
    assert other.status_code == 404                                        # another member's session
    end = await client.post(f"/admin/view-as/sessions/{sid}/end", headers=_as(s, roles, "csm"))
    assert end.status_code == 200 and end.json()["end_reason"] == "ended" and end.json()["open"] is False
    again = await client.post(f"/admin/view-as/sessions/{sid}/end", headers=_as(s, roles, "csm"))
    assert again.status_code == 200 and again.json()["changed"] is False
    after = await client.get("/v1/account/members", headers=bearer)
    assert after.status_code == 401 and after.json()["detail"] == "view_as_ended"
    assert len(await _audit(pool, "tenant.view_as_ended", sid)) == 1

    # the log is append-only in the database too
    with pytest.raises(asyncpg.PostgresError):
        await pool.execute("UPDATE shared.staff_view_as_session SET reason = 'changed' WHERE session_id = $1::uuid",
                           sid)


@pytest.mark.asyncio
async def test_scope_and_a_removed_assignment_end_the_view(world):
    client, pool, t, s, roles = world
    refused = await client.post(f"/admin/tenants/{t['other']}/view-as", headers=_as(s, roles, "csm"),
                                json={"reason": REASON})
    assert refused.status_code == 403
    assert await pool.fetchval("SELECT count(*) FROM shared.staff_view_as_session WHERE tenant_id = $1::uuid",
                               t["other"]) == 0
    # support reaches the pool
    pooled = await client.post(f"/admin/tenants/{t['pooled']}/view-as", headers=_as(s, roles, "support"),
                               json={"reason": REASON})
    assert pooled.status_code == 201
    # the content role never
    writer = await client.post(f"/admin/tenants/{t['mine']}/view-as", headers=_as(s, roles, "writer"),
                               json={"reason": REASON})
    assert writer.status_code == 403

    assert (await _assign(client, s, roles, t["mine"], "csm", "backup")).status_code == 200
    started = await client.post(f"/admin/tenants/{t['mine']}/view-as", headers=_as(s, roles, "csm"),
                                json={"reason": REASON})
    bearer = {"Authorization": f"Bearer {started.json()['token']}"}
    assert (await client.get("/v1/account/members", headers=bearer)).status_code == 200
    await client.delete(f"/admin/tenants/{t['mine']}/assignments/{s['csm']}", headers=_as(s, roles, "admin"))
    gone = await client.get("/v1/account/members", headers=bearer)
    assert gone.status_code == 401
    logged = await _audit(pool, "tenant.view_as_request", started.json()["session_id"])
    assert logged[-1]["details"]["outcome"] == "refused_out_of_scope"


@pytest.mark.asyncio
async def test_an_expired_session_is_closed_as_expired(world):
    client, pool, t, s, roles = world
    started = await client.post(f"/admin/tenants/{t['pooled']}/view-as", headers=_as(s, roles, "admin"),
                                json={"reason": REASON, "minutes": 5})
    sid = started.json()["session_id"]
    # move the window into the past (the guard allows no UPDATE of it, so as the table owner without the trigger)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("ALTER TABLE shared.staff_view_as_session DISABLE TRIGGER staff_view_as_session_guard")
        await conn.execute("UPDATE shared.staff_view_as_session SET started_at = now() - interval '10 minutes', "
                           "expires_at = now() - interval '5 minutes' WHERE session_id = $1::uuid", sid)
        await conn.execute("ALTER TABLE shared.staff_view_as_session ENABLE TRIGGER staff_view_as_session_guard")
    res = await client.get("/v1/account/members", headers={"Authorization": f"Bearer {started.json()['token']}"})
    # the token itself is still valid for a few more minutes; the session is not
    assert res.status_code == 401
    assert await pool.fetchval("SELECT end_reason FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                               sid) == "expired"


@pytest.mark.asyncio
async def test_a_new_session_ends_the_previous_one(world):
    client, pool, t, s, roles = world
    first = await client.post(f"/admin/tenants/{t['pooled']}/view-as", headers=_as(s, roles, "admin"),
                              json={"reason": REASON})
    second = await client.post(f"/admin/tenants/{t['mine']}/view-as", headers=_as(s, roles, "admin"),
                               json={"reason": REASON})
    assert second.status_code == 201
    assert await pool.fetchval("SELECT end_reason FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                               first.json()["session_id"]) == "ended"
    listed = await client.get("/admin/view-as/sessions?open_only=true", headers=_as(s, roles, "admin"))
    ids = [x["session_id"] for x in listed.json()["sessions"]]
    assert second.json()["session_id"] in ids and first.json()["session_id"] not in ids


@pytest.mark.asyncio
async def test_assignments_overview_is_scoped_and_the_put_refuses_a_content_role(world):
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["mine"], "csm", "owner")).status_code == 200
    assert (await _assign(client, s, roles, t["other"], "admin", "owner")).status_code == 200
    bad = await _assign(client, s, roles, t["mine"], "writer", "support")
    assert bad.status_code == 409

    def mine(body):
        return {x["tenant_id"]: x for x in body["tenants"] if x["tenant_id"] in t.values()}

    admin = await client.get("/admin/tenant-assignments", headers=_as(s, roles, "admin"))
    assert admin.status_code == 200 and admin.json()["can_assign"] is True
    seen = mine(admin.json())
    assert set(seen) == set(t.values())
    assert [(a["admin_user_id"], a["kind"]) for a in seen[t["mine"]]["assignments"]] == [(s["csm"], "owner")]
    assert seen[t["pooled"]]["in_pool"] is True and seen[t["mine"]]["in_pool"] is False

    csm = await client.get("/admin/tenant-assignments", headers=_as(s, roles, "csm"))
    assert csm.status_code == 200 and csm.json()["can_assign"] is False
    assert set(mine(csm.json())) == {t["mine"]}
    support = await client.get("/admin/tenant-assignments", headers=_as(s, roles, "support"))
    assert set(mine(support.json())) == {t["pooled"]}
    assert (await client.get("/admin/tenant-assignments", headers=_as(s, roles, "writer"))).status_code == 403

    staff = await client.get("/admin/tenant-assignments/staff", headers=_as(s, roles, "admin"))
    ids = {x["admin_user_id"] for x in staff.json()["staff"]}
    assert {s["admin"], s["csm"], s["support"]} <= ids and s["writer"] not in ids
    assert (await client.get("/admin/tenant-assignments/staff", headers=_as(s, roles, "csm"))).status_code == 403
