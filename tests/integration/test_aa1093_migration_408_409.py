"""AA-1093 lane L2 — CIS 408 (shared.tenant_erasure_request / tenant_erasure_step) and 409 (the feed event type
account.erasure_requested) on real Postgres (conftest replays both).

  * 408: registered; the CHECKs (mode, phase, window, the active / done app sets, fingerprint and approval shapes, step
    app / mode / status / error code, sha256 hash, JSON shapes); a report id once per app; one open request per tenant (a cancelled one does not count); no FK to shared.tenants (the request outlives
    the tenant row); the touch trigger moves txid and updated_at on every update; the grants (cis_app SELECT, INSERT,
    UPDATE, no DELETE, set by 409; nothing for PUBLIC / aa_app_user). 408 is an expand file (plain CREATE FUNCTION /
    TRIGGER): it is applied once, never re-run.
  * 409: registered; shared.tenant_account_emit() accepts account.erasure_requested and still refuses an unknown type;
    a re-run is a no-op.

Invented values only; everything runs in a transaction that is rolled back."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M409 = os.path.join(MIGRATIONS, "409_tenant_account_event_erasure_requested.sql")
REQ = "shared.tenant_erasure_request"
STEP = "shared.tenant_erasure_step"
# Fixed ids (AA-993: never random at import in a parametrize).
TENANT = "10930000-0000-4000-8000-000000000408"
OTHER_TENANT = "10930000-0000-4000-8000-000000000409"


def _body(path: str) -> str:
    sql = open(path, encoding="utf-8").read()
    return sql.replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


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


async def _request(c, tenant=TENANT, **over) -> str:
    row = {"mode": "live", "phase": "dry_run"}
    row.update(over)
    return str(await c.fetchval(
        f"INSERT INTO {REQ} (tenant_id, mode, phase, opened_at, not_before_cis, not_before_booking, "
        "not_before_tripplanner, not_before_crm, created_by) VALUES ($1::uuid, $2, $3, now(), now() + interval '30 days', "
        "now() + interval '30 days', now() + interval '30 days', now() + interval '90 days', 'admin:pytest') "
        "RETURNING request_id", tenant, row["mode"], row["phase"]))


async def _refused(c, sqlstate, sql, *args):
    with pytest.raises(asyncpg.PostgresError) as info:
        async with c.transaction():
            await c.execute(sql, *args)
    assert info.value.sqlstate == sqlstate, info.value


@pytest.mark.asyncio
async def test_408_409_registered_and_409_rerun_is_a_noop(conn):
    for v in ("408", "409"):
        assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = $1", v) == v
    await _request(conn)
    await conn.execute(_body(M409))
    assert await conn.fetchval(f"SELECT count(*) AS n FROM {REQ} WHERE tenant_id = $1::uuid", TENANT) == 1


@pytest.mark.asyncio
async def test_request_checks(conn):
    rid = await _request(conn)
    await _refused(conn, "23514", f"UPDATE {REQ} SET mode = 'test' WHERE request_id = $1::uuid", rid)
    await _refused(conn, "23514", f"UPDATE {REQ} SET phase = 'erased' WHERE request_id = $1::uuid", rid)
    await _refused(conn, "23514", f"UPDATE {REQ} SET not_before_crm = opened_at - interval '1 day' "
                   "WHERE request_id = $1::uuid", rid)
    await _refused(conn, "23514", f"UPDATE {REQ} SET combined_fingerprint = 'abc', fingerprint_at = now() "
                   "WHERE request_id = $1::uuid", rid)
    await _refused(conn, "23514", f"UPDATE {REQ} SET combined_fingerprint = repeat('a', 64) WHERE request_id = $1::uuid",
                   rid)   # fingerprint without its time
    # an approval is all four columns or none
    await _refused(conn, "23514", f"UPDATE {REQ} SET approved_fingerprint = repeat('b', 64), approved_by = 'admin:x' "
                   "WHERE request_id = $1::uuid", rid)
    await conn.execute(f"UPDATE {REQ} SET approved_fingerprint = repeat('b', 64), approved_by = 'admin:x', "
                       "approved_at = now(), approval_expires_at = now() + interval '24 hours' WHERE request_id = $1::uuid",
                       rid)
    await _refused(conn, "23514", f"UPDATE {REQ} SET blockers = '{{}}'::jsonb WHERE request_id = $1::uuid", rid)
    await _refused(conn, "23514", f"UPDATE {REQ} SET done_at = now() WHERE request_id = $1::uuid", rid)
    await _refused(conn, "23514", f"UPDATE {REQ} SET round = 0 WHERE request_id = $1::uuid", rid)
    await _refused(conn, "23514", f"UPDATE {REQ} SET round_apps = '{{cis,partner}}' WHERE request_id = $1::uuid", rid)
    await _refused(conn, "23514", f"UPDATE {REQ} SET round_apps = '{{cis}}', done_apps = '{{cis}}' "
                   "WHERE request_id = $1::uuid", rid)        # an app is active or done, never both
    await conn.execute(f"UPDATE {REQ} SET round_apps = '{{booking,tripplanner}}', done_apps = '{{cis}}' "
                       "WHERE request_id = $1::uuid", rid)


@pytest.mark.asyncio
async def test_one_open_request_per_tenant(conn):
    rid = await _request(conn)
    await _refused(conn, "23505", f"INSERT INTO {REQ} (tenant_id, opened_at, not_before_cis, not_before_booking, "
                   "not_before_tripplanner, not_before_crm, created_by) VALUES ($1::uuid, now(), now(), now(), now(), "
                   "now(), 'admin:pytest')", TENANT)
    await conn.execute(f"UPDATE {REQ} SET phase = 'cancelled', cancelled_at = now() WHERE request_id = $1::uuid",
                       rid)
    second = await _request(conn)
    assert second != rid
    await _request(conn, tenant=OTHER_TENANT)


@pytest.mark.asyncio
async def test_no_foreign_key_to_tenants(conn):
    # the tenant id is not a shared.tenants row at all: the request is the erasure record and outlives the tenant
    assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM shared.tenants WHERE tenant_id = $1::uuid) AS ok",
                                   TENANT)
    await _request(conn)
    fks = await conn.fetch("SELECT confrelid::regclass::text AS ref FROM pg_constraint WHERE contype = 'f' "
                           "AND conrelid IN ($1::regclass, $2::regclass)", REQ, STEP)
    assert sorted(r["ref"] for r in fks) == [REQ]


@pytest.mark.asyncio
async def test_touch_trigger_moves_txid_and_updated_at(conn):
    rid = await _request(conn)
    before = await conn.fetchrow(f"SELECT txid::text AS txid, updated_at FROM {REQ} WHERE request_id = $1::uuid", rid)
    await conn.execute(f"UPDATE {REQ} SET txid = '1'::xid8, updated_at = '2000-01-01' WHERE request_id = $1::uuid",
                       rid)
    after = await conn.fetchrow(f"SELECT txid::text AS txid, updated_at FROM {REQ} WHERE request_id = $1::uuid", rid)
    assert after["txid"] == before["txid"] != "1"          # same transaction, the caller's value is overwritten
    assert after["updated_at"].year > 2000


@pytest.mark.asyncio
async def test_step_checks_and_keys(conn):
    rid = await _request(conn)
    ins = (f"INSERT INTO {STEP} (request_id, round, app, mode, status, plan_hash, report_id, error_code) "
           "VALUES ($1::uuid, 1, $2, $3, $4, $5, $6::uuid, $7)")
    rep = str(uuid.UUID(int=0x1093))
    await conn.execute(ins, rid, "booking", "dry_run", "ok", "a" * 64, rep, None)
    await _refused(conn, "23505", ins, rid, "booking", "dry_run", "ok", "b" * 64, None, None)   # one row per mode
    await _refused(conn, "23505", ins, rid, "booking", "apply", "ok", None, rep, None)          # report id per app
    await conn.execute(ins, rid, "crm", "apply", "ok", None, rep, None)                          # another app may
    await conn.execute(ins, rid, "tripplanner", "apply", "refused", "c" * 64, None, "plan_changed")
    await conn.execute(ins, rid, "cis", "dry_run", "pending", None, None, None)
    await conn.execute(ins, rid, "crm", "dry_run", "app_not_ready", None, None, None)
    await _refused(conn, "23514", ins, rid, "partner", "verify", "ok", None, None, None)
    await _refused(conn, "23514", ins, rid, "cis", "erase", "ok", None, None, None)
    await _refused(conn, "23514", ins, rid, "cis", "verify", "done", None, None, None)
    await _refused(conn, "23514", ins, rid, "booking", "verify", "ok", "a" * 32, None, None)   # sha256 only
    await _refused(conn, "23514", ins, rid, "booking", "verify", "failed", None, None, "Plan Changed")
    await _refused(conn, "23503", ins, str(uuid.UUID(int=1093)), "cis", "verify", "ok", None, None, None)
    await _refused(conn, "23514", f"UPDATE {STEP} SET tables = '{{}}'::jsonb WHERE request_id = $1::uuid", rid)
    await _refused(conn, "23514", f"UPDATE {STEP} SET census = '[]'::jsonb WHERE request_id = $1::uuid", rid)


@pytest.mark.asyncio
async def test_grants_are_exact(conn):
    rows = await conn.fetch("""
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END AS r, c.relname AS o,
               a.privilege_type AS p
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.oid IN ($1::regclass, $2::regclass)
           AND (a.grantee = 0 OR a.grantee IN ('cis_app'::regrole, 'aa_app_user'::regrole))""", REQ, STEP)
    got = sorted((r["r"], r["o"], r["p"]) for r in rows)
    want = sorted((role, t, p) for role in ("cis_app",) for t in ("tenant_erasure_request", "tenant_erasure_step")
                  for p in ("INSERT", "SELECT", "UPDATE"))
    assert got == want


@pytest.mark.asyncio
async def test_409_feed_accepts_erasure_requested(conn):
    tid = TENANT
    await conn.execute("SELECT shared.tenant_account_emit('account.erasure_requested', $1::uuid, $2::jsonb)", tid,
                       json.dumps({"request_id": str(uuid.UUID(int=408)), "status": "scheduled"}))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_account_event WHERE tenant_id = $1::uuid "
                               "AND type = 'account.erasure_requested'", tid) == 1
    await _refused(conn, "23514", "SELECT shared.tenant_account_emit('account.erased', $1::uuid, '{}'::jsonb)", tid)
    con = await conn.fetchval("SELECT pg_get_constraintdef(oid) AS ok FROM pg_constraint "
                              "WHERE conname = 'tenant_account_event_type_check'")
    assert "account.erasure_requested" in con and "NOT VALID" not in con
