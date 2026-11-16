"""AA-1122 — the new reads and writes on the real schema (CIS 450-452 applied by conftest): the research month ledger by
job, the monthly volume history (never overwritten) and the season read, the tour PAA backfill (candidates, the write
that never overwrites a bought list, coverage), the rename carry, the agency markets of automatic research and the
Research tab's run rows.

One asyncpg connection inside a transaction that is rolled back. No DataForSEO, LLM or Jev call. Invented names only.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TENANT_ID  # noqa: E402
from api.routers import admin_segment_research as router  # noqa: E402
from services.acp_contract import research_auto as ra  # noqa: E402
from services.acp_contract import research_carry as sm  # noqa: E402
from services.seo_intelligence import search_history as sh  # noqa: E402
from services.seo_intelligence import tour_paa  # noqa: E402
from shared import cost_guard
from shared import research_budget as cg  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def fetch(self, *a):
        return await self.conn.fetch(*a)


async def _needs_cis_450_451(conn) -> None:
    """The history table (CIS 450) and the PAA provenance columns (CIS 451) ship in this wave's migration-only PR, which
    merges before this code PR (MIG-4): on a branch without them the two tests that write them are skipped; on main (and
    in the merge queue, after the migration PR) they run."""
    ok = await conn.fetchval(
        "SELECT to_regclass('acp_contract.search_volume_monthly') IS NOT NULL AND EXISTS (SELECT 1 FROM "
        "information_schema.columns WHERE table_schema = 'silver_aa_internal' AND table_name = 'seo_context' "
        "AND column_name = 'paa_source') AS ok")
    if not ok:
        pytest.skip("CIS 450-451 not in this tree yet (the wave's migration PR merges first)")


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_ID)
    yield c
    await t.rollback()
    await c.close()


async def _job(conn, kind: str, status: str = "succeeded", created_by: str | None = None, scope: str | None = None,
               created_at: datetime | None = None) -> str:
    payload = {"scope": scope} if scope else {}
    return str(await conn.fetchval(
        "INSERT INTO shared.job (kind, payload, status, created_by, created_at, run_after) "
        "VALUES ($1, $2::jsonb, $3, $4, coalesce($5, now()), now()) RETURNING id",
        kind, json.dumps(payload), status, created_by, created_at))


async def _dfs(conn, job_id, cost, stage="a3_search_demand", at=None):
    await conn.execute("INSERT INTO shared.dfs_call_log (endpoint, cost_usd, cache_hit, fetched_live, job_id, stage, "
                       "created_at) VALUES ('search_volume_bulk', $1, false, true, $2::uuid, $3, coalesce($4, now()))",
                       cost, job_id, stage, at)


async def _llm(conn, job_id, cost, provider="bedrock", at=None):
    await conn.execute("INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, provider, job_id, created_at, "
                       "quality_signal) VALUES ('segment_research_batch', 'writer', 'zz-model', $1, $2, $3::uuid, "
                       "coalesce($4, now()), '{}'::jsonb)",
                       cost, provider, job_id, at)


@pytest.mark.asyncio
async def test_month_spend_sums_research_jobs_by_job_whatever_the_stage(tx):
    await tx.execute("DELETE FROM shared.dfs_call_log WHERE job_id IS NOT NULL")
    run = await _job(tx, "segment_research", created_by="system:auto", scope="auto_new")
    paa = await _job(tx, "tour_paa_backfill")
    other = await _job(tx, "s1_seo_prefetch")
    await _dfs(tx, run, 0.06)
    await _dfs(tx, run, 0.0006, stage="a3_research")              # any stage of a research job counts
    await _llm(tx, run, 0.01)
    await _llm(tx, run, 0.002, provider="typesafe")                # Jev rows too
    await _dfs(tx, paa, 0.002, stage="a1_tour_paa")
    await _dfs(tx, other, 5.0, stage="s1_seo_prefetch")            # not research: outside the cap
    await _dfs(tx, run, 1.0, at=datetime.now(timezone.utc) - timedelta(days=45))   # last month
    got = await cg.research_month_spend(_Pool(tx))
    assert got["total_usd"] == pytest.approx(0.0746)
    assert await cg.job_spend(_Pool(tx), run) == pytest.approx(1.0726)


@pytest.mark.asyncio
async def test_history_is_written_once_and_read_as_a_season(tx):
    await _needs_cis_450_451(tx)
    may = {"zz lantern valley": ("search_volume_bulk", [(date(2026, m, 1), 100 * m) for m in range(1, 13)])}
    assert await sh.write(tx, "US", may) == 12
    again = {"zz lantern valley": ("search_volume_bulk_queue", [(date(2026, 12, 1), 1)])}
    await sh.write(tx, "US", again)
    assert await tx.fetchval("SELECT search_volume FROM acp_contract.search_volume_monthly WHERE keyword = $1 "
                             "AND market = 'US' AND month = '2026-12-01'", "zz lantern valley") == 1200
    await tx.execute("INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, rank, source, "
                     "prompt_version, proposed_at) VALUES ('place', 'Zz Lantern Valley', 'zz lantern valley', 0, "
                     "'place_name', 'zz', now())")
    seasons = await sh.place_seasonality(_Pool(tx), ["Zz Lantern Valley"], "US")
    assert seasons["Zz Lantern Valley"].peak_month == 12
    counts = await sh.counts(_Pool(tx))
    assert counts["rows"] >= 12 and counts["keywords"] >= 1


async def _published(conn, name: str) -> uuid.UUID:
    tour_id = uuid.uuid4()
    await conn.execute("""INSERT INTO silver_aa_internal.raw_tours
                          (tour_id, tenant_id, batch_id, src_name, country, source_group_id, source_status)
                          VALUES ($1, $2, $3, $4, 'Mongolia', $5, 'active')""",
                       tour_id, uuid.UUID(TENANT_ID), uuid.UUID(BATCH_ID), name, uuid.uuid4())
    await conn.execute("""INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, aa_name,
                          master_status) VALUES ($1, gen_random_uuid(), $2, 'active')""", tour_id, name)
    return tour_id


async def _seo(conn, tour_id, paa: list, source=None, filled_at=None):
    await conn.execute("""INSERT INTO silver_aa_internal.seo_context (tour_id, tenant_id, keyword_search, keyword_ideas,
                          people_also_ask, related_keywords, cache_key, provider, fetched_at, paa_source, paa_filled_at)
                          VALUES ($1, $2, 'zz seed', '[]'::jsonb, $3::jsonb, '[]'::jsonb, 'seo:zz:2840:ideas_v3',
                                  'dataforseo', '2026-10-05 12:00+00', $4, $5)""",
                       tour_id, uuid.UUID(TENANT_ID), json.dumps(paa), source, filled_at)


@pytest.mark.asyncio
async def test_tour_paa_candidates_write_rule_and_coverage(tx):
    await _needs_cis_450_451(tx)
    empty = await _published(tx, "Zz Lantern Valley Trek")
    bought = await _published(tx, "Zz Amber Coast Walk")
    old_store = await _published(tx, "Zz Quorra Ridge Hike")
    await _seo(tx, empty, [])
    await _seo(tx, bought, ["Is the coast walk hard?"])                                 # S1 bought it: never touched
    await _seo(tx, old_store, ["Old?"], "store", datetime.now(timezone.utc) - timedelta(days=400))
    window = timedelta(days=180)
    rows = await tx.fetch(tour_paa.CANDIDATES_SQL, window, [empty, bought, old_store])
    assert {r["tour_id"] for r in rows} == {str(empty), str(old_store)}
    await tx.execute(tour_paa.WRITE_SQL, str(bought), json.dumps(["overwritten?"]), "store")
    await tx.execute(tour_paa.WRITE_SQL, str(empty), json.dumps(["When to visit?"]), "store")
    got = {r["tour_id"]: (json.loads(r["people_also_ask"]), r["paa_source"], r["fetched_at"]) for r in await tx.fetch(
        "SELECT tour_id, people_also_ask::text AS people_also_ask, paa_source, fetched_at "
        "FROM silver_aa_internal.seo_context WHERE tour_id = ANY($1::uuid[])", [empty, bought])}
    assert got[bought][:2] == (["Is the coast walk hard?"], None)
    assert got[empty][:2] == (["When to visit?"], "store")
    assert got[empty][2] == datetime(2026, 10, 5, 12, tzinfo=timezone.utc)              # fetched_at untouched
    cov = await tour_paa.coverage(_Pool(tx))
    assert cov["published"] >= 3 and cov["with_paa"] >= 3
    store = await tx.fetch(tour_paa.STORE_SQL, list(tour_paa.STORE_MARKETS), ["%zz lantern%"], window)
    assert isinstance(store, list)


@pytest.mark.asyncio
async def test_rename_carry_copies_log_and_map_and_keeps_the_newest(tx):
    t_old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    t_new = datetime(2026, 10, 1, tzinfo=timezone.utc)
    await tx.executemany("INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at) "
                         "VALUES ($1, $2, $3, $4)",
                         [("Zz Hội An", "US", "volume", t_new), ("Zz Hội An", "US", "serp", t_new),
                          ("Zz Hoi An", "US", "volume", t_old)])
    await tx.execute("INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, rank, source, "
                     "prompt_version, proposed_at) VALUES ('place', 'Zz Hội An', 'zz hoi an', 0, 'place_name', 'zz', "
                     "now())")
    assert await sm.carry_research(tx, [("Zz Hội An", "Zz Hoi An")]) == 1
    rows = {(r["tier"]): r["researched_at"] for r in await tx.fetch(
        "SELECT tier, researched_at FROM acp_contract.segment_research_log WHERE canonical_place = 'Zz Hoi An'")}
    assert rows == {"volume": t_new, "serp": t_new}
    assert await tx.fetchval("SELECT count(*) AS n FROM acp_contract.place_keyword WHERE subject = 'Zz Hoi An'") == 1
    await sm.carry_research(tx, [("Zz Hội An", "Zz Hoi An")])                 # re-run: nothing doubles
    assert await tx.fetchval("SELECT count(*) AS n FROM acp_contract.place_keyword WHERE subject = 'Zz Hoi An'") == 1


@pytest.mark.asyncio
async def test_auto_markets_count_active_agencies_only_plus_the_default(tx):
    async def tenant(kind: str, status: str, market: str) -> None:
        tid = uuid.uuid4()
        await tx.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active, "
            "kind) VALUES ($1, $2, $3, 'starter'::plan_tier_enum, 3, 60, true, $4)",
            tid, f"Zz {kind}", f"zz1122-{tid.hex[:12]}", kind)
        await tx.execute("UPDATE shared.tenants SET account_status = $2 WHERE tenant_id = $1", tid, status)
        await tx.execute("INSERT INTO shared.tenant_seo_config (tenant_id, seo_provider, target_market) "
                         "VALUES ($1, 'dataforseo', $2::jsonb)", tid, json.dumps({"countries": [market]}))

    async def rows():
        return len(await tx.fetch(ra.ELIGIBLE_MARKETS_SQL, ra.MASTER_TENANT, ["active"]))

    before = await rows()
    await tenant("agency", "active", "UK")
    assert await rows() == before + 1
    await tenant("demo", "active", "DE")              # a demo tenant never starts a purchase
    await tenant("agency", "trial", "FR")             # a status outside the setting's list neither
    assert await rows() == before + 1
    markets = await ra.auto_markets(_Pool(tx), ["active"])
    assert "UK" in markets and "US" in markets     # plus the platform default


@pytest.mark.asyncio
async def test_recent_jobs_and_the_panel_run_rows(tx):
    job = await _job(tx, "segment_research", "failed", "system:auto", "auto_refresh")
    await _dfs(tx, job, 0.12)
    jobs = await ra.recent_jobs(_Pool(tx))
    assert jobs[0]["id"] == job and jobs[0]["scope"] == "auto_refresh"
    rows = await tx.fetch(router.RUNS_SQL, list(cg.RESEARCH_JOB_KINDS))
    mine = next(r for r in rows if r["id"] == job)
    assert mine["dfs_usd"] == pytest.approx(0.12) and mine["llm_usd"] == 0
    stages = await tx.fetch(router.DFS_STAGES_SQL, cg.month_start())
    assert any(r["stage"] == "a3_search_demand" for r in stages)
