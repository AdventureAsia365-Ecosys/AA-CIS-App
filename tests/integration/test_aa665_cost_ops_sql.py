"""AA-665 — Cost Operations SQL against real Postgres (migrations applied by conftest, 293 included).

C2: shared.dfs_call_log rows written through record_dfs_call_with_pool carry the bound job id; dfs_runs groups them
by job (job fields, payload scope, the job's LLM cost) and, with infer_unlinked, matches rows without a job to the job
whose run window contains them, or to a recorded cost incident (api/services/cost_incidents.py) when no job ran.
C3: cost_forecast over llm_call_daily (rolled day wins over raw rows of that day), llm_call_log, dfs_call_log and
the stored Cost Explorer snapshot, against the spend_budget rows.

The fixture rows live in January / February 2025 (no other test writes there) and are deleted at the end.
"""
import json
import os
import sys
from datetime import date, datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import cost_incidents, cost_ops  # noqa: E402
from shared.dfs_client.call_log import record_dfs_call_with_pool  # noqa: E402
from shared.llm_client.call_log import bind_job  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
KIND = "aa665_probe"
EP = "aa665_probe_endpoint"
STAGE = "aa665_probe_stage"
SERVICE = "AA665 probe service"
ACC2_ID = "111122223333"           # conftest: shared.aws_client.bronze.caller_account_id (acc2) in tests


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    yield p
    async with p.acquire() as c:
        await c.execute("DELETE FROM shared.dfs_call_log WHERE endpoint = $1", EP)
        await c.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
        await c.execute("DELETE FROM shared.llm_call_daily WHERE stage = $1", STAGE)
        await c.execute("DELETE FROM shared.cost_explorer_snapshot WHERE service = $1", SERVICE)
        await c.execute("DELETE FROM shared.job WHERE kind = $1", KIND)
    await p.close()


async def _job(c, started, finished, payload, status="failed", created_by="admin:probe"):
    return await c.fetchval(
        """INSERT INTO shared.job (kind, payload, status, created_by, cost_usd, error, started_at, finished_at)
           VALUES ($1, $2::jsonb, $3, $4, 1.9, 'DataForSEO budget stop', $5, $6) RETURNING id::text""",
        KIND, json.dumps(payload), status, created_by, started, finished)


async def _dfs_at(c, created_at, cost, job_id=None):
    await c.execute(
        """INSERT INTO shared.dfs_call_log (endpoint, cost_usd, cache_hit, fetched_live, keyword_count, job_id,
                                            created_at)
           VALUES ($1, $2, false, true, 10, $3::uuid, $4)""", EP, cost, job_id, created_at)


async def _llm(c, created_at, cost, provider, account=None, job_id=None):
    await c.execute(
        """INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, provider, account, job_id,
                                            created_at)
           VALUES ($1, 'writer', 'probe-model', $2, '{}'::jsonb, $3, $4, $5::uuid, $6)""",
        STAGE, cost, provider, account, job_id, created_at)


@pytest.mark.asyncio
async def test_dfs_log_carries_the_bound_job_and_runs_group_by_job(pool):
    async with pool.acquire() as c:
        job_id = await _job(c, _utc(2025, 1, 10, 1), _utc(2025, 1, 10, 3),
                            {"country": "Bhutan", "segment_ids": [1, 2, 3]})
        await _llm(c, _utc(2025, 1, 10, 2), 0.25, "bedrock", "acc3", job_id)
    with bind_job(job_id):
        await record_dfs_call_with_pool(pool, endpoint=EP, fetched_live=True, cost_usd=1.5, keyword_count=40)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT job_id::text FROM shared.dfs_call_log WHERE endpoint = $1", EP) == job_id
        # place the logged row inside the job's run, then one more linked and two unlinked rows
        await c.execute("UPDATE shared.dfs_call_log SET created_at = $2 WHERE endpoint = $1", EP,
                        _utc(2025, 1, 10, 1, 30))
        await _dfs_at(c, _utc(2025, 1, 10, 2), 0.5, job_id)
        await _dfs_at(c, _utc(2025, 1, 10, 2, 30), 0.2)          # no job id, inside the job's window
        await _dfs_at(c, _utc(2025, 1, 10, 5), 0.1)              # no job id, no job running

    out = await cost_ops.dfs_runs(pool, _utc(2025, 1, 10), _utc(2025, 1, 11), infer_unlinked=True)
    assert out["summary"]["calls"] == 4 and out["summary"]["linked_calls"] == 2
    assert out["summary"]["dfs_cost_usd"] == pytest.approx(2.3) and out["summary"]["jobs"] == 1
    linked = next(r for r in out["runs"] if r["job_id"] == job_id)
    assert linked["calls"] == 2 and linked["dfs_cost_usd"] == pytest.approx(2.0)
    assert linked["llm_cost_usd"] == pytest.approx(0.25) and linked["endpoints"] == [EP]
    job = linked["job"]
    assert job["kind"] == KIND and job["status"] == "failed" and job["created_by"] == "admin:probe"
    assert job["scope"] == {"country": "Bhutan", "segment_ids": "3 items"}
    assert job["job_reported_cost_usd"] == pytest.approx(1.9) and job["error"] == "DataForSEO budget stop"
    unlinked = next(r for r in out["runs"] if r["job_id"] is None)
    assert unlinked["calls"] == 2 and unlinked["job"] is None

    inferred = {r["attribution"]: r for r in out["inferred"]}
    assert inferred["window"]["job_id"] == job_id and inferred["window"]["calls"] == 1
    assert inferred["window"]["job"]["kind"] == KIND
    assert inferred["none"]["calls"] == 1 and inferred["none"]["job_id"] is None


@pytest.mark.asyncio
async def test_two_overlapping_jobs_make_an_ambiguous_match(pool):
    async with pool.acquire() as c:
        await _job(c, _utc(2025, 1, 12, 1), _utc(2025, 1, 12, 4), {"a": 1})
        await _job(c, _utc(2025, 1, 12, 2), _utc(2025, 1, 12, 3), {"b": 2}, status="succeeded")
        await _dfs_at(c, _utc(2025, 1, 12, 2, 30), 0.3)
    out = await cost_ops.dfs_runs(pool, _utc(2025, 1, 12), _utc(2025, 1, 13), infer_unlinked=True)
    (row,) = out["inferred"]
    assert row["attribution"] == "ambiguous" and row["job_id"] is None and row["candidate_kinds"] == [KIND]


@pytest.mark.asyncio
async def test_rows_without_a_job_inside_a_recorded_incident_are_attributed_to_it(pool, monkeypatch):
    inc = cost_incidents.CostIncident(
        id="aa665-probe", provider="dfs", title="Probe incident", started_at=_utc(2025, 1, 14, 5),
        ended_at=_utc(2025, 1, 14, 11), trigger="t", scope="s", failure_point="f", cause="c",
        llm_stages=(STAGE,))
    monkeypatch.setattr(cost_ops, "incidents_overlapping",
                        lambda since, until, provider=None: cost_incidents.incidents_overlapping(
                            since, until, provider, (inc,)))
    async with pool.acquire() as c:
        job_id = await _job(c, _utc(2025, 1, 14, 6), _utc(2025, 1, 14, 7), {"a": 1})
        await _dfs_at(c, _utc(2025, 1, 14, 5, 5), 0.5)            # incident, no job
        await _dfs_at(c, _utc(2025, 1, 14, 8, 51), 0.25)          # incident, no job: the last paid call
        await _dfs_at(c, _utc(2025, 1, 14, 9, 30), 0.0)           # incident, failed call logged at 0
        await _dfs_at(c, _utc(2025, 1, 14, 6, 30), 0.1)           # incident window, but a job ran: the job wins
        await _dfs_at(c, _utc(2025, 1, 14, 12), 0.05)             # after the incident, no job
        await _llm(c, _utc(2025, 1, 14, 5, 6), 0.01, "bedrock", "acc3")
        await _llm(c, _utc(2025, 1, 14, 5, 7), 0.02, "bedrock", "acc3")

    out = await cost_ops.dfs_runs(pool, _utc(2025, 1, 14), _utc(2025, 1, 15), infer_unlinked=True)
    (view,) = out["incidents"]
    assert view["id"] == "aa665-probe" and view["dfs"]["calls"] == 4
    assert view["dfs"]["cost_usd"] == pytest.approx(0.85)
    assert view["dfs"]["first_paid_at"] == _utc(2025, 1, 14, 5, 5).isoformat()
    assert view["dfs"]["last_paid_at"] == _utc(2025, 1, 14, 8, 51).isoformat()
    assert view["llm"]["source"] == "raw" and view["llm"]["calls"] == 2
    assert view["llm"]["cost_usd"] == pytest.approx(0.03)

    inferred = {r["attribution"]: r for r in out["inferred"]}
    assert inferred["incident"]["incident_id"] == "aa665-probe" and inferred["incident"]["calls"] == 3
    assert inferred["incident"]["incident_title"] == "Probe incident" and inferred["incident"]["job_id"] is None
    assert inferred["incident"]["dfs_cost_usd"] == pytest.approx(0.75)
    assert inferred["window"]["job_id"] == job_id and inferred["window"]["calls"] == 1
    assert inferred["none"]["calls"] == 1 and inferred["none"]["incident_id"] is None


@pytest.mark.asyncio
async def test_forecast_reads_rolled_raw_dfs_ce_and_budgets(pool, monkeypatch):
    monkeypatch.setattr("shared.aws_client.bronze.caller_account_id", lambda: ACC2_ID)   # as conftest does
    monkeypatch.delenv(cost_ops.AWS_BUDGET_ENV, raising=False)
    now = _utc(2025, 2, 20, 12)
    async with pool.acquire() as c:
        for d in range(13, 20):                                   # 13..19/02: 1 USD a day on acc3, raw rows
            await _llm(c, _utc(2025, 2, d, 10), 1.0, "bedrock", "acc3")
        # 12/02 is rolled up: the daily row counts, a raw row left on that day does not
        await c.execute(
            """INSERT INTO shared.llm_call_daily (day, app, tenant_id, stage, role, model, provider, account, is_shadow,
                                                  calls, fallback_calls, tokens_in, tokens_out, cost_usd, updated_at)
               VALUES ('2025-02-12', 'cis', NULL, $1, 'writer', 'probe-model', 'bedrock', 'acc3', false,
                       3, 0, 0, 0, 3.0, now())""", STAGE)
        await _llm(c, _utc(2025, 2, 12, 9), 50.0, "bedrock", "acc3")
        await _llm(c, _utc(2025, 2, 20, 9), 0.4, "typesafe")       # today, partial
        await _dfs_at(c, _utc(2025, 2, 19, 8), 0.7)
        for d in range(1, 18):                                     # CE acc2 1..17/02 at 2 USD
            await c.execute(
                """INSERT INTO shared.cost_explorer_snapshot (account_id, service, period_start, period_end, amount_usd,
                                                              unit)
                   VALUES ($1, $2, $3, $4, 2.0, 'USD')""", ACC2_ID, SERVICE, date(2025, 2, d), date(2025, 2, d + 1))
        budgets = {r["provider"]: r["per_day_usd"]
                   for r in await c.fetch("SELECT provider, per_day_usd::float FROM shared.spend_budget "
                                          "WHERE scope = 'global'")}

    out = await cost_ops.cost_forecast(pool, None, now=now)
    assert out["month"] == "2025-02" and out["days_in_month"] == 28 and out["complete_days"] == 19
    by = {p["key"]: p for p in out["providers"]}
    bed = by["bedrock"]
    assert bed["mtd_usd"] == pytest.approx(3.0 + 7.0)
    assert bed["run_rate_usd_per_day"] == pytest.approx(1.0)
    assert bed["days_left"] == 9 and bed["forecast_usd"] == pytest.approx(10.0 + 9.0)
    assert [a["account"] for a in bed["accounts"]] == ["acc3"]
    assert by["jev"]["mtd_usd"] == pytest.approx(0.4) and by["jev"]["forecast_usd"] == pytest.approx(0.4)
    assert by["dfs"]["mtd_usd"] == pytest.approx(0.7) and by["dfs"]["run_rate_usd_per_day"] == pytest.approx(0.1)
    if budgets.get("dfs") is not None:
        assert by["dfs"]["budget_usd"] == pytest.approx(budgets["dfs"] * 28)
    a2 = next(a for a in out["aws_accounts"] if a["account"] == "acc2")
    assert a2["covered_from"] == "2025-02-01" and a2["covered_to"] == "2025-02-17"
    assert a2["mtd_usd"] == pytest.approx(34.0) and a2["run_rate_usd_per_day"] == pytest.approx(2.0)
    assert a2["days_left"] == 11 and a2["forecast_usd"] == pytest.approx(34.0 + 22.0)
    assert a2["status"] == "ok"
