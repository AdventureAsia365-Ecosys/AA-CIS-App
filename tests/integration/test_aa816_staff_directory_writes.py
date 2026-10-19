"""AA-816 C6 — the staff directory writer (api/services/staff_directory.py) against real Postgres.

Proves the SQL, not Cognito (a recording fake stands in for the pool; the unit tests cover the boto3 calls under
Stubber): create inserts a Cognito-only shared.admin_users row (password_hash NULL, lower-cased address, derived
username) and its shared.staff_app_grant row and an acp_shared.audit_log row in one transaction; the Booking grant is
upserted, changed and deleted; disable / enable flip is_active; the owner-lock and last-owner queries run; the
admin_platform list reads the grant through its LEFT JOIN; a refused write leaves nothing behind.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio
from _audit_cleanup import delete_audit_rows  # AA-884: audit_log is append-only (CIS 248)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_platform  # noqa: E402
from api.services import staff_directory as sd  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class RecordingCognito:
    """Stands in for CognitoDirectory: records the calls, never touches AWS."""

    def __init__(self):
        self.calls = []

    def create_user(self, email):
        self.calls.append(("create_user", email))
        return True

    def add_to_group(self, email, group):
        self.calls.append(("add", email, group))

    def remove_from_group(self, email, group):
        self.calls.append(("remove", email, group))

    def disable(self, email):
        self.calls.append(("disable", email))

    def enable(self, email):
        self.calls.append(("enable", email))

    def global_sign_out(self, email, user_disabled=False):
        self.calls.append(("sign_out", email))


@pytest_asyncio.fixture
async def env():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    tag = uuid.uuid4().hex[:8]
    owner = uuid.uuid4()
    await pool.execute(
        "INSERT INTO shared.admin_users (id, username, password_hash, role, is_active, email, is_owner) "
        "VALUES ($1, $2, NULL, 'admin', true, $3, true)", owner, f"aa816c6-owner-{tag}",
        f"aa816c6-owner-{tag}@adventure.asia")
    created_emails = []
    yield pool, str(owner), tag, created_emails
    ids = await pool.fetch("SELECT id FROM shared.admin_users WHERE id = $1 OR lower(email) = ANY($2::text[])",
                           owner, created_emails)
    id_list = [r["id"] for r in ids]
    await delete_audit_rows(pool, "resource_type = 'admin_user' AND resource_id = ANY($1::text[])",
                            [str(i) for i in id_list])
    await pool.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", id_list)
    await pool.close()


@pytest.mark.asyncio
async def test_create_change_disable_enable_against_postgres(env):
    pool, owner, tag, emails = env
    email = f"aa816c6.{tag}@adventure.asia"
    emails.append(email)
    cognito = RecordingCognito()

    res = await sd.create_staff(pool, cognito, actor_id=owner, email=email.upper(), display_name="C6 Person",
                                role="reviewer", booking_role="sales")
    assert res.created and res.status == sd.STATUS_OK
    row = await pool.fetchrow("SELECT username, password_hash, role::text AS role, email, display_name, is_active, "
                              "is_owner FROM shared.admin_users WHERE id = $1::uuid", res.staff_id)
    assert dict(row) == {"username": f"aa816c6.{tag}", "password_hash": None, "role": "reviewer", "email": email,
                         "display_name": "C6 Person", "is_active": True, "is_owner": False}
    grant = await pool.fetchval("SELECT role FROM shared.staff_app_grant WHERE admin_user_id = $1::uuid "
                                "AND app = 'booking'", res.staff_id)
    assert grant == "sales"
    audit = await pool.fetch("SELECT actor, action, details FROM acp_shared.audit_log WHERE resource_type = "
                             "'admin_user' AND resource_id = $1 ORDER BY created_at", res.staff_id)
    assert [a["action"] for a in audit] == ["staff.created"]
    assert audit[0]["actor"] == f"admin:{owner}"
    assert json.loads(audit[0]["details"])["booking_role"] == "sales"
    assert cognito.calls == [("create_user", email), ("add", email, "cis-reviewer"), ("add", email, "booking-sales")]

    # The Booking grant: change (upsert), then remove.
    await sd.change_roles(pool, cognito, actor_id=owner, staff_id=res.staff_id, role="content",
                          booking_role="content_creator")
    assert await pool.fetchval("SELECT role FROM shared.staff_app_grant WHERE admin_user_id = $1::uuid",
                               res.staff_id) == "content_creator"
    assert await pool.fetchval("SELECT role::text FROM shared.admin_users WHERE id = $1::uuid",
                               res.staff_id) == "content"
    await sd.change_roles(pool, cognito, actor_id=owner, staff_id=res.staff_id, booking_role=None)
    assert await pool.fetchval("SELECT count(*) FROM shared.staff_app_grant WHERE admin_user_id = $1::uuid",
                               res.staff_id) == 0

    # The list's LEFT JOIN reads the grant (none now) next to the directory fields.
    await sd.change_roles(pool, cognito, actor_id=owner, staff_id=res.staff_id, booking_role="admin")
    async with pool.acquire() as conn:
        listed = await conn.fetchrow(admin_platform._SELECT + " WHERE u.id = $1", uuid.UUID(res.staff_id))
    assert listed["booking_role"] == "admin" and listed["linked"] is False and listed["email"] == email

    await sd.disable_staff(pool, cognito, actor_id=owner, staff_id=res.staff_id)
    assert await pool.fetchval("SELECT is_active FROM shared.admin_users WHERE id = $1::uuid",
                               res.staff_id) is False
    await sd.enable_staff(pool, cognito, actor_id=owner, staff_id=res.staff_id)
    assert await pool.fetchval("SELECT is_active FROM shared.admin_users WHERE id = $1::uuid",
                               res.staff_id) is True
    actions = await pool.fetch("SELECT action FROM acp_shared.audit_log WHERE resource_type = 'admin_user' "
                               "AND resource_id = $1 ORDER BY created_at", res.staff_id)
    assert [a["action"] for a in actions] == ["staff.created", "staff.roles_changed", "staff.roles_changed",
                                              "staff.roles_changed", "staff.disabled", "staff.enabled"]


@pytest.mark.asyncio
async def test_refused_writes_leave_nothing_behind(env):
    pool, owner, tag, emails = env
    cognito = RecordingCognito()
    # self-disable, self-demote: refused inside the transaction (the owner rows were locked FOR UPDATE first)
    with pytest.raises(sd.StaffDirectoryError):
        await sd.disable_staff(pool, cognito, actor_id=owner, staff_id=owner)
    with pytest.raises(sd.StaffDirectoryError):
        await sd.change_roles(pool, cognito, actor_id=owner, staff_id=owner, role="reviewer")
    assert await pool.fetchval("SELECT is_active AND role = 'admin' FROM shared.admin_users WHERE id = $1::uuid",
                               owner) is True
    # a non-owner actor: nothing is inserted
    email = f"aa816c6.refused.{tag}@adventure.asia"
    emails.append(email)
    await pool.execute("UPDATE shared.admin_users SET is_owner = false WHERE id = $1::uuid", owner)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(pool, cognito, actor_id=owner, email=email, display_name="X", role="content",
                              booking_role="sales")
    assert err.value.status == 403
    assert await pool.fetchval("SELECT count(*) FROM shared.admin_users WHERE lower(email) = $1", email) == 0
    assert cognito.calls == []


@pytest.mark.asyncio
async def test_last_owner_query_counts_only_active_admin_owners(env):
    pool, owner, tag, _ = env
    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetch(sd.SQL_LOCK_OWNERS)
            assert any(str(r["id"]) == owner for r in locked)
            others = await conn.fetchval(sd.SQL_OTHER_ACTIVE_OWNERS, owner)
            mine = await conn.fetchval(
                "SELECT count(*) FROM shared.admin_users WHERE is_owner AND is_active AND role = 'admin'")
    assert mine == others + 1
