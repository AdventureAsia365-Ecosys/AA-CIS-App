"""AA-979 P2b — services/tenant_membership/session.py on real Postgres (CIS 276 replayed by conftest): the staff
invitation row, the CRM's activation through the CIS 276 guard and trigger (membership.activated event), its
idempotency and refusals, and the exchange's membership read (active rows on active agencies only).

Same world / teardown pattern as test_aa978_membership_service.py (superuser cleanup with session_replication_role =
replica, test DB only)."""
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
from services.tenant_membership import session as studio  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_zz979partner"


@pytest_asyncio.fixture
async def world():
    conn = await asyncpg.connect(DSN)
    tids = [uuid.uuid4(), uuid.uuid4()]
    try:
        for n, tid in enumerate(tids):
            await conn.execute(
                "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                "is_active) VALUES ($1, $2, $3, 'growth', 3, 60, $4)",
                tid, f"AA-979 agency {n}", f"zz979-{tid.hex[:12]}", n == 0)
        yield conn, str(tids[0]), str(tids[1])
    finally:
        for tid in tids:
            await delete_audit_rows(conn, "tenant_id = $1", str(tid))
            async with conn.transaction():
                await conn.execute("SET LOCAL session_replication_role = replica")
                await conn.execute("DELETE FROM shared.tenant_membership_event WHERE tenant_id = $1", tid)
                await conn.execute("DELETE FROM shared.tenant_members WHERE tenant_id = $1", tid)
                await conn.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
        await conn.close()


@pytest.mark.asyncio
async def test_invite_then_activate_then_the_exchange_sees_it(world):
    conn, active_tid, _ = world
    member = await studio.invite(conn, active_tid, "editor", "admin:zz979")
    assert member["status"] == "invited" and member["invitation_id"]
    assert await studio.identity_memberships(conn, ISS, "sub-a") == []  # invited is not a session

    row, changed = await studio.activate(conn, member["invitation_id"], ISS, "sub-a", "m2m:crm")
    assert changed and row["status"] == "active" and (row["cognito_iss"], row["cognito_sub"]) == (ISS, "sub-a")
    events = await conn.fetch(
        "SELECT type FROM shared.tenant_membership_event WHERE tenant_id = $1::uuid ORDER BY seq", active_tid)
    assert [e["type"] for e in events][-2:] == ["membership.invited", "membership.activated"]
    actions = [r["action"] for r in await conn.fetch(
        "SELECT action FROM acp_shared.audit_log WHERE tenant_id = $1 ORDER BY created_at", active_tid)]
    assert actions == ["tenant.member_invited", "tenant.member_activated"]

    rows = await studio.identity_memberships(conn, ISS, "sub-a")
    assert [(str(r["tenant_id"]), r["content_studio_role"], r["name"]) for r in rows] == [
        (active_tid, "editor", "AA-979 agency 0")]

    again, changed = await studio.activate(conn, member["invitation_id"], ISS, "sub-a", "m2m:crm")
    assert not changed and again["status"] == "active"
    with pytest.raises(studio.InvitationClosed) as exc:
        await studio.activate(conn, member["invitation_id"], ISS, "sub-b", "m2m:crm")
    assert exc.value.code == "identity_mismatch"


@pytest.mark.asyncio
async def test_second_live_membership_of_one_identity_is_refused(world):
    conn, tid, _ = world
    first = await studio.invite(conn, tid, "viewer", "admin:zz979")
    second = await studio.invite(conn, tid, "owner", "admin:zz979")
    await studio.activate(conn, first["invitation_id"], ISS, "sub-c", "m2m:crm")
    with pytest.raises(studio.InvitationClosed) as exc:
        await studio.activate(conn, second["invitation_id"], ISS, "sub-c", "m2m:crm")
    assert exc.value.code == "already_member"


@pytest.mark.asyncio
async def test_removed_or_unknown_invitations_and_inactive_agencies(world):
    conn, tid, suspended_tid = world
    member = await studio.invite(conn, tid, "viewer", "admin:zz979")
    await conn.execute("UPDATE shared.tenant_members SET status = 'removed', removed_at = now(), "
                       "removed_by = 'admin:zz979' WHERE membership_id = $1::uuid", member["membership_id"])
    with pytest.raises(studio.InvitationClosed) as exc:
        await studio.activate(conn, member["invitation_id"], ISS, "sub-d", "m2m:crm")
    assert exc.value.code == "membership_removed"
    with pytest.raises(studio.InvitationNotFound):
        await studio.activate(conn, str(uuid.uuid4()), ISS, "sub-d", "m2m:crm")
    with pytest.raises(studio.TenantInactive):
        await studio.invite(conn, suspended_tid, "editor", "admin:zz979")


@pytest.mark.asyncio
async def test_exchange_ignores_memberships_of_suspended_agencies(world):
    conn, tid, suspended_tid = world
    await conn.execute(
        "INSERT INTO shared.tenant_members (tenant_id, cognito_iss, cognito_sub, content_studio_role, status, "
        "created_by) VALUES ($1::uuid, $2, 'sub-e', 'owner', 'active', 'seed:AA-979')", suspended_tid, ISS)
    assert await studio.identity_memberships(conn, ISS, "sub-e") == []
