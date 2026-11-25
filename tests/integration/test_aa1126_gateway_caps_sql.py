"""AA-1126 (S283) against real Postgres: the ledger reads of the anomaly breaker (G2: an app's spend since a time, the
median day over rolled-up and raw days) and of the run caps (G4: a job's spend, an eval run's, an ad-hoc stage's), as
written, on the replayed schema. Fixture days in February 2021 (no other test writes them), invented stages and ids;
everything runs in a transaction that is rolled back."""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared.llm_client import anomaly_breaker as ab  # noqa: E402
from shared.llm_client import run_cap  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
UTC = timezone.utc
TODAY = date(2021, 2, 10)
STAGE = "aa1126_probe"
JOB = "0aa11260-0000-4000-8000-000000000001"


def _ts(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


async def _row(c, *, day: date, hour: int, cost: float, app=None, qs_app=None, stage=STAGE, job=None, run=None):
    qs = {k: v for k, v in (("app", qs_app), ("eval_run", run)) if v}
    await c.execute(
        "INSERT INTO shared.llm_call_log (id, stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal, "
        "created_at, app, job_id) VALUES (gen_random_uuid(), $1, 'writer', 'm-probe', 10, 5, $2, $3::jsonb, $4, $5, "
        "$6::uuid)", stage, cost, json.dumps(qs), _ts(day, hour), app, job)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    ab.invalidate()                         # evaluate reads the median through the per-process cache (S291)
    try:
        yield c
    finally:
        ab.invalidate()
        await tr.rollback()
        await c.close()


@pytest.mark.asyncio
async def test_today_counts_the_app_since_the_window_start(conn):
    await _row(conn, day=TODAY, hour=9, cost=5.0, app="crm")
    await _row(conn, day=TODAY, hour=12, cost=7.0, qs_app="crm")          # an old row: the app in quality_signal
    await _row(conn, day=TODAY, hour=13, cost=100.0, app="cis")           # another app
    await _row(conn, day=date(2021, 2, 9), hour=23, cost=50.0, app="crm")  # yesterday
    assert await ab.read_spend_since(conn, "crm", _ts(TODAY, 0)) == pytest.approx(12.0)
    assert await ab.read_spend_since(conn, "crm", _ts(TODAY, 11)) == pytest.approx(7.0)   # after a reset at 11:00
    assert await ab.read_spend_since(conn, "tripplanner", _ts(TODAY, 0)) == 0.0


@pytest.mark.asyncio
async def test_the_median_day_reads_rolled_days_raw_days_and_empty_days(conn):
    rolled, raw = date(2021, 2, 7), date(2021, 2, 8)                       # 2021-02-09: no spend (0)
    await conn.execute(
        "INSERT INTO shared.llm_call_daily (day, app, tenant_id, stage, role, model, provider, account, is_shadow, "
        "calls, fallback_calls, tokens_in, tokens_out, cost_usd, updated_at) VALUES "
        "($1, 'crm', NULL, $2, 'writer', 'm-probe', 'bedrock', 'acc3', false, 3, 0, 30, 15, 8.0, now())",
        rolled, STAGE)
    await _row(conn, day=rolled, hour=10, cost=99.0, app="crm")            # a rolled day's raw rows are not read again
    await _row(conn, day=raw, hour=10, cost=2.0, app="crm")
    await _row(conn, day=raw, hour=11, cost=1.0, app="crm")
    await _row(conn, day=raw, hour=12, cost=40.0, app="cis")
    assert await ab.read_median(conn, "crm", TODAY, 3) == pytest.approx(3.0)        # median of [8, 3, 0]
    assert await ab.read_median(conn, "crm", TODAY, 2) == pytest.approx(1.5)        # median of [3, 0]
    assert await ab.read_median(conn, "tripplanner", TODAY, 3) == 0.0


@pytest.mark.asyncio
async def test_evaluate_trips_on_the_floor_or_the_multiple(conn):
    await _row(conn, day=TODAY, hour=9, cost=30.0, app="crm")
    policy = ab.caps_settings.AnomalyPolicy(multiple=4.0, floor_usd=25.0, baseline_days=14)
    v = await ab.evaluate(conn, "crm", mode="enforce", policy=policy, now=_ts(TODAY, 15))
    assert v.tripped and v.threshold_usd == 25.0 and v.spent_usd == pytest.approx(30.0)
    assert not v.refuses and v.as_dict()["enforced"] is False          # the CRM gateway does not read it yet
    reset = ab.app_profile.AppProfile(app="crm", anomaly_reset_at=_ts(TODAY, 10).isoformat())
    v2 = await ab.evaluate(conn, "crm", mode="enforce", policy=policy, profile=reset, now=_ts(TODAY, 15))
    assert not v2.tripped and v2.spent_usd == 0.0


@pytest.mark.asyncio
async def test_the_run_spend_of_a_job_an_eval_run_and_an_adhoc_stage(conn):
    await _row(conn, day=TODAY, hour=9, cost=0.4, job=JOB)
    await _row(conn, day=TODAY, hour=10, cost=0.3, job=JOB)
    await _row(conn, day=TODAY, hour=10, cost=9.0)                          # same stage, no job
    assert await run_cap.read_spend(conn, run_cap.Run("s1_rewrite", "job", JOB)) == pytest.approx(0.7)
    await _row(conn, day=TODAY, hour=11, cost=1.5, stage="writing_memory_eval", run="run-zz")
    await _row(conn, day=TODAY, hour=11, cost=2.5, stage="writing_memory_eval", run="run-other")
    ev = run_cap.Run("eval", "eval_run", "run-zz", stage="writing_memory_eval", since=_ts(TODAY, 0))
    assert await run_cap.read_spend(conn, ev) == pytest.approx(1.5)
    await _row(conn, day=TODAY, hour=12, cost=0.25, stage="adhoc_aa1126_probe")
    await _row(conn, day=TODAY, hour=8, cost=4.0, stage="adhoc_aa1126_probe")      # before this process started
    ad = run_cap.Run("adhoc", "stage", "adhoc_aa1126_probe", stage="adhoc_aa1126_probe", since=_ts(TODAY, 9))
    assert await run_cap.read_spend(conn, ad) == pytest.approx(0.25)


# ── AA-838 (S291, C4.3 W48 finding 3): the per-day rolled test and the tenant branches return the old sums ─────

_OLD_APP = "coalesce(l.app, l.quality_signal ->> 'app', 'cis')"
OLD_TODAY_SQL = f"""
    SELECT coalesce(sum(l.cost_usd), 0)::float AS usd
      FROM shared.llm_call_log l
     WHERE l.created_at >= $1 AND {_OLD_APP} = $2
"""
OLD_BASELINE_SQL = f"""
    WITH days AS (
        SELECT generate_series($1::date, $2::date - 1, interval '1 day')::date AS day
    ), rolled AS (
        SELECT d.day, sum(d.cost_usd)::float AS usd
          FROM shared.llm_call_daily d
         WHERE d.day >= $1 AND d.day < $2 AND d.app = $3
         GROUP BY 1
    ), raw AS (
        SELECT (l.created_at AT TIME ZONE 'UTC')::date AS day, coalesce(sum(l.cost_usd), 0)::float AS usd
          FROM shared.llm_call_log l
         WHERE l.created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
           AND l.created_at < ($2::date::timestamp AT TIME ZONE 'UTC')
           AND {_OLD_APP} = $3
           AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                            WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
         GROUP BY 1
    )
    SELECT coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(r.usd, w.usd, 0)), 0)::float AS median
      FROM days
      LEFT JOIN rolled r USING (day)
      LEFT JOIN raw w USING (day)
"""
# AA-1179 (S293): the S291 statement (one scan of the window grouped by UTC day, the rolled test per grouped day),
# replaced by per-day LATERAL sums on a raw created_at range.
S291_BASELINE_SQL = f"""
    WITH days AS (
        SELECT generate_series($1::date, $2::date - 1, interval '1 day')::date AS day
    ), rolled AS (
        SELECT d.day, sum(d.cost_usd)::float AS usd
          FROM shared.llm_call_daily d
         WHERE d.day >= $1 AND d.day < $2 AND d.app = $3
         GROUP BY 1
    ), raw_days AS (
        SELECT (l.created_at AT TIME ZONE 'UTC')::date AS day, coalesce(sum(l.cost_usd), 0)::float AS usd
          FROM shared.llm_call_log l
         WHERE l.created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
           AND l.created_at < ($2::date::timestamp AT TIME ZONE 'UTC')
           AND {_OLD_APP} = $3
         GROUP BY 1
    ), raw AS (
        SELECT g.day, g.usd
          FROM raw_days g
         WHERE NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d WHERE d.day = g.day)
    )
    SELECT coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(r.usd, w.usd, 0)), 0)::float AS median
      FROM days
      LEFT JOIN rolled r USING (day)
      LEFT JOIN raw w USING (day)
"""
OLD_TENANT_COST_TOTAL_SQL = """
    SELECT COALESCE(SUM(cost_usd), 0) FROM shared.llm_call_log WHERE tenant_id = $1 OR ($2 AND tenant_id IS NULL)
"""
OLD_TENANT_COST_WINDOW_SQL = """
    SELECT COALESCE(SUM(cost_usd), 0) FROM shared.llm_call_log
     WHERE (tenant_id = $1 OR ($2 AND tenant_id IS NULL)) AND created_at >= $3 AND created_at < $4
"""
APPS = ("cis", "crm", "tripplanner", "booking")


async def _ledger_mix(c):
    """Rows of every app shape on five days of February 2021: the app column, an old row with the app only in
    quality_signal, an old row with neither (a CIS row), and a day already rolled up (its raw rows must not count)."""
    for offset, cost in ((1, 1.25), (2, 2.5), (3, 0.75), (4, 4.0), (5, 3.0)):
        day = date(2021, 2, 10 - offset)
        await _row(c, day=day, hour=3, cost=cost, app="crm")
        await _row(c, day=day, hour=4, cost=cost / 2, qs_app="crm")
        await _row(c, day=day, hour=5, cost=cost * 3)                          # neither: cis
        await _row(c, day=day, hour=6, cost=cost + 1, app="tripplanner")
        await _row(c, day=day, hour=7, cost=0.5, app="cis", qs_app="crm")      # the column wins over quality_signal
    await c.execute(
        "INSERT INTO shared.llm_call_daily (day, app, tenant_id, stage, role, model, provider, account, is_shadow, "
        "calls, fallback_calls, tokens_in, tokens_out, cost_usd, updated_at) VALUES "
        "($1, 'crm', NULL, $2, 'writer', 'm-probe', 'bedrock', 'acc3', false, 3, 0, 30, 15, 6.5, now())",
        date(2021, 2, 7), STAGE)
    for hour, cost in ((1, 0.2), (8, 1.1), (9, 7.0)):
        await _row(c, day=TODAY, hour=hour, cost=cost, app="crm" if hour != 9 else None, qs_app="crm" if hour == 8
                   else None)


@pytest.mark.asyncio
async def test_today_and_baseline_reads_equal_the_per_row_reads(conn):
    await _ledger_mix(conn)
    for app in APPS:
        for since in (_ts(TODAY, 0), _ts(TODAY, 5)):
            old = await conn.fetchval(OLD_TODAY_SQL, since, app)
            assert await conn.fetchval(ab.TODAY_SQL, since, app) == pytest.approx(old)
        for days in (1, 3, 5, 7):
            start, end = ab.baseline_window(TODAY, days)
            old = await conn.fetchval(OLD_BASELINE_SQL, start, end, app)
            assert await conn.fetchval(S291_BASELINE_SQL, start, end, app) == old, (app, days)
            assert await conn.fetchval(ab.BASELINE_SQL, start, end, app) == old, (app, days)
    # the fixture reaches every branch: an old quality_signal row, an old CIS row, a rolled day
    assert await ab.read_spend_since(conn, "crm", _ts(TODAY, 0)) == pytest.approx(1.3)
    assert await ab.read_spend_since(conn, "cis", _ts(TODAY, 0)) == pytest.approx(7.0)
    assert await ab.read_median(conn, "crm", TODAY, 5) == pytest.approx(4.5)    # [1.875, 3.75, 6.5, 6, 4.5]


@pytest.mark.asyncio
async def test_tenant_cost_sums_in_two_branches_equal_the_or_sums(conn):
    from api.services import tenant360_lists as t360
    tenant, internal = "0aa83800-0000-4000-8000-000000000001", "00000000-0000-0000-0000-000000000001"
    for t in (tenant, internal):
        await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3) "
                           "ON CONFLICT (tenant_id) DO NOTHING", t, f"aa838 {t[:8]}", f"aa838-{t[:8]}")
    rows = ((tenant, 1.5, 1), (tenant, 2.25, 40), (internal, 0.75, 2), (None, 3.0, 3), (None, 5.0, 45))
    for tid, cost, days_ago in rows:
        await conn.execute(
            "INSERT INTO shared.llm_call_log (id, tenant_id, stage, role, model, cost_usd, quality_signal, created_at) "
            "VALUES (gen_random_uuid(), $1, $2, 'writer', 'm-probe', $3, '{}'::jsonb, $4)",
            tid, STAGE, cost, _ts(TODAY, 12) - timedelta(days=days_ago))
    since, until = _ts(TODAY, 12) - timedelta(days=30), _ts(TODAY, 12)
    for tid in (tenant, internal):
        for is_internal in (False, True):
            old = await conn.fetchval(OLD_TENANT_COST_TOTAL_SQL, tid, is_internal)
            new = await conn.fetchval(t360.TENANT_COST_TOTAL_SQL, tid, is_internal)
            assert new == old
            old = await conn.fetchval(OLD_TENANT_COST_WINDOW_SQL, tid, is_internal, since, until)
            new = await conn.fetchval(t360.TENANT_COST_WINDOW_SQL, tid, is_internal, since, until)
            assert new == old


# ── AA-1179 (S293): per-day LATERAL sums of the baseline, the edges of a UTC day and the plan shape ──────────────

@pytest.mark.asyncio
async def test_baseline_per_day_sums_equal_both_earlier_statements_at_the_day_edges(conn):
    """Rows on the first and last microsecond of a UTC day, on the window's start and end bounds, under a session time
    zone that is not UTC; a day rolled up for another app only (its raw rows are dropped for every app, the
    platform_cost rule); an empty window."""
    await conn.execute("SET LOCAL TimeZone = 'Asia/Ho_Chi_Minh'")
    await _ledger_mix(conn)
    edge = date(2021, 2, 3)
    for ts, cost, app in ((datetime(2021, 2, 3, 0, 0, tzinfo=UTC), 1.5, "crm"),
                          (datetime(2021, 2, 3, 23, 59, 59, 999999, tzinfo=UTC), 2.25, "crm"),
                          (datetime(2021, 2, 4, 0, 0, tzinfo=UTC), 0.5, "crm"),
                          (datetime(2021, 2, 2, 23, 59, 59, 999999, tzinfo=UTC), 9.0, "crm"),   # before a 7-day window
                          (datetime(2021, 2, 10, 0, 0, tzinfo=UTC), 11.0, "crm"),                # today: never baseline
                          (datetime(2021, 2, 1, 12, 0, tzinfo=UTC), 4.0, "tripplanner")):
        await conn.execute(
            "INSERT INTO shared.llm_call_log (id, stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal, "
            "created_at, app) VALUES (gen_random_uuid(), $1, 'writer', 'm-probe', 1, 1, $2, '{}'::jsonb, $3, $4)",
            STAGE, cost, ts, app)
    await conn.execute(
        "INSERT INTO shared.llm_call_daily (day, app, tenant_id, stage, role, model, provider, account, is_shadow, "
        "calls, fallback_calls, tokens_in, tokens_out, cost_usd, updated_at) VALUES "
        "($1, 'tripplanner', NULL, $2, 'writer', 'm-probe', 'bedrock', 'acc3', false, 1, 0, 1, 1, 0.5, now())",
        date(2021, 2, 1), STAGE)
    for app in APPS:
        for start, end in ((edge, TODAY), (date(2021, 1, 31), TODAY), (edge, edge + timedelta(days=1)),
                           (TODAY, TODAY), (date(2021, 2, 1), date(2021, 2, 2))):
            old = await conn.fetchval(OLD_BASELINE_SQL, start, end, app)
            assert await conn.fetchval(S291_BASELINE_SQL, start, end, app) == old, (app, start, end)
            assert await conn.fetchval(ab.BASELINE_SQL, start, end, app) == old, (app, start, end)
    # the edge day holds exactly its two crm rows (00:00:00 and 23:59:59.999999), not the 00:00 of the next day
    assert await conn.fetchval(ab.BASELINE_SQL, edge, edge + timedelta(days=1), "crm") == pytest.approx(3.75)
    # 2021-02-01 is rolled up for tripplanner only: its raw rows count for no app, the rolled row for tripplanner
    assert await conn.fetchval(ab.BASELINE_SQL, date(2021, 2, 1), date(2021, 2, 2), "tripplanner") == 0.5
    assert await conn.fetchval(ab.BASELINE_SQL, date(2021, 2, 1), date(2021, 2, 2), "crm") == 0.0


@pytest.mark.asyncio
async def test_baseline_plan_sums_each_day_on_a_created_at_range_without_a_sort(conn):
    """The plan guard of AA-1179: no Sort of ledger rows by an expression of created_at (the grouped form sorted the
    whole window and spilled to temp on Dev), and the raw part is a per-day sum (a nested loop over the days whose
    ledger read is bounded by the day's created_at range, the rolled test a one-time filter)."""
    start, end = ab.baseline_window(TODAY, 28)
    rows = await conn.fetch("EXPLAIN (VERBOSE) " + ab.BASELINE_SQL.replace("$1", f"'{start}'::date")
                            .replace("$2", f"'{end}'::date").replace("$3", "'cis'::text"))
    plan = "\n".join(r[0] for r in rows)
    assert "Sort Key" not in plan, plan                                   # only llm_call_daily is grouped (index order)
    assert "timezone('UTC'::text, l.created_at)" not in plan, plan       # no expression on the ledger column
    assert "Nested Loop Left Join" in plan and "One-Time Filter: (NOT " in plan, plan
    assert "l.created_at >= " in plan and "l.created_at < " in plan, plan
