"""AA-1003 R2 against real Postgres: the SQL of the daily safety net (TP-03 handoff, OPS-12 digest loaders).

The producer views (tp-015, bk-017) live in other repos and are not in this DB. As in test_aa858_ops_activity, the
fixture creates stand-in views of the CONTRACT SHAPE only (``tripplanner.v_ops_traffic_daily (day, metric, n)``,
``booking.v_ops_funnel_daily (day, metric, source_system, n)``) from VALUES lists and drops exactly what it created
(lesson S237: no global stand-ins left behind).

- handoff.read_days / kpis.inquiry_handoff on the views: per UTC day counts, TripPlanner-only received and cases;
- a dropped view: kpis.inquiry_handoff raises kpis.Unavailable (the daily chain goes on), not a plain error;
- the hourly check's shared.job reads (previous check by idempotency key, recent checks, the digest's last check);
- the gap bell: one INSERT into shared.notifications, throttled for 24 h unless the gap grows;
- the digest loaders' SQL (jobs, deploys, waves) run as written.
Fixture days 2023-05-18/19 (no other test writes them); every row this module writes is deleted at the end.
"""
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

from services.reports import digest, handoff, kpis, periods  # noqa: E402

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
    await pool.execute("DELETE FROM shared.job WHERE kind = $1 OR idempotency_key LIKE 'inquiry_handoff_check:2023-%'",
                       KIND)
    await pool.execute("DELETE FROM shared.notifications WHERE event_type = $1", handoff.GAP_EVENT)
    await pool.close()


def _day_ctx(day=DAY):
    start, end = periods.day_bounds(day)
    return kpis.DayContext(day=day, start=start, end=end, latest=False)


async def test_read_days_counts_the_contract_views_per_utc_day(env):
    pool, _ = env
    async with pool.acquire() as c:
        days = await handoff.read_days(c, DAY - timedelta(days=1), NEXT)
    assert [d.day for d in days] == [DAY - timedelta(days=1), DAY, NEXT]
    d = days[1]
    assert (d.sent, d.received, d.parked, d.failed, d.cases, d.gap) == (4, 3, 1, 1, 2, 1)
    assert (days[2].sent, days[2].received, days[2].gap) == (2, 2, 0) and days[0].gap == 0
    async with pool.acquire() as c:
        out = {v.kpi_id: v.value for v in await kpis.inquiry_handoff(c, _day_ctx())}
    assert out["K-TP-HANDOFFGAP"] == 1 and out["K-BK-PARKED"] == 2 and float(out["K-BK-INQ2CASE"]) == pytest.approx(
        2 / 3, abs=1e-4)


async def test_a_missing_contract_view_is_unavailable(env):
    pool, created = env
    await pool.execute("DROP VIEW booking.v_ops_funnel_daily")
    created.remove("booking.v_ops_funnel_daily")
    async with pool.acquire() as c:
        with pytest.raises(kpis.Unavailable, match="UndefinedTableError"):
            await kpis.inquiry_handoff(c, _day_ctx())
        # the connection is still usable afterwards (no transaction left aborted)
        assert await c.fetchval("SELECT 1") == 1


async def test_hourly_check_job_reads(env):
    pool, _ = env
    hour = datetime(2023, 5, 18, 5, tzinfo=UTC)
    prev_key = handoff.check_key(hour - timedelta(hours=1))
    await pool.execute(
        "INSERT INTO shared.job (kind, payload, status, result, idempotency_key, finished_at) "
        "VALUES ($1, '{}'::jsonb, 'succeeded', $2::jsonb, $3, now())",
        handoff.CHECK_KIND, json.dumps({"gap": 2, "sent": 4, "received": 2, "persistent": False}), prev_key)
    async with pool.acquire() as c:
        prev = await handoff.previous_result(c, hour)
        assert prev["gap"] == 2
        assert await handoff.previous_result(c, hour + timedelta(hours=5)) is None
        recent = await handoff.recent_checks(c, datetime.now(UTC) - timedelta(minutes=5),
                                             datetime.now(UTC) + timedelta(minutes=1))
        assert any(r["result"].get("gap") == 2 and r["status"] == "succeeded" for r in recent)
        last = await digest.load_handoff(c)
    assert last["last_check"]["gap"] == 2 and last["last_check"]["sent"] == 4


async def test_gap_bell_insert_and_throttle(env):
    pool, _ = env
    result = {"gap": 1, "sent": 4, "received": 3, "window": {"first": "2023-05-12", "last": "2023-05-18"}}
    async with pool.acquire() as c:
        assert await handoff.maybe_raise_gap_bell(c, result) is True
        assert await handoff.maybe_raise_gap_bell(c, result) is False             # same gap within 24 h
        assert await handoff.maybe_raise_gap_bell(c, {**result, "gap": 2}) is True  # it grew
        rows = await c.fetch("SELECT tenant_id::text AS tenant_id, actor_type, entity_type, entity_id, payload, "
                             "target_roles FROM shared.notifications WHERE event_type = $1 ORDER BY id",
                             handoff.GAP_EVENT)
    assert len(rows) == 2
    r = rows[-1]
    payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
    assert (r["tenant_id"], r["actor_type"], r["entity_type"], r["entity_id"]) == (
        handoff.PLATFORM_TENANT_ID, "system", "report", "TP-03")
    assert payload["gap"] == 2 and list(r["target_roles"]) == ["admin"]


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
