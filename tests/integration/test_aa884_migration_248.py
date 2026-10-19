"""AA-884 — CIS migration 248 against real Postgres (conftest applies every api/migrations file).

conftest swallows migration errors (lessons.md "Integration test xanh…"), so the first test proves 248 really
applied: it is registered and every object it creates exists. Then the behaviour:
  * the BEFORE INSERT chain trigger: consecutive chain_seq, prev_hash = the previous row_hash, and a Python
    recompute of row_hash from the stored columns equals the SQL one (the verifier in PR C relies on it);
  * concurrent inserts from separate connections still give a contiguous, valid chain (advisory lock);
  * a rolled-back insert leaves no gap (no sequence);
  * UPDATE, DELETE and TRUNCATE raise 42501 and change nothing;
  * the enum has staff / machine, job_worker.git_sha and shared.cost_explorer_app_snapshot exist, the new
    table is not readable by aa_app_user.
"""
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import timezone

import asyncpg
import pytest
import pytest_asyncio
from _audit_cleanup import delete_audit_rows

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

_ROW_SQL = """
    SELECT id, tenant_id, actor, actor_type::text AS actor_type, action, resource_type, resource_id,
           details::text AS details_text, created_at, http_method, route, status, chain_seq, prev_hash, row_hash
      FROM acp_shared.audit_log WHERE id = $1
"""


def canonical_hash(r) -> str:
    """The canonical text of migration 248 (acp_shared.audit_log_chain), recomputed in Python."""
    created = r["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    parts = [r["prev_hash"] or "", str(r["id"]), str(r["chain_seq"]), r["tenant_id"] or "", r["actor"] or "",
             r["actor_type"] or "", r["action"] or "", r["resource_type"] or "", r["resource_id"] or "",
             r["details_text"] or "", created, r["http_method"] or "", r["route"] or "",
             "" if r["status"] is None else str(r["status"])]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


async def _insert(conn, tag: str, **extra) -> uuid.UUID:
    cols = {"tenant_id": None, "actor": f"test:{tag}", "action": "aa884.test", "resource_type": "aa884_test",
            "resource_id": tag, "details": json.dumps({"tag": tag, "b": [1, 2], "a": "x|y"})}
    cols.update(extra)
    names = list(cols)
    casts = ["$%d::jsonb" % (i + 1) if n == "details" else "$%d" % (i + 1) for i, n in enumerate(names)]
    return await conn.fetchval(
        f"INSERT INTO acp_shared.audit_log ({', '.join(names)}) VALUES ({', '.join(casts)}) RETURNING id",
        *cols.values())


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=6)
    tag = uuid.uuid4().hex[:8]
    yield p, tag
    await delete_audit_rows(p, "resource_type = 'aa884_test' AND resource_id LIKE $1", f"{tag}%")
    await p.close()


@pytest.mark.asyncio
async def test_248_applied_and_objects_exist(pool):
    p, _ = pool
    assert await p.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '248'") == 1
    cols = {(r["table_name"], r["column_name"]): r["data_type"] for r in await p.fetch("""
        SELECT table_name, column_name, data_type FROM information_schema.columns
         WHERE (table_schema, table_name) IN (('acp_shared', 'audit_log'), ('shared', 'job_worker'),
                                              ('shared', 'cost_explorer_app_snapshot'))""")}
    for c, t in (("http_method", "text"), ("route", "text"), ("status", "smallint"), ("chain_seq", "bigint"),
                 ("prev_hash", "text"), ("row_hash", "text")):
        assert cols[("audit_log", c)] == t
    assert cols[("job_worker", "git_sha")] == "text"
    for c in ("account_id", "app_tag", "service", "period_start", "period_end", "amount_usd", "unit", "fetched_at"):
        assert ("cost_explorer_app_snapshot", c) in cols
    labels = await p.fetchval("""
        SELECT array_agg(e.enumlabel::text) FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
          JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = 'acp_shared' AND t.typname = 'audit_actor_type'""")
    assert {"staff", "machine", "hitl_reviewer", "tenant_admin", "tenant_reviewer"} <= set(labels)
    triggers = {r["tgname"] for r in await p.fetch(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'acp_shared.audit_log'::regclass AND NOT tgisinternal")}
    assert triggers == {"audit_log_chain", "audit_log_no_update", "audit_log_no_delete", "audit_log_no_truncate"}
    assert await p.fetchval(
        "SELECT has_table_privilege('aa_app_user', 'shared.cost_explorer_app_snapshot', 'SELECT')") is False


@pytest.mark.asyncio
async def test_chain_links_and_python_recompute(pool):
    p, tag = pool
    async with p.acquire() as conn:
        a = await _insert(conn, f"{tag}-a")
        b = await _insert(conn, f"{tag}-b", actor_type="staff", http_method="POST",
                          route="/admin/tenants/{tenant_id}", status=200, tenant_id="t-1")
        c = await _insert(conn, f"{tag}-c", actor_type="machine", details=None)
        ra, rb, rc = [await conn.fetchrow(_ROW_SQL, i) for i in (a, b, c)]
    assert rb["chain_seq"] == ra["chain_seq"] + 1 and rc["chain_seq"] == rb["chain_seq"] + 1
    assert rb["prev_hash"] == ra["row_hash"] and rc["prev_hash"] == rb["row_hash"]
    for r in (ra, rb, rc):
        assert r["row_hash"] == canonical_hash(r), r["chain_seq"]
    assert rb["actor_type"] == "staff" and rb["status"] == 200


@pytest.mark.asyncio
async def test_bad_http_method_refused(pool):
    p, tag = pool
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(p, f"{tag}-get", http_method="GET")


@pytest.mark.asyncio
async def test_rolled_back_insert_leaves_no_gap(pool):
    p, tag = pool
    async with p.acquire() as conn:
        first = await conn.fetchrow(_ROW_SQL, await _insert(conn, f"{tag}-1"))
        tx = conn.transaction()
        await tx.start()
        await _insert(conn, f"{tag}-rolled")
        await tx.rollback()
        second = await conn.fetchrow(_ROW_SQL, await _insert(conn, f"{tag}-2"))
    assert second["chain_seq"] == first["chain_seq"] + 1
    assert second["prev_hash"] == first["row_hash"]


@pytest.mark.asyncio
async def test_concurrent_inserts_keep_one_chain(pool):
    p, tag = pool
    n = 12

    async def one(i):
        async with p.acquire() as conn:
            async with conn.transaction():
                rid = await _insert(conn, f"{tag}-c{i:02d}")
                await asyncio.sleep(0.01)  # hold the lock a little so the others queue behind it
                return rid

    await asyncio.gather(*(one(i) for i in range(n)))
    rows = await p.fetch(
        "SELECT id, tenant_id, actor, actor_type::text AS actor_type, action, resource_type, resource_id, "
        "details::text AS details_text, created_at, http_method, route, status, chain_seq, prev_hash, row_hash "
        "FROM acp_shared.audit_log WHERE resource_type = 'aa884_test' AND resource_id LIKE $1 ORDER BY chain_seq",
        f"{tag}-c%")
    assert len(rows) == n
    seqs = [r["chain_seq"] for r in rows]
    assert seqs == list(range(seqs[0], seqs[0] + n))
    for prev, cur in zip(rows, rows[1:]):
        assert cur["prev_hash"] == prev["row_hash"]
    assert all(r["row_hash"] == canonical_hash(r) for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("stmt", [
    "UPDATE acp_shared.audit_log SET action = action WHERE id = $1",
    "DELETE FROM acp_shared.audit_log WHERE id = $1",
])
async def test_update_and_delete_refused(pool, stmt):
    p, tag = pool
    rid = await _insert(p, f"{tag}-x")
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
        await p.execute(stmt, rid)
    assert await p.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE id = $1", rid) == 1


@pytest.mark.asyncio
async def test_truncate_refused(pool):
    p, tag = pool
    rid = await _insert(p, f"{tag}-t")
    async with p.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
                await conn.execute("TRUNCATE acp_shared.audit_log")
        finally:
            await tx.rollback()
    assert await p.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE id = $1", rid) == 1


@pytest.mark.asyncio
async def test_cleanup_helper_bypasses_triggers_only_in_its_transaction(pool):
    p, tag = pool
    rid = await _insert(p, f"{tag}-cleanup")
    assert await delete_audit_rows(p, "id = $1", rid) == 1
    # The bypass was SET LOCAL: a plain DELETE afterwards is refused again.
    rid2 = await _insert(p, f"{tag}-cleanup2")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await p.execute("DELETE FROM acp_shared.audit_log WHERE id = $1", rid2)


@pytest.mark.asyncio
async def test_cost_explorer_app_snapshot_insert(pool):
    p, tag = pool
    acct = f"aa884-{tag}"
    try:
        await p.execute("""
            INSERT INTO shared.cost_explorer_app_snapshot
                (account_id, app_tag, service, period_start, period_end, amount_usd, unit)
            VALUES ($1, '', 'Amazon Relational Database Service', '2026-10-01', '2026-10-02', 1.23456, 'USD')""",
                        acct)
        row = await p.fetchrow("SELECT app_tag, amount_usd, fetched_at FROM shared.cost_explorer_app_snapshot "
                               "WHERE account_id = $1", acct)
        assert row["app_tag"] == "" and str(row["amount_usd"]) == "1.2346" and row["fetched_at"] is not None
    finally:
        await p.execute("DELETE FROM shared.cost_explorer_app_snapshot WHERE account_id = $1", acct)


@pytest.mark.asyncio
async def test_job_worker_git_sha_column_writable(pool):
    p, tag = pool
    wid = f"aa884-{tag}"
    try:
        await p.execute("""
            INSERT INTO shared.job_worker (worker_id, host, started_at, last_seen_at, max_parallel, caps,
                                           running_jobs, reaped_requeued, reaped_failed, git_sha)
            VALUES ($1, 'test', now(), now(), 1, '{}'::jsonb, 0, 0, 0, 'abc123')""", wid)
        assert await p.fetchval("SELECT git_sha FROM shared.job_worker WHERE worker_id = $1", wid) == "abc123"
    finally:
        await p.execute("DELETE FROM shared.job_worker WHERE worker_id = $1", wid)
