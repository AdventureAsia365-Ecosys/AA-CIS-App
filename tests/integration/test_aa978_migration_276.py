"""AA-978 (CRM-P1) — CIS 276 (shared.tenant_members, shared.tenant_membership_event and the feed triggers on
shared.tenants / shared.tenant_members) on real Postgres.

conftest replays 276 like every file. Each test runs in one transaction that is rolled back, apart from the
two-connection horizon test, which commits offboarded synthetic tenants and deletes them at the end.

  * the file applied and a re-run is a no-op; the grants it leaves are exact (cis_app only; nothing for PUBLIC,
    aa_app_user or catalog_owner, nothing on the identity sequence), also after a stray grant, and the AA-859
    invariant v3 finds nothing on these objects; aa_app_user is refused on both tables (42501);
  * guard 1 refuses a re-run over a table with other columns; guard 2 refuses a role that may write shared.tenants
    but not insert an event (the triggers run as the writer, SCHEDULE S250 §5); standing: every writer of
    shared.tenants can write the feed;
  * as cis_app, with the SQL of every api/routers/admin.py write path (create, PATCH is_active, offboard, restore)
    and the member writes: the right event per path, in order, with the contract Membership shape; a plan / quota
    change writes nothing;
  * the guards: a direct event insert, an event update / truncate, the delete of a fresh event or of a live member,
    a removed membership coming back, a rebinding of the identity are refused; the 90-day and offboarded-tenant
    deletes pass; the CHECKs and the partial unique index;
  * the feed horizon (contracts rule 4): an event of a transaction still open hides every later-txid event, and once
    it commits it sorts before them (ordering by (txid, seq), never skipped).
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import NO_PRIVILEGE_WITHOUT_RLS_SQL_V3, migration_body  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MEMBERS = "shared.tenant_members"
EVENTS = "shared.tenant_membership_event"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_zz978partner"
MEMBERSHIP_KEYS = ["content_studio_role", "identity", "invitation_id", "membership_id", "status", "tenant_id",
                   "updated_at"]
HORIZON = "pg_snapshot_xmin(pg_current_snapshot())"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, *, active=True, offboarded=False) -> uuid.UUID:
    """The INSERT of POST /admin/tenants (api/routers/admin.py), plus an offboarded variant for committed tests."""
    tid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, api_key_hash, rate_limit_rpm, "
        "is_active, cancelled_at) VALUES ($1, 'AA-978 synthetic', $2, 'starter', 3, NULL, 60, $3, "
        "CASE WHEN $4 THEN now() END)", tid, f"zz978-{tid.hex[:12]}", active, offboarded)
    return tid


async def _types(conn, tid) -> list[str]:
    return [r["type"] for r in await conn.fetch(f"SELECT type FROM {EVENTS} WHERE tenant_id = $1 ORDER BY txid, seq",
                                                tid)]


async def _as_runtime(conn):
    """Log in as the CIS runtime: re-apply 276 (other modules re-create cis_app, which drops its grants), give it the
    schema USAGE and the shared.tenants DML it has on Dev (the replay skips 244), then SET ROLE."""
    await conn.execute(migration_body(276))
    await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await conn.execute("GRANT SELECT, INSERT, UPDATE ON shared.tenants TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")


async def _refused(conn, exc, sql, *args):
    with pytest.raises(exc):
        async with conn.transaction():
            await conn.execute(sql, *args)


async def _member(conn, tid, *, status="active", role="editor", sub=None, invitation=None) -> uuid.UUID:
    iss, sub = (None, None) if status == "invited" else (ISS, sub or f"sub-{uuid.uuid4().hex[:12]}")
    return await conn.fetchval(
        f"INSERT INTO {MEMBERS} (tenant_id, cognito_iss, cognito_sub, content_studio_role, status, invitation_id, "
        "created_by) VALUES ($1, $2, $3, $4, $5, $6, 'admin:test') RETURNING membership_id",
        tid, iss, sub, role, status, invitation or (uuid.uuid4() if status == "invited" else None))


# ── the file ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_276_applied_and_rerun_is_a_noop(conn):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '276'"), \
        "conftest swallowed an error in 276 (see its log)"
    before = await conn.fetchval(f"SELECT count(*) FROM {EVENTS}")
    await conn.execute(migration_body(276))
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '276'") == 1
    assert await conn.fetchval(f"SELECT count(*) FROM {EVENTS}") == before        # the probe was rolled back


@pytest.mark.asyncio
async def test_276_privileges_are_exact(conn):
    await conn.execute(f"GRANT SELECT ON {MEMBERS}, {EVENTS} TO aa_app_user, catalog_owner, PUBLIC")
    await conn.execute(f"GRANT UPDATE, TRUNCATE ON {EVENTS} TO cis_app")
    await conn.execute("GRANT USAGE, UPDATE ON SEQUENCE shared.tenant_membership_event_seq_seq TO cis_app")
    await conn.execute(migration_body(276))
    rows = await conn.fetch("""
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END AS r, c.relname AS o,
               a.privilege_type AS p
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.oid IN ('shared.tenant_members'::regclass, 'shared.tenant_membership_event'::regclass,
                         'shared.tenant_membership_event_seq_seq'::regclass)
           AND (a.grantee = 0 OR a.grantee IN ('cis_app'::regrole, 'aa_app_user'::regrole,
                                               'catalog_owner'::regrole))""")
    got = sorted((r["r"], r["o"], r["p"]) for r in rows)
    assert got == sorted([
        ("cis_app", "tenant_members", "SELECT"), ("cis_app", "tenant_members", "INSERT"),
        ("cis_app", "tenant_members", "UPDATE"), ("cis_app", "tenant_members", "DELETE"),
        ("cis_app", "tenant_membership_event", "SELECT"), ("cis_app", "tenant_membership_event", "INSERT"),
        ("cis_app", "tenant_membership_event", "DELETE"),
    ])
    bad = [r for r in await conn.fetch(NO_PRIVILEGE_WITHOUT_RLS_SQL_V3) if "tenant_member" in r["rel"]]
    assert bad == []


@pytest.mark.asyncio
async def test_the_tenant_pool_is_refused_on_both_tables(conn):
    await conn.execute(migration_body(276))
    await conn.execute("GRANT USAGE ON SCHEMA shared TO aa_app_user")   # the refusal is the table's, not the schema's
    await conn.execute("SET LOCAL ROLE aa_app_user")
    for table in (MEMBERS, EVENTS):
        await _refused(conn, asyncpg.InsufficientPrivilegeError, f"SELECT count(*) FROM {table}")


@pytest.mark.asyncio
async def test_276_guard_refuses_a_table_with_other_columns(conn):
    await conn.execute(f"ALTER TABLE {MEMBERS} ADD COLUMN email text")
    with pytest.raises(asyncpg.RaiseError, match="exists with other columns"):
        async with conn.transaction():
            await conn.execute(migration_body(276))


@pytest.mark.asyncio
async def test_276_guard_refuses_a_tenants_writer_that_cannot_write_the_feed(conn):
    """SCHEDULE S250 §5 (trigger privileges): the triggers are not SECURITY DEFINER (MIG-6), so a new writer of
    shared.tenants without the event grant would fail every tenant write. The file refuses that state."""
    await conn.execute("CREATE ROLE zz978_writer NOLOGIN")
    await conn.execute("GRANT UPDATE (is_active) ON shared.tenants TO zz978_writer")
    with pytest.raises(asyncpg.RaiseError, match="zz978_writer may write shared.tenants but not insert"):
        async with conn.transaction():
            await conn.execute(migration_body(276))
    await conn.execute("REVOKE UPDATE (is_active) ON shared.tenants FROM zz978_writer")
    await conn.execute("GRANT INSERT ON shared.tenants TO zz978_writer")
    with pytest.raises(asyncpg.RaiseError, match="zz978_writer may write shared.tenants but not insert"):
        async with conn.transaction():
            await conn.execute(migration_body(276))


@pytest.mark.asyncio
async def test_standing_every_tenants_writer_can_write_the_feed(conn):
    rows = await conn.fetch("""
        SELECT DISTINCT a.grantee::regrole::text AS role
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.oid = 'shared.tenants'::regclass AND a.grantee NOT IN (0, c.relowner)
           AND a.privilege_type IN ('INSERT', 'UPDATE')
           AND NOT has_table_privilege(a.grantee, 'shared.tenant_membership_event', 'INSERT')""")
    assert [r["role"] for r in rows] == []


@pytest.mark.asyncio
async def test_functions_are_invoker_with_a_pinned_search_path(conn):
    rows = await conn.fetch("""
        SELECT p.proname, p.prosecdef, p.proconfig, has_function_privilege('public', p.oid, 'EXECUTE') AS pub
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'shared' AND (p.proname LIKE 'tenant_membership%' OR p.proname = 'tenant_members_guard')""")
    assert len(rows) == 4
    for r in rows:
        assert r["prosecdef"] is False and r["pub"] is False, r["proname"]
        assert "search_path=pg_catalog, pg_temp" in r["proconfig"], r["proname"]


# ── the write paths of api/routers/admin.py, as cis_app ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_tenant_write_path_emits_its_event(conn):
    await _as_runtime(conn)
    tid = await _tenant(conn)
    # PATCH plan / rate limit / publish limit / planning config: no event
    await conn.execute("UPDATE shared.tenants SET rate_limit_rpm = 120, updated_at = NOW() WHERE tenant_id = $1", tid)
    await conn.execute("UPDATE shared.tenants SET posts_per_week = 5 WHERE tenant_id = $1", tid)
    await conn.execute("UPDATE shared.tenants SET is_active = true, updated_at = NOW() WHERE tenant_id = $1", tid)
    assert await _types(conn, tid) == ["tenant.created"]
    # PATCH is_active
    await conn.execute("UPDATE shared.tenants SET is_active = $2, updated_at = NOW() WHERE tenant_id = $1", tid, False)
    await conn.execute("UPDATE shared.tenants SET is_active = $2, updated_at = NOW() WHERE tenant_id = $1", tid, True)
    # offboard (from locked or active), then restore
    await conn.execute("""
        UPDATE shared.tenants
        SET cancelled_at = NOW(), cancellation_reason = $2, is_active = FALSE,
            api_key_hash = 'REVOKED_' || tenant_id::text, updated_at = NOW()
        WHERE tenant_id = $1::uuid""", str(tid), "test")
    await conn.execute("""
        UPDATE shared.tenants
        SET cancelled_at = NULL, cancellation_reason = NULL, is_active = TRUE, api_key_hash = $2, updated_at = NOW()
        WHERE tenant_id = $1""", tid, "x" * 64)
    assert await _types(conn, tid) == ["tenant.created", "tenant.locked", "tenant.unlocked", "tenant.deleted",
                                       "tenant.unlocked"]
    rows = await conn.fetch(f"SELECT event_id, membership, occurred_at FROM {EVENTS} WHERE tenant_id = $1", tid)
    assert all(r["membership"] is None and r["event_id"] and r["occurred_at"] for r in rows)
    assert len({r["event_id"] for r in rows}) == 5


@pytest.mark.asyncio
async def test_a_tenant_created_locked_or_offboarded_says_so(conn):
    locked = await _tenant(conn, active=False)
    gone = await _tenant(conn, offboarded=True)
    assert await _types(conn, locked) == ["tenant.created", "tenant.locked"]
    assert await _types(conn, gone) == ["tenant.created", "tenant.deleted"]


@pytest.mark.asyncio
async def test_member_writes_emit_the_contract_membership(conn):
    await _as_runtime(conn)
    tid = await _tenant(conn)
    invitation = uuid.uuid4()
    mid = await _member(conn, tid, status="invited", invitation=invitation)
    await conn.execute(f"UPDATE {MEMBERS} SET status = 'active', cognito_iss = $2, cognito_sub = 'sub-1', "
                       "updated_at = now() WHERE membership_id = $1", mid, ISS)
    await conn.execute(f"UPDATE {MEMBERS} SET updated_at = now() WHERE membership_id = $1", mid)        # no event
    await conn.execute(f"UPDATE {MEMBERS} SET content_studio_role = 'owner', updated_at = now() "
                       "WHERE membership_id = $1", mid)
    await conn.execute(f"UPDATE {MEMBERS} SET status = 'removed', removed_at = now(), removed_by = 'admin:test', "
                       "updated_at = now() WHERE membership_id = $1", mid)
    rows = await conn.fetch(f"SELECT type, membership FROM {EVENTS} WHERE tenant_id = $1 AND type LIKE 'membership.%' "
                            "ORDER BY txid, seq", tid)
    assert [r["type"] for r in rows] == ["membership.invited", "membership.activated", "membership.role_changed",
                                         "membership.removed"]
    docs = [json.loads(r["membership"]) for r in rows]
    for doc in docs:
        assert sorted(doc) == MEMBERSHIP_KEYS
        assert doc["membership_id"] == str(mid) and doc["tenant_id"] == str(tid)
        assert doc["invitation_id"] == str(invitation)
        assert doc["updated_at"].endswith("Z") and "T" in doc["updated_at"]
    assert docs[0]["identity"] is None and docs[0]["status"] == "invited"
    assert docs[1]["identity"] == {"iss": ISS, "sub": "sub-1"} and docs[1]["content_studio_role"] == "editor"
    assert docs[2]["content_studio_role"] == "owner" and docs[2]["status"] == "active"
    assert docs[3]["status"] == "removed" and docs[3]["identity"] == {"iss": ISS, "sub": "sub-1"}
    # the identity sequence needs no grant: cis_app inserted every event through the triggers
    assert await conn.fetchval("SELECT current_user") == "cis_app"


# ── the guards ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_table_is_append_only(conn):
    await _as_runtime(conn)
    tid = await _tenant(conn)
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"INSERT INTO {EVENTS} (type, tenant_id) VALUES ('tenant.locked', $1)", tid)
    await _refused(conn, asyncpg.InsufficientPrivilegeError,                 # cis_app holds no UPDATE / TRUNCATE
                   f"UPDATE {EVENTS} SET type = 'tenant.deleted' WHERE tenant_id = $1", tid)
    await _refused(conn, asyncpg.InsufficientPrivilegeError, f"TRUNCATE {EVENTS}")
    await _refused(conn, asyncpg.RestrictViolationError, f"DELETE FROM {EVENTS} WHERE tenant_id = $1", tid)
    await conn.execute("RESET ROLE")                                        # the owner: the trigger refuses
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"UPDATE {EVENTS} SET type = 'tenant.deleted' WHERE tenant_id = $1", tid)
    await _refused(conn, asyncpg.RestrictViolationError, f"TRUNCATE {EVENTS}")
    await _refused(conn, asyncpg.RestrictViolationError, f"DELETE FROM {EVENTS} WHERE tenant_id = $1", tid)


@pytest.mark.asyncio
async def test_deletes_only_after_retention_or_offboarding(conn):
    await _as_runtime(conn)
    live, gone = await _tenant(conn), await _tenant(conn)
    keep = await _member(conn, live)
    old = await _member(conn, live)
    await conn.execute(f"UPDATE {MEMBERS} SET status = 'removed', removed_at = now() - interval '91 days', "
                       "removed_by = 'admin:test' WHERE membership_id = $1", old)
    fresh = await _member(conn, live)
    await conn.execute(f"UPDATE {MEMBERS} SET status = 'removed', removed_at = now(), removed_by = 'admin:test' "
                       "WHERE membership_id = $1", fresh)
    await _refused(conn, asyncpg.RestrictViolationError, f"DELETE FROM {MEMBERS} WHERE membership_id = $1", keep)
    await _refused(conn, asyncpg.RestrictViolationError, f"DELETE FROM {MEMBERS} WHERE membership_id = $1", fresh)
    assert await conn.execute(f"DELETE FROM {MEMBERS} WHERE membership_id = $1", old) == "DELETE 1"
    # the offboarded tenant: every row goes (AA-839 erasure), events too
    await _member(conn, gone)
    await conn.execute("UPDATE shared.tenants SET cancelled_at = now(), is_active = false WHERE tenant_id = $1", gone)
    assert await conn.execute(f"DELETE FROM {MEMBERS} WHERE tenant_id = $1", gone) == "DELETE 1"
    assert await conn.execute(f"DELETE FROM {EVENTS} WHERE tenant_id = $1", gone) == "DELETE 3"
    # an event older than 90 days of a live tenant (backdated as the owner: the guard reads occurred_at only)
    await conn.execute("RESET ROLE")
    await conn.execute("ALTER TABLE shared.tenant_membership_event DISABLE TRIGGER tenant_membership_event_guard")
    await conn.execute(f"UPDATE {EVENTS} SET occurred_at = now() - interval '91 days' "
                       "WHERE tenant_id = $1 AND type = 'tenant.created'", live)
    await conn.execute("ALTER TABLE shared.tenant_membership_event ENABLE TRIGGER tenant_membership_event_guard")
    assert await conn.execute(f"DELETE FROM {EVENTS} WHERE tenant_id = $1 AND type = 'tenant.created'",
                              live) == "DELETE 1"


@pytest.mark.asyncio
async def test_membership_lifecycle_rules(conn):
    tid = await _tenant(conn)
    mid = await _member(conn, tid, sub="sub-a")
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"UPDATE {MEMBERS} SET status = 'invited', cognito_iss = NULL, cognito_sub = NULL, "
                   "invitation_id = gen_random_uuid() WHERE membership_id = $1", mid)
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"UPDATE {MEMBERS} SET cognito_sub = 'sub-b' WHERE membership_id = $1", mid)
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"UPDATE {MEMBERS} SET tenant_id = $2 WHERE membership_id = $1", mid, await _tenant(conn))
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"INSERT INTO {MEMBERS} (tenant_id, cognito_iss, cognito_sub, content_studio_role, status, "
                   "removed_at, removed_by, created_by) VALUES ($1, $2, 'sub-z', 'viewer', 'removed', now(), 'a', 'a')",
                   tid, ISS)
    # one live row per identity and agency; after removal the person may be added again (a new row)
    await _refused(conn, asyncpg.UniqueViolationError,
                   f"INSERT INTO {MEMBERS} (tenant_id, cognito_iss, cognito_sub, content_studio_role, status, "
                   "created_by) VALUES ($1, $2, 'sub-a', 'viewer', 'active', 'a')", tid, ISS)
    await conn.execute(f"UPDATE {MEMBERS} SET status = 'removed', removed_at = now(), removed_by = 'a' "
                       "WHERE membership_id = $1", mid)
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"UPDATE {MEMBERS} SET status = 'active', removed_at = NULL, removed_by = NULL "
                   "WHERE membership_id = $1", mid)
    await _member(conn, tid, sub="sub-a")
    # the same identity in another agency is a separate membership
    await _member(conn, await _tenant(conn), sub="sub-a")


@pytest.mark.asyncio
@pytest.mark.parametrize("columns, values", [
    ("content_studio_role, status, created_by", "'agent', 'active', 'a'"),                  # a CRM role
    ("content_studio_role, status, created_by", "'owner', 'pending', 'a'"),
    ("content_studio_role, status, created_by", "'owner', 'active', 'a'"),                  # active without identity
    ("cognito_iss, cognito_sub, content_studio_role, status, invitation_id, created_by",
     f"'{ISS}', 's', 'owner', 'invited', gen_random_uuid(), 'a'"),                          # invited with identity
    ("content_studio_role, status, created_by", "'owner', 'invited', 'a'"),                 # invited without invitation
    ("cognito_iss, cognito_sub, content_studio_role, status, created_by",
     "'http://not-https.example', 's', 'owner', 'active', 'a'"),
    ("cognito_iss, cognito_sub, content_studio_role, status, created_by", f"'{ISS}', NULL, 'owner', 'active', 'a'"),
    ("cognito_iss, cognito_sub, content_studio_role, status, created_by",
     f"'{ISS}', repeat('s', 129), 'owner', 'active', 'a'"),
    ("cognito_iss, cognito_sub, content_studio_role, status, created_by", f"'{ISS}', 's', 'owner', 'active', ''"),
])
async def test_checks_refuse(conn, columns, values):
    tid = await _tenant(conn)
    await _refused(conn, asyncpg.CheckViolationError,
                   f"INSERT INTO {MEMBERS} (tenant_id, {columns}) VALUES ($1, {values})", tid)


@pytest.mark.asyncio
async def test_removed_needs_its_time_and_actor(conn):
    tid = await _tenant(conn)
    mid = await _member(conn, tid)
    await _refused(conn, asyncpg.CheckViolationError,
                   f"UPDATE {MEMBERS} SET status = 'removed' WHERE membership_id = $1", mid)
    await _refused(conn, asyncpg.CheckViolationError,
                   f"UPDATE {MEMBERS} SET status = 'removed', removed_at = now() WHERE membership_id = $1", mid)


# ── the feed horizon (two connections, committed rows) ──────────────────────────────────────────────────────────────

FEED_SQL = (f"SELECT tenant_id, type, seq FROM {EVENTS} WHERE txid < {HORIZON} AND tenant_id = ANY($1::uuid[]) "
            "ORDER BY txid, seq")


@pytest.mark.asyncio
async def test_an_open_transaction_holds_back_every_later_event_and_is_never_skipped():
    """contracts rule 4: A takes its xid first and writes its event late (higher seq); B commits in between. While A
    is open the feed serves neither; after A commits, A's events come first ((txid, seq) order), so a consumer whose
    cursor passed B's seq would still get A's (ordering by seq alone would have skipped them)."""
    a, b, reader = [await asyncpg.connect(DSN) for _ in range(3)]
    ta = tb = None
    try:
        ta, tb = uuid.uuid4(), uuid.uuid4()
        tx = a.transaction()
        await tx.start()
        await a.fetchval("SELECT pg_current_xact_id()")                      # A's xid, before B's
        async with b.transaction():
            await b.execute("SELECT pg_current_xact_id()")
            await _tenant_with_id(b, tb)                                     # B: higher txid, lower seq
        await _tenant_with_id(a, ta)                                         # A: higher seq, lower txid
        assert await reader.fetch(FEED_SQL, [ta, tb]) == []                  # A open: B held back too
        await tx.commit()
        rows = await reader.fetch(FEED_SQL, [ta, tb])
        assert [(r["tenant_id"], r["type"]) for r in rows] == [
            (ta, "tenant.created"), (ta, "tenant.deleted"), (tb, "tenant.created"), (tb, "tenant.deleted")]
        seqs = {r["tenant_id"]: r["seq"] for r in await reader.fetch(
            f"SELECT tenant_id, min(seq) AS seq FROM {EVENTS} WHERE tenant_id = ANY($1::uuid[]) GROUP BY 1", [ta, tb])}
        assert seqs[ta] > seqs[tb]                                           # the case seq order would skip
    finally:
        for c in (a, b):
            if not c.is_closed():
                await c.close()
        await reader.execute(f"DELETE FROM {EVENTS} WHERE tenant_id = ANY($1::uuid[])", [ta, tb])
        await reader.execute("DELETE FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])", [ta, tb])
        await reader.close()


async def _tenant_with_id(conn, tid):
    await conn.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active, "
        "cancelled_at) VALUES ($1, 'AA-978 horizon', $2, 'starter', 3, 60, false, now())",
        tid, f"zz978h-{tid.hex[:12]}")
