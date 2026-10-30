"""AA-1003 R4 against real Postgres: every SQL the R4 readers run, as written, on the replayed schema, then each
renderer on what the collectors returned.

Fixture rows and live-shape stand-ins live in one transaction rolled back at the end: a lifecycle event, an eval run,
a rerun wave with one failed s1_rewrite job and its LLM call, a retention job row; empty contract views with the
owners' column lists (booking.v_ops_inquiry_inbox bk-035, tripplanner.v_ops_inquiry_keys tp-022 and the ops.overview
views). pg_stat_statements may be absent from the test server: OPS-13 then names it as not readable.
"""
import contextlib
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.reports import money, quality, registry, reliability  # noqa: E402
from shared.jobs import queue  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
UTC = timezone.utc
MON, SUN = date(2023, 5, 1), date(2023, 5, 7)          # an ISO week no other test writes
TAG = "aa1003_r4_probe"
NOW = datetime(2023, 5, 8, 2, tzinfo=UTC)
EXPECTED = {"s1_rewrite": 900, "retention_job": 600}


def _ts(day: date, hour: int = 3) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


# Live-shape stand-ins inside the test's rolled-back transaction (lesson S237: never global): raw_tours.source_key
# (CIS 258, skipped by the replay) and the owner contract views DATA-02 reads (empty, contract columns).
SHAPE_STANDINS = (
    """DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'silver_aa_internal'
                          AND table_name = 'raw_tours' AND column_name = 'source_key') THEN
           ALTER TABLE silver_aa_internal.raw_tours
             ADD COLUMN source_key text GENERATED ALWAYS AS ((COALESCE(source_group_id, tour_id))::text) STORED;
         END IF;
       END $$""",
)
VIEW_STANDINS = {
    "tripplanner.v_ops_traffic_daily": "SELECT NULL::date AS day, NULL::text AS metric, NULL::bigint AS n WHERE false",
    "booking.v_ops_funnel_daily": ("SELECT NULL::date AS day, NULL::text AS metric, NULL::text AS source_system, "
                                   "NULL::bigint AS n WHERE false"),
    "booking.v_ops_inquiry_inbox": ("SELECT NULL::text AS source, NULL::text AS idempotency_key, NULL::text AS status, "
                                    "NULL::timestamptz AS received_at, NULL::timestamptz AS processed_at, "
                                    "NULL::boolean AS has_trip_case WHERE false"),
    "tripplanner.v_ops_inquiry_keys": ("SELECT NULL::text AS idempotency_key, NULL::timestamptz AS created_at "
                                       "WHERE false"),
}


@pytest_asyncio.fixture
async def env():
    """One connection inside a transaction rolled back at the end (fixture rows and stand-ins never stay)."""
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for ddl in SHAPE_STANDINS:
            await c.execute(ddl)
        for view, body in VIEW_STANDINS.items():
            if await c.fetchval("SELECT to_regclass($1)", view) is None:
                await c.execute(f"CREATE SCHEMA IF NOT EXISTS {view.split('.')[0]}")
                await c.execute(f"CREATE VIEW {view} AS {body}")
        tour = uuid.uuid4()
        job_id, run_id, wave_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await c.execute(
            "INSERT INTO gold_aa_internal.tour_lifecycle_event (source_system, source_id, tour_id, event, reason, "
            "action, actor, details, occurred_at, idempotency_key) VALUES ('cis', $1, $2, 'retired', "
            "'source_deleted', 'retire', $1, '{}'::jsonb, $3, $4)", TAG, tour, _ts(MON + timedelta(days=1)),
            f"{TAG}:{tour}")
        await c.execute(
            "INSERT INTO shared.prompt_eval_runs (id, pipeline, prompt_version, tour_count, avg_quality_score, "
            "baseline_avg_quality_score, gate_pass_count, gate_fail_count, cost_usd, regression_detected, "
            "triggered_by, created_at, run_kind) VALUES ($1, 's1_old', $2, 3, 7.5, 8.0, 2, 1, 0.3, false, $2, $3, "
            "'regression')", run_id, TAG, _ts(MON + timedelta(days=2)))
        await c.execute(
            "INSERT INTO shared.rerun_wave (id, name, country, expected_tour_ids, status, created_at, closed_at) "
            "VALUES ($1, $2, 'Japan', ARRAY[$3::uuid], 'incomplete', $4, $5)",
            wave_id, TAG, tour, _ts(MON), _ts(MON + timedelta(days=3)))
        await c.execute(
            "INSERT INTO shared.job (id, kind, payload, status, attempt, max_attempts, run_after, created_by, "
            "created_at, started_at, finished_at, error, cost_usd) VALUES ($1, 's1_rewrite', $2::jsonb, 'failed', 2, "
            "2, $3, $4, $3, $3, $5, 'boom', 0.4)", job_id, json.dumps({"tour_id": str(tour)}), _ts(MON, 4), TAG,
            _ts(MON, 5))
        await c.execute(
            "INSERT INTO shared.llm_call_log (id, stage, role, model, tokens_in, tokens_out, cost_usd, "
            "quality_signal, created_at, account, fallback_used, provider, job_id) VALUES (gen_random_uuid(), $1, "
            "'writer', 'm', 10, 5, 0.25, '{}'::jsonb, $2, 'acc3', false, 'bedrock', $3)",
            TAG, _ts(MON, 4) + timedelta(minutes=5), job_id)
        await c.execute(
            "INSERT INTO shared.job (kind, payload, status, attempt, max_attempts, run_after, created_by, created_at, "
            "started_at, finished_at, result) VALUES ('retention_job', '{}'::jsonb, 'succeeded', 1, 1, $1, $2, $1, "
            "$1, $1, '{\"mode\": \"dry_run\"}'::jsonb)", _ts(MON + timedelta(days=1)), TAG)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def test_quality_readers_and_renderers(env):
    reg = registry.load()
    c = env
    cis01 = await quality.collect_cis01(c, MON, SUN)
    evals = await quality.eval_period(c, MON, SUN)
    cis07 = await quality.collect_cis07(c, MON, SUN)
    assert {k: v for k, v in cis01.items() if money.failed(v)} == {}, cis01
    assert any(r["reason"] == "source_deleted" for r in cis01["lifecycle"]["counts"])
    assert any(r["source_id"] == TAG for r in cis01["lifecycle"]["review"])
    assert any(r["prompt_version"] == TAG for r in evals["runs"])
    assert {k: v for k, v in cis07.items() if money.failed(v)} == {}, cis07
    wave = next(w for w in cis07["waves"]["waves"] if w["report"]["wave"]["name"] == TAG)
    stage = next(s for s in wave["stages"] if s["stage"] == TAG)
    assert stage["cost_usd"] == pytest.approx(0.25) and stage["failed_cost_usd"] == pytest.approx(0.25)
    out = quality.render_cis01(reg, reg.report("CIS-01"), MON, SUN, cis01, NOW)
    assert out.markdown.startswith("# ") and out.output["report_id"] == "CIS-01"
    out = quality.render_cis06(reg, reg.report("CIS-06"), MON, SUN, {"eval": evals}, NOW)
    assert "K-EVAL-DELTA" in {k["id"] for k in out.output["kpis"]}
    out = quality.render_cis07(reg, reg.report("CIS-07"), MON, SUN, cis07, NOW)
    assert any(w["name"] == TAG and w["retry_ratio"] == 1.0 for w in out.output["waves"])


class _OneConn:
    """queue.claim's pool interface over the test connection (its transaction nests as a savepoint)."""

    def __init__(self, conn):
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


async def _pss_readable(c) -> bool:
    """pg_stat_statements answers a query: the extension exists AND is preloaded (CI's postgres:15 service creates
    the extension through CIS 234 but does not preload it). Probed in a savepoint so a refusal does not abort the
    test's transaction."""
    if await c.fetchval("SELECT to_regclass('public.pg_stat_statements_info')") is None:
        return False
    try:
        async with c.transaction():
            await c.fetchval("SELECT stats_reset FROM public.pg_stat_statements_info")
        return True
    except asyncpg.PostgresError:
        return False


async def test_reliability_readers_and_renderers(env, monkeypatch):
    from services.reports import money_sources as src

    async def no_rds(first, end):
        return {"unavailable": "not_permitted: test"}
    monkeypatch.setattr(src, "rds_metrics", no_rds)
    c = env
    has_pss = await _pss_readable(c)
    if has_pss:         # the real claim, read back through CLAIM_SQL (pg_stat_statements normalises its literals)
        await queue.claim(_OneConn(c), "aa1003-r4-probe", {TAG: 1}, 60)
        claim = await c.fetchrow(reliability.CLAIM_SQL)
        assert claim["calls"] and claim["calls"] >= 1 and claim["mean_ms"] is not None, dict(claim)
    if not has_pss:     # a failed statement would abort this test's transaction (Dev and CI production: no transaction)
        async def no_pss(conn):
            raise RuntimeError("pg_stat_statements is not installed in this test DB")
        monkeypatch.setattr(reliability, "statements", no_pss)
    reg = registry.load()
    jobs = await reliability.collect_jobs(c, MON, SUN)
    ops13 = await reliability.collect_ops13(c, MON, SUN)
    data02 = await reliability.collect_data02(c, MON, SUN)
    versions = await reliability.schema_versions(c)
    data04 = await reliability.collect_data04(c, date(2023, 5, 1), date(2023, 5, 31))
    assert {k: v for k, v in jobs.items() if money.failed(v)} == {}, jobs
    s1 = next(r for r in jobs["job_kinds"] if r["kind"] == "s1_rewrite")
    assert s1["failed"] >= 1 and s1["retried"] >= 1
    assert set(k for k, v in ops13.items() if money.failed(v)) <= ({"statements"} if not has_pss else set()), ops13
    if has_pss:
        claim_obj = next(o for o in reliability.objectives(ops13) if o["id"] == "job_claim")
        assert claim_obj["value"] is not None, claim_obj
    assert {k: v for k, v in data02.items() if money.failed(v)} == {}, data02          # contract stand-ins: empty
    assert data02["unmatched"]["unmatched"] == 0 and data02["inbox"]["status"] == []
    assert any(v["version"] == "305" for v in versions)
    assert {k: v for k, v in data04.items() if money.failed(v)} == {}, data04
    assert {r["table"] for r in data04["overdue"]} >= {"shared.job", "shared.llm_call_log"}
    assert any(r["kind"] == "retention_job" and r["mode"] == "dry_run" for r in data04["runs"])
    for rid, facts in (("OPS-14", jobs), ("OPS-13", ops13), ("DATA-02", data02), ("DATA-03", {"versions": versions}),
                       ("DATA-04", data04)):
        fn = {"OPS-14": reliability.render_ops14, "OPS-13": reliability.render_ops13,
              "DATA-02": reliability.render_data02, "DATA-03": reliability.render_data03,
              "DATA-04": reliability.render_data04}[rid]
        kw = {"expected": EXPECTED} if rid in ("OPS-14", "OPS-13") else {}
        out = fn(reg, reg.report(rid), MON, SUN, facts, NOW, **kw)
        assert out.markdown.startswith("# ") and out.output["report_id"] == rid
