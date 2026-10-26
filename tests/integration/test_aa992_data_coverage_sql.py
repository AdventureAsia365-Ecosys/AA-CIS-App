"""AA-992 — every SQL statement of the data coverage page runs on the migrated test DB as the role that reads it on
Dev: ``cis_app`` for the CIS tables and the owner contract views, ``aa_app_user`` for the ``catalog.*`` views (lesson
S231: a wrong column surfaces only when the statement really runs). Then the whole build runs on the same database
through a one-connection pool, and EXPLAIN shows that no statement touches the two big log tables.

The test DB differs from Dev in known ways, each filled here with a stand-in of the live shape inside the test's
rolled-back transaction (lesson S237: never a global stand-in):
- the owner views (tp-020, bk-017 / bk-023 / bk-025) live in other repos' schemas: empty views with the contract's
  exact column lists;
- the replay skips CIS 253–258 / 274 part B / 285 (catalog recipe, conftest): ``raw_tours.source_key`` (258, the
  same generated column), ``catalog.v_tour_retired`` / ``v_tour_lifecycle_outbox`` (274, contract columns) and
  ``geo.component_redirect`` (285);
- CI's postgres:15 has no pgvector: the VECTOR columns the page tests for NULL (``content_embedding``, ``embedding``)
  and ``acp_contract.atom_embedding`` get ``real[]`` stand-ins. ``catalog.v_tour_component.embedding`` is a view
  column and cannot be added: that one statement skips with the reason when it is missing.
Anything else missing fails.
"""
from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X1_CIS_SCHEMAS  # noqa: E402
from _ttv_live_standin import TTV_LIVE_STANDIN_DDL  # noqa: E402

from api.services.data_coverage import queries as q  # noqa: E402
from api.services.data_coverage.defs import MASTER_TENANT  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
NOW = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)

# Relations whose CIS migration needs pgvector (VECTOR columns / HNSW), absent from a pgvector-less test DB.
VECTOR_RELATIONS = {
    "acp_contract.atom_embedding", "acp_contract.question_embedding", "geo.tour_place", "geo.tour_build",
    "geo.tour_day", "geo.tour_stop", "geo.graph_build", "geo.place_name", "geo.place_shape",
    "geo.component_redirect", "catalog.v_tour_component", "catalog.v_tour_stop", "catalog.v_tour_leg",
    "acp_shared.content_piece", "acp_shared.atom_place_card", "acp_shared.place_card",
}

# (name, sql, args) read as cis_app.
CIS_STATEMENTS = [
    ("IN_FILES", q.IN_FILES, (MASTER_TENANT,)),
    ("IN_BATCHES", q.IN_BATCHES, (MASTER_TENANT,)),
    ("IN_DROPS", q.IN_DROPS, (MASTER_TENANT,)),
    ("IN_STAGING", q.IN_STAGING, (MASTER_TENANT,)),
    ("IN_S3_PATHS", q.IN_S3_PATHS, (MASTER_TENANT,)),
    ("RAW", q.RAW, (MASTER_TENANT,)),
    ("S1", q.S1, (MASTER_TENANT,)),
    ("MASTER", q.MASTER, (MASTER_TENANT,)),
    ("REVIEW", q.REVIEW, ()),
    ("SEO", q.SEO, (MASTER_TENANT,)),
    ("ATOMS", q.ATOMS, (MASTER_TENANT,)),
    ("SEG", q.SEG, (MASTER_TENANT,)),
    ("SEG_NO_MEMBER_ATOMS", q.SEG_NO_MEMBER_ATOMS, (MASTER_TENANT,)),
    ("GC", q.GC, ()),
    ("RANK", q.RANK, (MASTER_TENANT,)),
    ("ROUTE", q.ROUTE, (MASTER_TENANT,)),
    ("PAA", q.PAA, ()),
    ("EMB", q.EMB, ()),
    ("GEO", q.GEO, (MASTER_TENANT,)),
    ("CARD", q.CARD, (MASTER_TENANT,)),
    ("PHOTO", q.PHOTO, (MASTER_TENANT,)),
    ("TEN", q.TEN, (MASTER_TENANT,)),
    ("T2", q.T2, ()),
    ("TACT", q.TACT, ()),
    ("T9", q.T9, ()),
    ("AA_CONTENT", q.AA_CONTENT, ()),
    ("TP_TOTALS", q.TP_TOTALS, ()),
    ("TP_PINS", q.TP_PINS, ()),
    ("TP_ID_MAP", q.TP_ID_MAP, ()),
    ("TP_PIN_CHAIN", q.TP_PIN_CHAIN, (MASTER_TENANT, ["c1"], [2], ["c1"], ["c2"])),
    ("TP_PURGE", q.TP_PURGE, (["k1"],)),
    ("BK_TOTALS", q.BK_TOTALS, ()),
    ("BK_PLAN_KEYS", q.BK_PLAN_KEYS, (MASTER_TENANT,)),
    ("SUPPLY", q.SUPPLY, ()),
    ("BK_CASES", q.BK_CASES, ()),
    ("BK_FUNNEL", q.BK_FUNNEL, ()),
    ("LIVE_COUNT", q.LIVE_COUNT, (MASTER_TENANT,)),
    ("LIVE_SINCE", q.LIVE_SINCE, (MASTER_TENANT, NOW)),
]
TENANT_STATEMENTS = [
    ("TENANT_TP_LAYERS", q.TENANT_TP_LAYERS),
    ("TENANT_TP_COMPONENTS", q.TENANT_TP_COMPONENTS),
    ("TENANT_TP_RETIRED", q.TENANT_TP_RETIRED),
    ("TENANT_TP_OUTBOX", q.TENANT_TP_OUTBOX),
]

# Contract stand-ins: exact column lists of tp-020 / bk-025 and the bk-017 / bk-023 columns the page reads.
OWNER_VIEW_STANDINS = {
    "tripplanner.v_ops_data_totals": "SELECT NULL::text AS metric, NULL::bigint AS n WHERE false",
    "tripplanner.v_ops_pin_components": "SELECT NULL::text AS component_id, NULL::bigint AS trips WHERE false",
    "tripplanner.v_ops_component_id_map": "SELECT NULL::text AS old_id, NULL::text AS new_id WHERE false",
    "booking.v_ops_data_totals": "SELECT NULL::text AS metric, NULL::bigint AS n WHERE false",
    "booking.v_ops_trip_plan_keys": ("SELECT NULL::text AS source_system, NULL::text AS source_id, "
                                     "NULL::boolean AS sellable WHERE false"),
    "booking.v_ops_case_status": "SELECT NULL::text AS status, NULL::bigint AS n WHERE false",
    "booking.v_ops_funnel_daily": ("SELECT NULL::date AS day, NULL::text AS metric, NULL::text AS source_system, "
                                   "NULL::bigint AS n WHERE false"),
    "booking.v_supply_hotels": ("SELECT NULL::uuid AS id, NULL::text AS status, NULL::uuid AS destination_id "
                                "WHERE false"),
    "booking.v_supply_services": ("SELECT NULL::uuid AS id, NULL::text AS status, NULL::uuid AS destination_id "
                                  "WHERE false"),
}

# Live-shape stand-ins for what the CI replay or a pgvector-less server leaves out (idempotent; rolled back).
SHAPE_STANDINS = (
    """DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'silver_aa_internal'
                          AND table_name = 'raw_tours' AND column_name = 'source_key') THEN
           ALTER TABLE silver_aa_internal.raw_tours
             ADD COLUMN source_key text GENERATED ALWAYS AS ((COALESCE(source_group_id, tour_id))::text) STORED;
         END IF;
       END $$""",
    "ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS content_embedding real[]",
    "ALTER TABLE gold_aa_internal.published_tours ADD COLUMN IF NOT EXISTS content_embedding real[]",
    "ALTER TABLE geo.tour_place ADD COLUMN IF NOT EXISTS embedding real[]",
    """CREATE TABLE IF NOT EXISTS acp_contract.atom_embedding (
         atom_id text NOT NULL, view text NOT NULL, embedding real[], PRIMARY KEY (atom_id, view))""",
    """CREATE TABLE IF NOT EXISTS geo.component_redirect (
         old_id uuid PRIMARY KEY, new_id uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""",
    """DO $$ BEGIN
         IF to_regclass('catalog.v_tour_retired') IS NULL THEN
           CREATE VIEW catalog.v_tour_retired AS
             SELECT NULL::text AS source_system, NULL::text AS source_id, NULL::text AS state, NULL::text AS reason,
                    NULL::timestamptz AS changed_at WHERE false;
           GRANT SELECT ON catalog.v_tour_retired TO aa_app_user;
         END IF;
         IF to_regclass('catalog.v_tour_lifecycle_outbox') IS NULL THEN
           CREATE VIEW catalog.v_tour_lifecycle_outbox AS
             SELECT NULL::bigint AS event_id, NULL::text AS idempotency_key WHERE false;
           GRANT SELECT ON catalog.v_tour_lifecycle_outbox TO aa_app_user;
         END IF;
       END $$""",
)

MANY_ROWS = {"IN_DROPS", "IN_S3_PATHS", "SEG_NO_MEMBER_ATOMS", "TP_TOTALS", "TP_PINS", "TP_ID_MAP", "BK_TOTALS",
             "GC"}
_MISSING_RE = re.compile(r'relation "([^"]+)" does not exist')


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for ddl in SHAPE_STANDINS:
            await c.execute(ddl)
        # The replay skips CIS 244 (conftest), so cis_app has none of its Dev grants here: give it USAGE + SELECT on
        # the CIS runtime schemas for this transaction. This test checks the SQL (tables, columns, types); the Dev
        # grant set is the X1 tests' and the isolation probe's.
        for schema in X1_CIS_SCHEMAS:
            if await c.fetchval("SELECT to_regnamespace($1)", schema) is not None:
                await c.execute(f"GRANT USAGE ON SCHEMA {schema} TO cis_app")
                await c.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO cis_app")
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(TTV_LIVE_STANDIN_DDL)
            await c.execute("GRANT SELECT ON gold_aa_internal.tenant_tour_versions TO cis_app")
        for view, body in OWNER_VIEW_STANDINS.items():
            schema = view.split(".")[0]
            if await c.fetchval("SELECT to_regclass($1)", view) is None:
                await c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                await c.execute(f"CREATE VIEW {view} AS {body}")
            await c.execute(f"GRANT USAGE ON SCHEMA {schema} TO cis_app")
            await c.execute(f"GRANT SELECT ON {view} TO cis_app")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _missing_vector_relation(c, err: Exception) -> str | None:
    """The relation an UndefinedTable error names, when it is one of the pgvector-only relations and really absent."""
    m = _MISSING_RE.search(str(err))
    if not m:
        return None
    name = m.group(1)
    candidates = [n for n in VECTOR_RELATIONS if n == name or n.endswith("." + name)]
    for n in candidates:
        if await c.fetchval("SELECT to_regclass($1)", n) is None:
            return n
    return None


async def _run_as(c, role: str, sql: str, args: tuple) -> list:
    async with c.transaction():  # savepoint: an error rolls back only this statement (and the SET LOCAL ROLE)
        await c.execute(f"SET LOCAL ROLE {role}")
        await c.execute("SET LOCAL statement_timeout = '10s'")
        rows = await c.fetch(sql, *args)
        await c.execute("RESET ROLE")
        return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("name,sql,args", CIS_STATEMENTS, ids=[s[0] for s in CIS_STATEMENTS])
async def test_statement_runs_as_cis_app(conn, name, sql, args):
    try:
        rows = await _run_as(conn, "cis_app", sql, args)
    except asyncpg.exceptions.UndefinedTableError as e:
        missing = await _missing_vector_relation(conn, e)
        if missing:
            pytest.skip(f"{name}: {missing} needs pgvector, absent from this test DB")
        raise
    if name not in MANY_ROWS:
        assert len(rows) == 1, f"{name} must return one row of counts"
        assert all(v is None or isinstance(v, (int, float, datetime)) for v in rows[0].values()), name


@pytest.mark.asyncio
@pytest.mark.parametrize("name,sql", TENANT_STATEMENTS, ids=[s[0] for s in TENANT_STATEMENTS])
async def test_catalog_statement_runs_as_aa_app_user(conn, name, sql):
    try:
        await _run_as(conn, "aa_app_user", sql, ())
    except asyncpg.exceptions.UndefinedColumnError as e:
        if "embedding" in str(e) and not await conn.fetchval(
                "SELECT count(*) > 0 FROM pg_extension WHERE extname = 'vector'"):
            pytest.skip(f"{name}: catalog.v_tour_component.embedding needs pgvector, absent from this test DB")
        raise
    except asyncpg.exceptions.UndefinedTableError as e:
        missing = await _missing_vector_relation(conn, e)
        if missing:
            pytest.skip(f"{name}: {missing} needs pgvector, absent from this test DB")
        raise


@pytest.mark.asyncio
@pytest.mark.parametrize("name,sql,args", CIS_STATEMENTS, ids=[s[0] for s in CIS_STATEMENTS])
async def test_no_statement_reads_the_big_log_tables(conn, name, sql, args):
    assert "llm_call_log" not in sql and "decision_log" not in sql
    try:
        plan = "\n".join(r[0] for r in await conn.fetch(f"EXPLAIN {sql}", *args))
    except asyncpg.exceptions.UndefinedTableError as e:
        if await _missing_vector_relation(conn, e):
            pytest.skip("pgvector-only relation absent")
        raise
    assert "llm_call_log" not in plan and "decision_log" not in plan


class _OneConnPool:
    def __init__(self, c):
        self.c = c

    @asynccontextmanager
    async def acquire(self, timeout=None):
        async with self.c.transaction():   # savepoint per source, like a fresh connection's transaction
            await self.c.execute("SET LOCAL ROLE cis_app")
            yield _NoTx(self.c)
            await self.c.execute("RESET ROLE")


class _NoTx:
    """The connection with transaction() as a savepoint and READ ONLY kept (SET TRANSACTION is first-statement only)."""

    def __init__(self, c):
        self.c = c

    def transaction(self, **_):
        return self.c.transaction()

    def __getattr__(self, name):
        return getattr(self.c, name)


@pytest.mark.asyncio
async def test_the_fast_tier_builds_on_the_migrated_db(conn, monkeypatch):
    """The whole build on this DB (sources as cis_app; S3 stubbed, no catalog pool, no CRM token): every fast CIS source
    answers ok, or is a pgvector-only gap; the response validates against the API schema."""
    import api.services.data_coverage.build as b
    from api.schemas.data_coverage import DataCoverageResponse
    from api.services.data_coverage import sources as src

    monkeypatch.setattr(src, "CIS_CONCURRENCY", 1)   # one shared connection: one statement at a time
    s3 = type("S3", (), {"list_objects_v2": lambda self, **kw: {"Contents": [], "IsTruncated": False}})()
    out = await b.build(_OneConnPool(conn), None, None, authorization=None, s3=s3, bucket="aa-cis-bronze-test")
    DataCoverageResponse.model_validate(out)
    vector_sources = {"emb", "geo", "card", "photo", "t9", "tp_pins"}
    for key, s in out["sources"].items():
        if s["tier"] != "fast":
            continue
        assert s["status"] == "ok" or key in vector_sources, (key, s)
    assert out["live_tours"] == await conn.fetchval(q.LIVE_COUNT, MASTER_TENANT)
