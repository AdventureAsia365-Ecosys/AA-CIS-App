"""AA-1003 R1 — migration 305 (shared.kpi_daily, shared.report_run) and the report core SQL against real Postgres.

conftest applies every migration, 305 included. These tests check the replayed shape (keys, the scheduled-run unique
index, the CHECKs, cis_app's grants, the registration), then run the store and KPI SQL as written in
services/reports/: the upsert overwrites a day, a scheduled run of the same report and period is created once, a
manual one any number of times, last_runs reports what is covered, and the KPI computers read shared.job and
shared.llm_call_log for one ICT day (fixture day 2023-05-17, which no other test writes; rows deleted at the end),
and K-CAT-LIVE counts the 'served' keys of the replayed gold_aa_internal.v_tour_liveness (CIS 272).
"""
import json
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.reports import kpis, periods, store  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
DAY = date(2023, 5, 17)
KIND = "aa1003_probe"
STAGE = "aa1003_probe_stage"
PROBE_KPI = "K-AA1003-PROBE"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    yield p
    async with p.acquire() as c:
        await c.execute("DELETE FROM shared.kpi_daily WHERE day = $1 OR kpi_id = $2", DAY, PROBE_KPI)
        await c.execute("DELETE FROM shared.report_run WHERE period_start = $1", DAY)
        await c.execute("DELETE FROM shared.job WHERE kind = $1", KIND)
        await c.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
    await p.close()


def _ict(hour: int, minute: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=periods.ICT)


async def test_replayed_shape(pool):
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '305'") == 1
        pk = await c.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'kpi_daily_pkey'")
        assert pk == "PRIMARY KEY (kpi_id, day, scope)"
        uq = await c.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = 'report_run_schedule_uq'")
        assert "UNIQUE" in uq and "(report_id, period_start, period_end)" in uq and "schedule" in uq
        for table in ("shared.kpi_daily", "shared.report_run"):
            for priv in ("SELECT", "INSERT", "UPDATE"):
                assert await c.fetchval("SELECT has_table_privilege('cis_app', $1, $2)", table, priv), (table, priv)
            assert await c.fetchval("SELECT relrowsecurity FROM pg_class WHERE oid = $1::regclass", table) is False


async def test_checks_refuse_bad_rows(pool):
    async with pool.acquire() as c:
        for kpi_id, scope, unit in (("bad id", "all", "count"), (PROBE_KPI, "tenant:x", "count"),
                                    (PROBE_KPI, "all", "furlongs")):
            with pytest.raises(asyncpg.CheckViolationError):
                await c.execute("INSERT INTO shared.kpi_daily (kpi_id, day, scope, value, unit, source) "
                                "VALUES ($1, $2, $3, 1, $4, 'probe')", kpi_id, DAY, scope, unit)
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO shared.report_run (report_id, period_start, period_end) "
                            "VALUES ('EX-04', $1, $2)", DAY, date(2023, 5, 16))


async def test_upsert_overwrites_the_day(pool):
    v = kpis.KpiValue(PROBE_KPI, "all", Decimal("2"), "probe")
    async with pool.acquire() as c:
        await store.upsert_kpis(c, DAY, [v], units={PROBE_KPI: "count"}, versions={}, job_id=None)
        await store.upsert_kpis(c, DAY, [kpis.KpiValue(PROBE_KPI, "all", Decimal("5"), "probe")],
                                units={PROBE_KPI: "count"}, versions={}, job_id=None)
        rows = await store.kpi_series(c, [PROBE_KPI], DAY, DAY)
    assert len(rows) == 1 and rows[0]["value"] == Decimal("5")


async def test_runs_schedule_once_manual_many_and_last_runs(pool):
    async with pool.acquire() as c:
        first = await store.create_run(c, "EX-04", DAY, DAY, trigger="schedule", triggered_by="scheduler:aa-1003")
        assert first is not None
        assert await store.create_run(c, "EX-04", DAY, DAY, trigger="schedule", triggered_by="x") is None
        m1 = await store.create_run(c, "EX-04", DAY, DAY, trigger="manual", triggered_by="admin:p")
        m2 = await store.create_run(c, "EX-04", DAY, DAY, trigger="manual", triggered_by="admin:p")
        assert m1 and m2 and m1 != m2
        await store.mark_running(c, first, "00000000-0000-0000-0000-000000000001")
        await store.finish_ok(c, first, summary="ok", output={"kpis": []}, markdown="# EX-04")
        await store.finish_failed(c, m1, "boom")
        run = await store.get_run(c, first)
        assert run["status"] == "succeeded" and json.loads(run["output"]) == {"kpis": []}
        assert run["output_markdown"] == "# EX-04" and run["started_at"] and run["finished_at"]
        rows, total = await store.list_runs(c, "EX-04", limit=2, offset=0)
        assert total >= 3 and len(rows) == 2
        last = (await store.last_runs(c))["EX-04"]
        assert last["covered_to"] >= DAY and last["failed_7d"] >= 1


async def test_kpi_computers_read_one_ict_day(pool):
    ctx = kpis.DayContext.for_day(DAY)
    async with pool.acquire() as c:
        # inside the ICT day (00:30 ICT = previous UTC day) and just outside it (00:00 ICT of the next day)
        for finished in (_ict(0, 30), _ict(23, 59)):
            await c.execute(
                "INSERT INTO shared.job (kind, payload, status, created_at, run_after, started_at, finished_at) "
                "VALUES ($1, '{}'::jsonb, 'failed', $2, $2, $3, $3)", KIND, finished, finished)
        await c.execute(
            "INSERT INTO shared.job (kind, payload, status, created_at, run_after, started_at, finished_at) "
            "VALUES ($1, '{}'::jsonb, 'failed', $2, $2, $2, $2)", KIND,
            datetime(2023, 5, 17, 17, 0, tzinfo=timezone.utc))
        for cost, fallback, app in ((Decimal("0.5"), True, None), (Decimal("1.25"), False, "tripplanner")):
            await c.execute(
                "INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, created_at, "
                "fallback_used, provider) VALUES ($1, 'writer', 'probe-model', $2, $3::jsonb, $4, $5, 'bedrock')",
                STAGE, cost, json.dumps({"app": app} if app else {}), _ict(12), fallback)
        failed = await kpis.job_fail(c, ctx)
        llm = {(v.kpi_id, v.scope): v.value for v in await kpis.llm_day(c, ctx)}
        age = await kpis.job_queue_age(c, ctx)
    assert failed[0].value == 2                     # the two rows inside the day; the 17:00 UTC one is the next day
    assert llm[("K-LLM-COST-DAY", "app:tripplanner")] == Decimal("1.25")
    assert llm[("K-LLM-COST-DAY", "app:cis")] == Decimal("0.5")
    assert llm[("K-LLM-COST-DAY", "all")] == Decimal("1.75")
    assert llm[("K-LLM-FALLBACK-24H", "all")] == Decimal("0.5000")
    assert age and age[0].value >= 0


MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"


async def _liveness_tour(c, label, master_status):
    tour_id = uuid.uuid4()
    await c.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "source_status, source_group_id, source_version) "
        "VALUES ($1, $2, $3, $4, 'Vietnam', '3 days', 'active'::silver_aa_internal.source_status_enum, $5, 1)",
        tour_id, MASTER_TENANT, BATCH_ID, f"aa1003 {label}", uuid.uuid4())
    await c.execute(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
        "aa_summary, aa_highlights, master_status) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, $5)",
        tour_id, uuid.uuid4(), MASTER_TENANT, f"aa1003 {label}", master_status)


async def test_tours_live_counts_served_keys_of_the_replayed_view(pool):
    """K-CAT-LIVE against the real v_tour_liveness (CIS 272): its states are served / paused / retired, and the KPI
    counts the served ones. A served, a paused and a retired key are added in a rolled-back transaction."""
    ctx = kpis.DayContext(day=DAY, start=_ict(0), end=_ict(23, 59), latest=True)
    async with pool.acquire() as c:
        tr = c.transaction()
        await tr.start()
        try:
            states = {r["state"] for r in await c.fetch("SELECT DISTINCT state FROM gold_aa_internal.v_tour_liveness")}
            assert states <= {"served", "paused", "retired"}
            before = (await kpis.tours_live(c, ctx))[0].value
            await _liveness_tour(c, "served", "active")
            await _liveness_tour(c, "paused", "inactive")
            await _liveness_tour(c, "retired", "trashed")
            after = await kpis.tours_live(c, ctx)
            direct = await c.fetchval(
                "SELECT count(*) FROM gold_aa_internal.v_tour_liveness WHERE state = 'served'")
        finally:
            await tr.rollback()
    assert after[0].kpi_id == "K-CAT-LIVE" and after[0].scope == "all"
    assert after[0].value == before + 1
    assert after[0].value == direct and direct >= 1
