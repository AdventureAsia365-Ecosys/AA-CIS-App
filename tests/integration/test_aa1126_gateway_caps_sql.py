"""AA-1126 (S283) against real Postgres: the ledger reads of the anomaly breaker (G2: an app's spend since a time, the
median day over rolled-up and raw days) and of the run caps (G4: a job's spend, an eval run's, an ad-hoc stage's), as
written, on the replayed schema. Fixture days in February 2021 (no other test writes them), invented stages and ids;
everything runs in a transaction that is rolled back."""
import json
import os
import sys
from datetime import date, datetime, timezone

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
    try:
        yield c
    finally:
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
