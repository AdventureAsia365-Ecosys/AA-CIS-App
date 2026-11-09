"""AA-1003 R2 against real Postgres: the SQL of the daily safety net (OPS-12 digest loaders; TP-03 retired by the R0
hotfix, root ADR 0019 B2B-only model).

The producer views (tp-015, bk-017) live in other repos and are not in this DB. As in test_aa858_ops_activity, the
fixture creates stand-in views of the CONTRACT SHAPE only (``tripplanner.v_ops_traffic_daily (day, metric, n)``,
``booking.v_ops_funnel_daily (day, metric, source_system, n)``) from VALUES lists and drops exactly what it created
(lesson S237: no global stand-ins left behind).

- the EX-01 demand reader (money_sources.demand) on the views: 7-day sums per metric; a dropped view is an error
  for that side only (handoff.CONTRACT_ERRORS), and the connection stays usable;
- TP-03 retired: a failed TP-03 run in the last 7 days is not flagged by the digest's reports section;
- the digest loaders' SQL (jobs, deploys, waves) run as written.
Fixture days 2023-05-18/19 (no other test writes them); every row this module writes is deleted at the end.
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.reports import digest, money_sources, periods, registry  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
DAY = date(2023, 5, 18)
NEXT = DAY + timedelta(days=1)
KIND = "aa1003_r2_probe"
UTC = timezone.utc


def _values(rows):
    return ", ".join("(" + ", ".join(f"'{v}'::date" if i == 0 else (f"'{v}'" if isinstance(v, str) else str(v))
                                     for i, v in enumerate(r)) + ")" for r in rows)


@pytest_asyncio.fixture
async def env():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    created_schemas, created_views = [], []
    for schema in ("tripplanner", "booking"):
        if not await pool.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema):
            await pool.execute(f"CREATE SCHEMA {schema}")
            created_schemas.append(schema)
    tp_rows = [(DAY, "inquiry_sent", 3), (DAY, "inquiry_sent", 1),          # two rows of a day are summed
               (NEXT, "inquiry_sent", 2), (DAY, "planner_session_started", 40)]
    await pool.execute(f"CREATE VIEW tripplanner.v_ops_traffic_daily AS SELECT * FROM (VALUES {_values(tp_rows)}) "
                       "AS t(day, metric, n)")
    created_views.append("tripplanner.v_ops_traffic_daily")
    bk_rows = [(DAY, "inquiry_received", "tripplanner", 3),
               (DAY, "inquiry_received", "cis", 5),                        # another source: not TripPlanner's
               (DAY, "inquiry_parked", "tripplanner", 1),
               (DAY, "inquiry_failed", "cis", 1),
               (DAY, "trip_case_created", "tripplanner", 2),
               (DAY, "trip_case_created", "manual", 9),
               (NEXT, "inquiry_received", "tripplanner", 2)]
    await pool.execute(f"CREATE VIEW booking.v_ops_funnel_daily AS SELECT * FROM (VALUES {_values(bk_rows)}) "
                       "AS t(day, metric, source_system, n)")
    created_views.append("booking.v_ops_funnel_daily")
    yield pool, created_views
    for view in created_views:
        await pool.execute(f"DROP VIEW IF EXISTS {view}")
    for schema in created_schemas:
        await pool.execute(f"DROP SCHEMA IF EXISTS {schema}")
    await pool.execute("DELETE FROM shared.job WHERE kind = $1", KIND)
    await pool.execute("DELETE FROM shared.report_run WHERE report_id = 'TP-03' AND triggered_by = $1", KIND)
    await pool.close()


async def test_demand_reads_the_contract_views_as_written(env):
    pool, _ = env
    async with pool.acquire() as c:
        out = await money_sources.demand(c, DAY, NEXT)
    assert out["tripplanner"] == {"inquiry_sent": 6, "planner_session_started": 40}
    assert out["booking"]["inquiry_received"] == 10 and out["booking"]["trip_case_created"] == 11


async def test_a_missing_contract_view_is_an_error_for_its_side_only(env):
    pool, created = env
    await pool.execute("DROP VIEW booking.v_ops_funnel_daily")
    created.remove("booking.v_ops_funnel_daily")
    async with pool.acquire() as c:
        out = await money_sources.demand(c, DAY, NEXT)
        assert "UndefinedTableError" in out["booking"]["error"] and out["tripplanner"]["inquiry_sent"] == 6
        # the connection is still usable afterwards (no transaction left aborted)
        assert await c.fetchval("SELECT 1") == 1


async def test_a_retired_report_s_failed_runs_are_not_flagged(env):
    pool, _ = env
    await pool.execute(
        "INSERT INTO shared.report_run (report_id, period_start, period_end, status, trigger, triggered_by, "
        "finished_at, error) VALUES ('TP-03', $1, $1, 'failed', 'manual', $2, now(), 'UndefinedTableError: x')",
        DAY, KIND)
    async with pool.acquire() as c:
        out = await digest.load_reports(c, registry.load(), datetime.now(UTC))
    assert not any(f["id"] == "TP-03" for f in out["flagged"])


async def test_digest_loaders_run_as_written(env):
    pool, _ = env
    start, _end = periods.day_bounds(DAY)
    await pool.execute(
        "INSERT INTO shared.job (kind, payload, status, error, finished_at) VALUES "
        "($1, '{}'::jsonb, 'failed', 'boom', $2), ($1, '{}'::jsonb, 'failed', 'boom', $2)",
        KIND, start + timedelta(hours=2))
    await pool.execute(
        "INSERT INTO shared.job (kind, payload, status, run_after, idempotency_key) "
        "VALUES ($1, '{}'::jsonb, 'queued', now() - interval '2 hours', $2)", KIND, f"{KIND}:{uuid.uuid4()}")
    now = datetime.now(UTC)
    async with pool.acquire() as c:
        jobs = await digest.load_jobs(c, DAY, now)
        deploys = await digest.load_deploys(c, now)
        waves = await digest.load_waves(c)
    assert {"kind": KIND, "n": 2} in jobs["failed_by_kind"] and jobs["failed_total"] >= 2
    assert jobs["overdue_queued"] >= 1 and jobs["oldest_due_at"] is not None
    assert isinstance(jobs["expired_leases"], int)
    assert isinstance(deploys["workers"], list) and isinstance(deploys["migrations"], list)
    assert isinstance(waves["open"], list)
