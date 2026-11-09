"""AA-1035 R2 slice 2 on real Postgres (CIS 364 tables, guard trigger, audit chain): view-as, the Assignments page.

- a customer_success member starts a view-as session on its book: the session row (reason, 60-minute window), the
  start audit row; reads with the token are served and logged (tenant.view_as_request, route, outcome served); a write
  with the token is refused and logged; ending the session closes it (guard: once) and the token stops working;
- a tenant outside the member's book: 403, no row; removing the assignment ends the view at the next request;
- an expired session reads as ended and is closed as 'expired';
- the Assignments overview is scoped per role and the PUT refuses a role that works no tenant book;
- (S268 fix pass) the start answers a token only to the staff BFF (x-view-as-client: bff), and the reads depend on the
  role: support's token gets 403 view_as_not_for_role on /v1/quota and 403 view_as_read_only on /v1/billing (logged).
- (S268 fix round 2) a tenant offboarded or suspended during a session ends it at the next request (401, end_reason
  revoked, cause tenant_inactive) and the start refuses such a tenant (409); every path that ends a session writes one
  tenant.view_as_ended row under that session (the end route, a new start, the gate on expiry or a lost right); the
  bell of a desk role reaches only the tenant rows its scope covers.
- (S268 fix round 3) the session is bound to its start role (signed srl): a directory row that differs from the
  sign-in role is refused at the start (409, no row); a row that moves away from it during the session (the reported
  case: still admin in the table while the group says customer_success, then the assignment removed) ends the view at
  the next request (cause role_changed), and so does a different role forwarded by the staff BFF.

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


def _as(s, roles, who, bff=True):
    h = {"X-Admin-Secret": SECRET, "x-admin-role": roles[who], "x-admin-user-id": s[who]}
    if bff:
        h["x-view-as-client"] = "bff"   # api/core/view_as.py BFF_HEADER: the view-as BFF sets it on the start call
    return h


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
    # not through the BFF: refused before any row (no token for a page)
    not_bff = await client.post(f"/admin/tenants/{t['pooled']}/view-as", headers=_as(s, roles, "support", bff=False),
                                json={"reason": REASON})
    assert not_bff.status_code == 403 and not_bff.json()["detail"] == "view_as_bff_only"
    assert "token" not in not_bff.json()
    assert await pool.fetchval("SELECT count(*) FROM shared.staff_view_as_session WHERE tenant_id = $1::uuid",
                               t["pooled"]) == 0
    # support reaches the pool, but never the plan / usage read nor billing
    pooled = await client.post(f"/admin/tenants/{t['pooled']}/view-as", headers=_as(s, roles, "support"),
                               json={"reason": REASON})
    assert pooled.status_code == 201
    assert "/v1/quota" not in pooled.json()["allowed_reads"] and "/v1/billing" not in pooled.json()["allowed_reads"]
    support_bearer = {"Authorization": f"Bearer {pooled.json()['token']}"}
    quota = await client.get("/v1/quota", headers=support_bearer)
    assert quota.status_code == 403 and quota.json()["detail"] == "view_as_not_for_role"
    billing = await client.get("/v1/billing", headers=support_bearer)
    assert billing.status_code == 403 and billing.json()["detail"] == "view_as_read_only"
    assert (await client.get("/v1/account/members", headers=support_bearer)).status_code == 200
    support_log = await _audit(pool, "tenant.view_as_request", pooled.json()["session_id"])
    assert [(r["details"]["outcome"], r["route"], r["status"]) for r in support_log] == [
        ("refused_route_not_for_role", "/v1/quota", 403), ("refused_read_only", None, 403),
        ("served", "/v1/account/members", None)]
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
    # the view ended for good: the row is closed (revoked) with one ended row naming the cause; a re-assignment does
    # not bring it back
    sid = started.json()["session_id"]
    assert await pool.fetchval("SELECT end_reason FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                               sid) == "revoked"
    (ended,) = await _audit(pool, "tenant.view_as_ended", sid)
    assert ended["details"]["cause"] == "out_of_scope" and ended["actor"] == "system:view_as_gate"
    assert (await _assign(client, s, roles, t["mine"], "csm", "backup")).status_code == 200
    assert (await client.get("/v1/account/members", headers=bearer)).status_code == 401
    assert len(await _audit(pool, "tenant.view_as_ended", sid)) == 1     # no second end of a closed session


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
    (ended,) = await _audit(pool, "tenant.view_as_ended", sid)        # the gate's close is logged too
    assert ended["tenant_id"] == t["pooled"] and ended["details"]["end_reason"] == "expired"
    assert ended["details"]["cause"] == "expired"
    again = await client.get("/v1/account/members", headers={"Authorization": f"Bearer {started.json()['token']}"})
    assert again.status_code == 401 and len(await _audit(pool, "tenant.view_as_ended", sid)) == 1


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
    # the implicit end is logged under the FIRST session (its own tenant), naming the session that replaced it
    (ended,) = await _audit(pool, "tenant.view_as_ended", first.json()["session_id"])
    assert ended["tenant_id"] == t["pooled"] and ended["actor"] == f"admin:{s['admin']}"
    assert ended["details"] == {"session_id": first.json()["session_id"], "end_reason": "ended", "cause": "replaced",
                                "session_staff": s["admin"], "replaced_by": second.json()["session_id"]}
    assert await _audit(pool, "tenant.view_as_ended", second.json()["session_id"]) == []
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


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["offboard", "suspend"])
async def test_a_tenant_offboarded_or_suspended_during_a_session_ends_it(world, change):
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["mine"], "csm", "owner")).status_code == 200
    started = await client.post(f"/admin/tenants/{t['mine']}/view-as", headers=_as(s, roles, "csm"),
                                json={"reason": REASON})
    assert started.status_code == 201
    sid, bearer = started.json()["session_id"], {"Authorization": f"Bearer {started.json()['token']}"}
    assert (await client.get("/v1/account/members", headers=bearer)).status_code == 200
    if change == "offboard":
        await pool.execute("UPDATE shared.tenants SET cancelled_at = now() WHERE tenant_id = $1::uuid", t["mine"])
    else:
        await pool.execute("UPDATE shared.tenants SET is_active = false WHERE tenant_id = $1::uuid", t["mine"])
    gone = await client.get("/v1/account/members", headers=bearer)
    assert gone.status_code == 401 and gone.json()["detail"] == "view_as_ended"
    assert await pool.fetchval("SELECT end_reason FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                               sid) == "revoked"
    (ended,) = await _audit(pool, "tenant.view_as_ended", sid)
    assert ended["tenant_id"] == t["mine"] and ended["details"]["cause"] == "tenant_inactive"
    assert ended["details"]["end_reason"] == "revoked" and ended["details"]["session_staff"] == s["csm"]
    logged = await _audit(pool, "tenant.view_as_request", sid)
    assert [r["details"]["outcome"] for r in logged] == ["served", "refused_tenant_inactive"]
    # the start applies the same rule: no new session on that tenant
    again = await client.post(f"/admin/tenants/{t['mine']}/view-as", headers=_as(s, roles, "csm"),
                              json={"reason": REASON})
    assert again.status_code == 409
    assert await pool.fetchval("SELECT count(*) FROM shared.staff_view_as_session WHERE tenant_id = $1::uuid",
                               t["mine"]) == 1


@pytest.mark.asyncio
async def test_the_bell_of_a_desk_role_reaches_only_its_tenants(world):
    """customer_success: its book; support: its book and the pool; finance: every tenant. A platform row (the AA
    internal tenant) stays role-targeted. Mark one / all never reaches a row outside the scope."""
    client, pool, t, s, roles = world
    internal = "00000000-0000-0000-0000-000000000001"
    assert (await _assign(client, s, roles, t["mine"], "csm", "owner")).status_code == 200
    assert (await _assign(client, s, roles, t["other"], "admin", "owner")).status_code == 200   # out of the pool
    tag = f"aa1035s2.bell.{uuid.uuid4().hex[:8]}"
    ids = {}
    for key, tid in (("mine", t["mine"]), ("other", t["other"]), ("pooled", t["pooled"]), ("internal", internal)):
        ids[key] = await pool.fetchval(
            "INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, "
            "target_roles) VALUES ($1::uuid, 'system', $2, 'tenant', $3, '{}'::jsonb, "
            "ARRAY['customer_success','support','finance']) RETURNING id", tid, tag, key)
    try:
        def headers(who, role=None):
            return {"X-Admin-Secret": SECRET, "x-admin-role": role or roles[who], "x-admin-user-id": s[who]}

        async def seen(h):
            res = await client.get("/admin/notifications?limit=200", headers=h)
            assert res.status_code == 200, res.text
            return {x["entity_id"] for x in res.json()["items"] if x["event_type"] == tag}

        assert await seen(headers("csm")) == {"mine", "internal"}
        assert await seen(headers("support")) == {"pooled", "internal"}
        assert await seen(headers("admin", "finance")) == {"mine", "other", "pooled", "internal"}
        # no staff id: a desk role reaches no tenant row (fail closed), the platform row only
        no_id = {"X-Admin-Secret": SECRET, "x-admin-role": "support"}
        assert await seen(no_id) == {"internal"}

        # marks: support cannot mark the csm's tenant row, read-all clears only its own scope
        miss = await client.put(f"/admin/notifications/{ids['mine']}/read", headers=headers("support"))
        assert miss.status_code == 404
        cleared = await client.put("/admin/notifications/read-all", headers=headers("support"))
        assert cleared.status_code == 200
        unread = {r["entity_id"] for r in await pool.fetch(
            "SELECT entity_id FROM shared.notifications WHERE event_type = $1 AND NOT is_read", tag)}
        assert unread == {"mine", "other"}
        # the count follows the same scope as the list (csm: its unread tenant row; the platform row is read now)
        count = await client.get("/admin/notifications/count", headers=headers("csm"))
        listed = await client.get("/admin/notifications?unread_only=true&limit=200", headers=headers("csm"))
        assert count.status_code == 200 and count.json()["unread"] == len(listed.json()["items"])
        assert {x["entity_id"] for x in listed.json()["items"] if x["event_type"] == tag} == {"mine"}
    finally:
        await pool.execute("DELETE FROM shared.notifications WHERE event_type = $1", tag)


@pytest.mark.asyncio
async def test_a_directory_role_other_than_the_sign_in_role_never_widens_a_view(world):
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["mine"], "csm", "owner")).status_code == 200
    # drift at the start (the table says admin, the sign-in group customer_success): refused before any row
    await pool.execute("UPDATE shared.admin_users SET role = 'admin' WHERE id = $1::uuid", s["csm"])
    refused = await client.post(f"/admin/tenants/{t['mine']}/view-as", headers=_as(s, roles, "csm"),
                                json={"reason": REASON})
    assert refused.status_code == 409 and "token" not in refused.json()
    assert await pool.fetchval("SELECT count(*) FROM shared.staff_view_as_session WHERE admin_user_id = $1::uuid",
                               s["csm"]) == 0

    # the reported case: a session started while the two agreed, then the row drifts ABOVE the sign-in role and the
    # assignment is removed. Before the fix the gate took admin's scope (every tenant) and kept serving.
    await pool.execute("UPDATE shared.admin_users SET role = 'customer_success' WHERE id = $1::uuid", s["csm"])
    started = await client.post(f"/admin/tenants/{t['mine']}/view-as", headers=_as(s, roles, "csm"),
                                json={"reason": REASON})
    assert started.status_code == 201, started.text
    sid, bearer = started.json()["session_id"], {"Authorization": f"Bearer {started.json()['token']}"}
    assert (await client.get("/v1/account/members", headers=bearer)).status_code == 200
    await pool.execute("UPDATE shared.admin_users SET role = 'admin' WHERE id = $1::uuid", s["csm"])
    await client.delete(f"/admin/tenants/{t['mine']}/assignments/{s['csm']}", headers=_as(s, roles, "admin"))
    gone = await client.get("/v1/account/members", headers=bearer)
    assert gone.status_code == 401 and gone.json()["detail"] == "view_as_ended"
    assert await pool.fetchval("SELECT end_reason FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                               sid) == "revoked"
    (ended,) = await _audit(pool, "tenant.view_as_ended", sid)
    assert ended["details"]["cause"] == "role_changed" and ended["actor"] == "system:view_as_gate"
    logged = await _audit(pool, "tenant.view_as_request", sid)
    assert [r["details"]["outcome"] for r in logged] == ["served", "refused_role_changed"]


@pytest.mark.asyncio
async def test_a_different_role_forwarded_by_the_bff_ends_the_view(world):
    client, pool, t, s, roles = world
    started = await client.post(f"/admin/tenants/{t['pooled']}/view-as", headers=_as(s, roles, "support"),
                                json={"reason": REASON})
    assert started.status_code == 201, started.text
    sid, bearer = started.json()["session_id"], {"Authorization": f"Bearer {started.json()['token']}"}
    same = await client.get("/v1/account/members", headers={**bearer, "x-view-as-staff-role": "support"})
    assert same.status_code == 200
    # the sign-in group moved to customer_success (the table did not): the next read ends the view, never widens it
    moved = await client.get("/v1/account/members", headers={**bearer, "x-view-as-staff-role": "customer_success"})
    assert moved.status_code == 401
    (ended,) = await _audit(pool, "tenant.view_as_ended", sid)
    assert ended["details"]["cause"] == "role_changed" and ended["details"]["end_reason"] == "revoked"
    assert (await client.get("/v1/account/members", headers=bearer)).status_code == 401


# ── S268 follow-ups: offboarding and the staff sign-out end the open views at once; verify-tenant; E2E admin ─────────

async def _start(client, s, roles, tenant, who):
    res = await client.post(f"/admin/tenants/{tenant}/view-as", headers=_as(s, roles, who), json={"reason": REASON})
    assert res.status_code == 201, res.text
    return res.json()


@pytest.mark.asyncio
async def test_offboarding_a_tenant_ends_every_open_view_on_it_at_once(world):
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["mine"], "csm", "owner")).status_code == 200
    csm = await _start(client, s, roles, t["mine"], "csm")
    admin = await _start(client, s, roles, t["mine"], "admin")
    elsewhere = await _start(client, s, roles, t["pooled"], "support")          # another tenant: stays open
    res = await client.post(f"/admin/tenants/{t['mine']}/offboard", headers=_as(s, roles, "admin"),
                            json={"reason": "Partner closed the account"})
    assert res.status_code == 200, res.text
    assert res.json()["ended_view_as_sessions"] == 2
    for sess, staff in ((csm, "csm"), (admin, "admin")):
        # closed by the offboarding itself: no request with the token was needed
        assert await pool.fetchval("SELECT end_reason FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                                   sess["session_id"]) == "revoked"
        (ended,) = await _audit(pool, "tenant.view_as_ended", sess["session_id"])
        assert ended["tenant_id"] == t["mine"] and ended["actor"] == f"admin:{s['admin']}"
        assert ended["details"] == {"session_id": sess["session_id"], "end_reason": "revoked",
                                    "cause": "tenant_offboarded", "session_staff": s[staff]}
        assert await _audit(pool, "tenant.view_as_request", sess["session_id"]) == []
    offboard = await pool.fetchval("SELECT details FROM acp_shared.audit_log WHERE action = 'agency.offboard' "
                                   "AND resource_id = $1", t["mine"])
    details = json.loads(offboard) if isinstance(offboard, str) else offboard
    assert sorted(details["ended_view_as_sessions"]) == sorted([csm["session_id"], admin["session_id"]])
    assert await pool.fetchval("SELECT ended_at IS NULL FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                               elsewhere["session_id"]) is True
    gone = await client.get("/v1/account/members", headers={"Authorization": f"Bearer {csm['token']}"})
    assert gone.status_code == 401 and gone.json()["detail"] == "view_as_ended"


class _Cognito:
    """Stands in for CognitoDirectory (records nothing that matters here, never touches AWS)."""

    def disable(self, email):
        pass

    def enable(self, email):
        pass

    def global_sign_out(self, email, user_disabled=False):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("op,cause", [("disable_staff", "staff_disabled"), ("sign_out_staff", "staff_signed_out")])
async def test_disabling_or_signing_out_a_member_ends_its_open_view_at_once(world, op, cause):
    from api.services import staff_directory as sd

    client, pool, t, s, roles = world
    await pool.execute("UPDATE shared.admin_users SET is_owner = true WHERE id = $1::uuid", s["admin"])
    assert (await _assign(client, s, roles, t["mine"], "csm", "owner")).status_code == 200
    mine = await _start(client, s, roles, t["mine"], "csm")
    other = await _start(client, s, roles, t["pooled"], "support")                # another member: stays open
    await getattr(sd, op)(pool, _Cognito(), actor_id=s["admin"], staff_id=s["csm"])
    assert await pool.fetchval("SELECT end_reason FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                               mine["session_id"]) == "revoked"
    (ended,) = await _audit(pool, "tenant.view_as_ended", mine["session_id"])
    assert ended["actor"] == f"admin:{s['admin']}" and ended["tenant_id"] == t["mine"]
    assert ended["details"]["cause"] == cause and ended["details"]["session_staff"] == s["csm"]
    staff_row = await pool.fetchval("SELECT details FROM acp_shared.audit_log WHERE resource_type = 'admin_user' "
                                    "AND resource_id = $1 ORDER BY created_at DESC LIMIT 1", s["csm"])
    staff_details = json.loads(staff_row) if isinstance(staff_row, str) else staff_row
    assert staff_details["ended_view_as_sessions"] == [mine["session_id"]]
    assert await pool.fetchval("SELECT ended_at IS NULL FROM shared.staff_view_as_session WHERE session_id = $1::uuid",
                               other["session_id"]) is True
    # a second sign-out finds nothing open: no second ended row
    if op == "sign_out_staff":
        await sd.sign_out_staff(pool, _Cognito(), actor_id=s["admin"], staff_id=s["csm"])
        assert len(await _audit(pool, "tenant.view_as_ended", mine["session_id"])) == 1
    await delete_audit_rows(pool, "resource_type = 'admin_user' AND resource_id = $1", s["csm"])


@pytest.mark.asyncio
async def test_verify_tenant_refuses_a_view_as_token(world):
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["mine"], "csm", "owner")).status_code == 200
    sess = await _start(client, s, roles, t["mine"], "csm")
    res = await client.post("/auth/verify-tenant", json={"token": sess["token"]})
    assert res.status_code == 403 and res.json()["detail"] == "A view-as token is not a tenant session"
