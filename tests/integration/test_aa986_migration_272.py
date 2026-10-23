"""AA-986 L2 — CIS 272 (tour lifecycle record, Master status reason, gold_aa_internal.v_tour_liveness) on real
Postgres.

conftest replays 272 like every file. Each test runs in one transaction that is rolled back.

  * the file applied and a re-run is a no-op; the grants it leaves (cis_app INSERT + SELECT on the record,
    catalog_owner SELECT, the tenant pool SELECT on the view only), the record is append-only (trigger), the reason
    CHECK refuses an unknown value;
  * guard 4 (AA-859 invariant v3) refuses a tenant-pool privilege on another relation without RLS;
  * the view's state / reason for every shape of key (served, paused master_inactive / superseded_pending, retired
    master_trashed / source_deleted / superseded_orphan, never served = no row) and its served keys = the
    catalog._live_tour rule.
"""
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"


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


async def _tour(conn, label, *, group=None, version=1, source_status="active", master="active", deleted=False):
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "source_status, deleted_at, source_group_id, source_version) "
        "VALUES ($1, $2, $3, $4, 'Vietnam', '3 days', $5::silver_aa_internal.source_status_enum, "
        "CASE WHEN $6 THEN now() END, $7, $8)",
        tour_id, MASTER, BATCH_ID, f"aa986 {label}", source_status, deleted, group or uuid.uuid4(), version)
    if master:
        await conn.execute(
            "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
            "aa_summary, aa_highlights, master_status) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, $5)",
            tour_id, uuid.uuid4(), MASTER, f"aa986 {label}", master)
    group_id = await conn.fetchval("SELECT source_group_id FROM silver_aa_internal.raw_tours WHERE tour_id = $1",
                                   tour_id)
    return SimpleNamespace(tour=str(tour_id), group=str(group_id))


async def _state(conn, t):
    return tuple(await conn.fetchrow("SELECT state, reason FROM gold_aa_internal.v_tour_liveness WHERE source_id = $1",
                                     t.group) or (None, None))


# ── the file ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_272_applied_and_rerun_is_a_noop(conn):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '272'"), \
        "conftest swallowed an error in 272 (see its log)"
    await conn.execute(migration_body(272))
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '272'") == 1


@pytest.mark.asyncio
async def test_272_privileges_and_append_only(conn):
    # Other modules of the suite drop and re-create catalog_owner / cis_app (DROP OWNED takes their grants): the file
    # re-sets its own grants, so re-apply it first and check what it leaves.
    await conn.execute(migration_body(272))
    rows = await conn.fetch("""
        SELECT a.grantee::regrole::text AS r, c.relname AS o, a.privilege_type AS p
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.oid IN ('gold_aa_internal.tour_lifecycle_event'::regclass,
                         'gold_aa_internal.v_tour_liveness'::regclass)
           AND a.grantee IN ('cis_app'::regrole, 'aa_app_user'::regrole, 'catalog_owner'::regrole)""")
    got = sorted((r["r"], r["o"], r["p"]) for r in rows)
    assert got == sorted([("cis_app", "tour_lifecycle_event", "INSERT"), ("cis_app", "tour_lifecycle_event", "SELECT"),
                          ("catalog_owner", "tour_lifecycle_event", "SELECT"),
                          ("cis_app", "v_tour_liveness", "SELECT"), ("aa_app_user", "v_tour_liveness", "SELECT"),
                          ("catalog_owner", "v_tour_liveness", "SELECT")])
    t = await _tour(conn, "ao")
    await conn.execute("""
        INSERT INTO gold_aa_internal.tour_lifecycle_event (source_id, tour_id, event, reason, action, idempotency_key)
        VALUES ($1, $2, 'retired', 'master_trashed', 'trash_master', $3)""", t.group, uuid.UUID(t.tour),
        f"test:{uuid.uuid4()}")
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        async with conn.transaction():
            await conn.execute("UPDATE gold_aa_internal.tour_lifecycle_event SET reason = 'publish'")
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        async with conn.transaction():
            await conn.execute("DELETE FROM gold_aa_internal.tour_lifecycle_event")
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE gold_aa_internal.published_tours SET master_status_reason = 'whatever' "
                               "WHERE tour_id = $1", uuid.UUID(t.tour))


@pytest.mark.asyncio
async def test_272_guard_refuses_a_tenant_pool_grant_on_a_relation_without_rls(conn):
    """Guard 4 (AA-859 invariant v3): the file re-sets its own objects' grants, and refuses to run while the tenant
    pool holds a privilege on any other relation without RLS."""
    await conn.execute("CREATE TABLE gold_aa_internal.zz986_no_rls (x int)")
    await conn.execute("GRANT SELECT ON gold_aa_internal.zz986_no_rls TO aa_app_user")
    with pytest.raises(asyncpg.RaiseError, match="invariant v3"):
        async with conn.transaction():
            await conn.execute(migration_body(272))
    await conn.execute("GRANT SELECT ON gold_aa_internal.tour_lifecycle_event TO aa_app_user")
    await conn.execute("DROP TABLE gold_aa_internal.zz986_no_rls")
    await conn.execute(migration_body(272))          # takes the stray grant away again
    assert not await conn.fetchval(
        "SELECT has_table_privilege('aa_app_user', 'gold_aa_internal.tour_lifecycle_event', 'SELECT')")


@pytest.mark.asyncio
async def test_view_states(conn):
    served = await _tour(conn, "served")
    paused = await _tour(conn, "paused", master="inactive")
    m_trashed = await _tour(conn, "mtrash", master="trashed")
    s_deleted = await _tour(conn, "sdel", source_status="trashed", deleted=True)
    g = uuid.uuid4()
    orphan = await _tour(conn, "orphan", group=g, source_status="superseded")
    g2 = uuid.uuid4()
    pending_old = await _tour(conn, "pend1", group=g2, source_status="superseded")
    await _tour(conn, "pend2", group=g2, version=2, master=None)
    never = await _tour(conn, "never", master=None)
    assert await _state(conn, served) == ("served", None)
    assert await _state(conn, paused) == ("paused", "master_inactive")
    assert await _state(conn, m_trashed) == ("retired", "master_trashed")
    assert await _state(conn, s_deleted) == ("retired", "source_deleted")
    assert await _state(conn, orphan) == ("retired", "superseded_orphan")
    assert await _state(conn, pending_old) == ("paused", "superseded_pending")
    assert await _state(conn, never) == (None, None)
    # served keys = the catalog._live_tour rule (258 predicates)
    rule = {r["k"] for r in await conn.fetch("""
        SELECT DISTINCT (COALESCE(rt.source_group_id, rt.tour_id))::text AS k
          FROM gold_aa_internal.published_tours pt JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
         WHERE pt.tenant_id = $1 AND pt.master_status = 'active' AND pt.deleted_at IS NULL
           AND rt.source_status = 'active' AND rt.deleted_at IS NULL""", uuid.UUID(MASTER))}
    view = {r["source_id"] for r in await conn.fetch(
        "SELECT source_id FROM gold_aa_internal.v_tour_liveness WHERE state = 'served'")}
    assert view == rule
