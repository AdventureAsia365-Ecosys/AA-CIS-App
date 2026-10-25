"""AA-978 — services/tenant_membership on real Postgres (CIS 276 replayed by conftest): the snapshot and identity
reads, the staff writes (role change, remove, their audit rows and refusals) and the feed SQL with its cursor and
snapshot horizon, end to end through the CIS 276 triggers.

The feed only serves committed transactions older than every open one, so this module commits its rows (a synthetic
tenant) and removes them at teardown with the append-only triggers skipped (tests/integration/_audit_cleanup.py
pattern: superuser, session_replication_role = replica, test DB only).
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _audit_cleanup import delete_audit_rows  # noqa: E402
from services.tenant_membership import feed, portal, reader, writer  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_zz978svc"


@pytest_asyncio.fixture
async def world():
    conn = await asyncpg.connect(DSN)
    tid = uuid.uuid4()
    try:
        await conn.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active) "
            "VALUES ($1, 'AA-978 service', $2, 'starter', 3, 60, true)", tid, f"zz978s-{tid.hex[:12]}")
        owner = await conn.fetchval(
            "INSERT INTO shared.tenant_members (tenant_id, cognito_iss, cognito_sub, content_studio_role, status, "
            "created_by) VALUES ($1, $2, 'sub-owner', 'owner', 'active', 'seed:AA-978') RETURNING membership_id",
            tid, ISS)
        invited = await conn.fetchval(
            "INSERT INTO shared.tenant_members (tenant_id, content_studio_role, status, invitation_id, created_by) "
            "VALUES ($1, 'viewer', 'invited', gen_random_uuid(), 'seed:AA-978') RETURNING membership_id", tid)
        yield conn, str(tid), str(owner), str(invited)
    finally:
        await delete_audit_rows(conn, "tenant_id = $1", str(tid))
        async with conn.transaction():
            await conn.execute("SET LOCAL session_replication_role = replica")
            await conn.execute("DELETE FROM shared.tenant_membership_event WHERE tenant_id = $1", tid)
            await conn.execute("DELETE FROM shared.tenant_members WHERE tenant_id = $1", tid)
            await conn.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
        await conn.close()


async def _all_events(conn, tid: str, limit: int = 500) -> list[dict]:
    out, after = [], None
    while True:
        page = await feed.list_events(conn, after, limit)
        out += [e for e in page["items"] if e["tenant_id"] == tid]
        if not page["items"]:
            return out
        after = page["next_cursor"]


@pytest.mark.asyncio
async def test_snapshot_and_identity_reads(world):
    conn, tid, owner, invited = world
    data = await reader.tenant_members(conn, tid)
    assert data["tenant_state"] == "active"
    assert {m["membership_id"]: m["status"] for m in data["members"]} == {owner: "active", invited: "invited"}
    found = await reader.identity_memberships(conn, ISS, "sub-owner")
    assert [m["membership_id"] for m in found["memberships"]] == [owner]
    assert (await reader.identity_memberships(conn, ISS + "x", "sub-owner"))["memberships"] == []
    assert await reader.tenant_members(conn, str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_staff_writes_audit_and_feed(world):
    conn, tid, owner, invited = world
    row, changed = await writer.change_role(conn, tid, owner, "editor", "admin:test")
    assert changed and row["content_studio_role"] == "editor" and row["identity_fingerprint"]
    assert "cognito_sub" not in row and "sub-owner" not in str(row)
    _, changed = await writer.change_role(conn, tid, owner, "editor", "admin:test")
    assert not changed
    row, changed = await writer.remove(conn, tid, owner, "admin:test")
    assert changed and row["status"] == "removed" and row["removed_by"] == "admin:test"
    _, changed = await writer.remove(conn, tid, owner, "admin:test")
    assert not changed
    with pytest.raises(writer.MemberRemoved):
        await writer.change_role(conn, tid, owner, "owner", "admin:test")
    with pytest.raises(writer.MemberNotFound):
        await writer.remove(conn, tid, str(uuid.uuid4()), "admin:test")
    with pytest.raises(writer.MemberNotFound):                      # the right id under another tenant
        await writer.remove(conn, str(uuid.uuid4()), invited, "admin:test")
    with pytest.raises(ValueError):
        await writer.change_role(conn, tid, invited, "agent", "admin:test")

    actions = [r["action"] for r in await conn.fetch(
        "SELECT action FROM acp_shared.audit_log WHERE tenant_id = $1 ORDER BY created_at", tid)]
    assert actions == ["tenant.member_role_changed", "tenant.member_removed"]

    events = await _all_events(conn, tid)
    assert [e["type"] for e in events] == ["tenant.created", "membership.activated", "membership.invited",
                                           "membership.role_changed", "membership.removed"]
    removed = events[-1]["membership"]
    assert removed["identity"] == {"iss": ISS, "sub": "sub-owner"} and removed["status"] == "removed"
    assert removed["content_studio_role"] == "editor"

    listing = await writer.list_members(conn, tid)
    assert [m["status"] for m in listing] == ["invited", "removed"]          # removed last
    assert [m["membership_id"] for m in (await reader.tenant_members(conn, tid))["members"]] == [invited]


@pytest.mark.asyncio
async def test_feed_cursor_pages_one_by_one(world):
    conn, tid, _, _ = world
    every = await _all_events(conn, tid)
    one_by_one = await _all_events(conn, tid, limit=1)
    assert [e["event_id"] for e in one_by_one] == [e["event_id"] for e in every]
    assert len({e["cursor"] for e in every}) == len(every)
    # a cursor taken now and replayed later only gives what came after it
    page = await feed.list_events(conn, every[-1]["cursor"], 500)
    assert all(e["tenant_id"] != tid for e in page["items"])
    await conn.execute("UPDATE shared.tenants SET is_active = false WHERE tenant_id = $1::uuid", tid)
    page = await feed.list_events(conn, every[-1]["cursor"], 500)
    assert [e["type"] for e in page["items"] if e["tenant_id"] == tid] == ["tenant.locked"]
    assert (await reader.tenant_members(conn, tid))["tenant_state"] == "locked"


@pytest.mark.asyncio
async def test_an_open_transaction_hides_its_event_until_commit(world):
    conn, tid, _, _ = world
    every = await _all_events(conn, tid)
    other = await asyncpg.connect(DSN)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("UPDATE shared.tenants SET is_active = false WHERE tenant_id = $1::uuid", tid)
        page = await feed.list_events(conn, every[-1]["cursor"], 500)
        assert [e for e in page["items"] if e["tenant_id"] == tid] == []
        await tx.commit()
        page = await feed.list_events(conn, every[-1]["cursor"], 500)
        assert [e["type"] for e in page["items"] if e["tenant_id"] == tid] == ["tenant.locked"]
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_portal_members_list_is_the_tenants_own_current_members(world):
    """AA-978 follow-up (GET /v1/account/members): invited + active rows of this tenant only, active first, the
    fingerprint instead of the identity; a removal leaves the list; another tenant id reads nothing."""
    conn, tid, owner, invited = world
    body = await portal.list_for_tenant(conn, tid)
    assert [(m["membership_id"], m["status"], m["role"]) for m in body["data"]] == [
        (owner, "active", "owner"), (invited, "invited", "viewer")]
    assert body["data"][0]["member_ref"] == writer.fingerprint(ISS, "sub-owner")
    assert body["data"][1]["member_ref"] is None
    assert "sub-owner" not in str(body) and ISS not in str(body)
    assert (await portal.list_for_tenant(conn, str(uuid.uuid4())))["data"] == []
    await writer.remove(conn, tid, invited, "admin:test")
    after = await portal.list_for_tenant(conn, tid)
    assert [m["membership_id"] for m in after["data"]] == [owner] and after["invited"] == 0
