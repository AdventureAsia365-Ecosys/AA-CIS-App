"""AA-858 against real Postgres: the SQL behind the TripPlanner and Booking cards of GET /admin/overview/ops.

The producer views (tp-015, bk-017) live in other repos and are not in this DB. The fixture creates stand-in views of
the CONTRACT SHAPE only (``tripplanner.v_ops_traffic_daily (day, metric, n)``, ``booking.v_ops_funnel_daily (day,
metric, source_system, n)``, ``booking.v_ops_case_status (status, n)``) from VALUES lists, and drops exactly what it
created afterwards (lesson S237: no global stand-ins left behind; another test owns ``tripplanner`` tables).

- daily TripPlanner series over 14 UTC days, AI searches from shared.llm_call_log stage tp_search_embed;
- Booking weeks (date_trunc('week') = ISO Monday), attention counts over the whole view, cases by status;
- a missing view → status unavailable / contract_view_missing (no exception);
- the cost card runs on AA-884's cost_by_app against the real tables without Redis (no CE call).
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.services.ops_overview import activity, cost  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


def _values(rows):
    return ", ".join("(" + ", ".join(f"'{v}'::date" if i == 0 else (f"'{v}'" if isinstance(v, str) else str(v))
                                     for i, v in enumerate(r)) + ")" for r in rows)


@pytest_asyncio.fixture
async def env():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    created_schemas = []
    for schema in ("tripplanner", "booking"):
        exists = await pool.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema)
        if not exists:
            await pool.execute(f"CREATE SCHEMA {schema}")
            created_schemas.append(schema)
    tp_rows = [
        (TODAY, "planner_session_started", 5),
        (TODAY, "planner_session_started", 1),            # two rows of a day are summed
        (TODAY - timedelta(days=2), "trip_started", 3),
        (TODAY - timedelta(days=9), "inquiry_sent", 2),
        (TODAY - timedelta(days=20), "planner_session_started", 100),   # outside 14 days
        (TODAY, "metric_added_later", 7),                 # unknown names are ignored
    ]
    await pool.execute(f"CREATE VIEW tripplanner.v_ops_traffic_daily AS SELECT * FROM (VALUES {_values(tp_rows)}) "
                       "AS t(day, metric, n)")
    bk_rows = [
        (MONDAY, "inquiry_received", "tripplanner", 3),
        (MONDAY, "inquiry_received", "cis", 1),             # a second source, same day
        (MONDAY, "trip_case_created", "tripplanner", 2),
        (MONDAY - timedelta(weeks=1), "proposal_sent", "manual", 2),
        (MONDAY - timedelta(weeks=1), "confirmed", "manual", 1),
        (MONDAY - timedelta(weeks=30), "inquiry_parked", "tripplanner", 1),   # attention counts all rows
        (MONDAY - timedelta(weeks=30), "trip_case_created", "manual", 50),    # outside 8 weeks
    ]
    await pool.execute(f"CREATE VIEW booking.v_ops_funnel_daily AS SELECT * FROM (VALUES {_values(bk_rows)}) "
                       "AS t(day, metric, source_system, n)")
    await pool.execute("CREATE VIEW booking.v_ops_case_status AS SELECT * FROM (VALUES ('new_inquiry', 2::bigint), "
                       "('building', 1::bigint), ('completed', 4::bigint)) AS t(status, n)")
    tag = f"aa858-{uuid.uuid4().hex[:8]}"
    base = await pool.fetchval(
        "SELECT count(*) FROM shared.llm_call_log WHERE stage = 'tp_search_embed' AND created_at >= $1",
        datetime.combine(TODAY - timedelta(days=13), datetime.min.time(), tzinfo=timezone.utc))
    for ts in (NOW, NOW, NOW - timedelta(days=3), NOW - timedelta(days=30)):
        await pool.execute(
            "INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, created_at) "
            "VALUES ('tp_search_embed', 'embed', 'cohere-embed-v4', 0, $1::jsonb, $2)",
            f'{{"app": "tripplanner", "test": "{tag}"}}', ts)
    yield pool, int(base or 0)
    await pool.execute("DELETE FROM shared.llm_call_log WHERE quality_signal ->> 'test' = $1", tag)
    await pool.execute("DROP VIEW IF EXISTS tripplanner.v_ops_traffic_daily")
    await pool.execute("DROP VIEW IF EXISTS booking.v_ops_funnel_daily, booking.v_ops_case_status")
    for schema in created_schemas:
        await pool.execute(f"DROP SCHEMA {schema}")
    await pool.close()


@pytest.mark.asyncio
async def test_tripplanner_daily_series_and_searches(env):
    pool, base_searches = env
    out = await activity.tripplanner_activity(pool, NOW)
    assert out["status"] == "ok" and len(out["days"]) == 14
    days = {d["day"]: d for d in out["days"]}
    assert days[TODAY.isoformat()]["planner_session_started"] == 6
    assert days[(TODAY - timedelta(days=2)).isoformat()]["trip_started"] == 3
    assert days[(TODAY - timedelta(days=9)).isoformat()]["inquiry_sent"] == 2
    assert out["totals_7d"]["planner_session_started"] == 6 and out["prev_7d"]["inquiry_sent"] == 2
    searches = sum(d["ai_search"] for d in out["days"])
    assert searches == base_searches + 3          # the 30-day-old row is outside the window
    assert days[TODAY.isoformat()]["ai_search"] >= 2


@pytest.mark.asyncio
async def test_booking_weeks_status_and_attention(env):
    pool, _ = env
    out = await activity.booking_funnel(pool, NOW)
    assert out["status"] == "ok" and len(out["weeks"]) == 8
    this_week = out["weeks"][-1]
    assert this_week["week_start"] == MONDAY.isoformat()
    assert this_week["inquiry_received"] == 4 and this_week["trip_case_created"] == 2
    last_week = out["weeks"][-2]
    assert last_week["proposal_sent"] == 2 and last_week["confirmed"] == 1
    assert out["totals"]["trip_case_created"] == 2          # the 30-week-old 50 is outside
    assert out["inbox_attention"] == {"parked": 1, "failed": 0}
    assert out["open_cases"] == 3
    assert {s["status"]: s["n"] for s in out["cases_by_status"]}["completed"] == 4


@pytest.mark.asyncio
async def test_missing_views_are_unavailable(env):
    pool, _ = env
    await pool.execute("DROP VIEW tripplanner.v_ops_traffic_daily")
    await pool.execute("DROP VIEW booking.v_ops_funnel_daily")
    tp = await activity.tripplanner_activity(pool, NOW)
    bk = await activity.booking_funnel(pool, NOW)
    assert tp["status"] == bk["status"] == "unavailable"
    assert tp["reason"] == bk["reason"] == "contract_view_missing"


@pytest.mark.asyncio
async def test_cost_card_on_real_tables_without_redis(env):
    pool, _ = env
    out = await cost.cost_card(pool, None, NOW)
    assert out["status"] == "ok"
    keys = [r["key"] for r in out["rows"]]
    assert set(keys[:3]) == {"cis", "tripplanner", "booking"}
    assert out["refresh"]["attempted"] is False        # no Redis → never a Cost Explorer call
