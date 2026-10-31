"""AA-804 (UI part 1) — the AI providers page SQL against real Postgres (migrations applied by conftest).

- trace_job: one job's rows from llm_call_log, dfs_call_log, geo.geocode_call and decision_log in time order; the two
  ledgers without a job index are read only inside the job's run window; another job's rows never show.
- read_providers: every source query runs on the real schema (no error row), the month ledger attributes a row to
  its account, and Jev / DataForSEO / Geoapify rows are always listed.

The fixture rows live in July 2024 (no other test writes there) and are deleted at the end. Names are invented.
"""
import json
import os
import sys
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import ai_gateway as gw  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
KIND = "aa804_probe"
STAGE = "aa804_probe_stage"
EP = "aa804_probe_endpoint"
ACCOUNT = "aa804-probe-conn"
GEO = "aa804_probe_geo"


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield p
    async with p.acquire() as c:
        await c.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
        await c.execute("DELETE FROM shared.dfs_call_log WHERE endpoint = $1", EP)
        await c.execute("DELETE FROM geo.geocode_call WHERE provider = $1", GEO)
        await c.execute("DELETE FROM shared.decision_log WHERE stage = $1", STAGE)
        await c.execute("DELETE FROM shared.job WHERE kind = $1", KIND)
    await p.close()


async def _job(c, started, finished):
    return await c.fetchval(
        """INSERT INTO shared.job (kind, payload, status, created_by, started_at, finished_at)
           VALUES ($1, $2::jsonb, 'succeeded', 'admin:probe', $3, $4) RETURNING id::text""",
        KIND, json.dumps({"tour_id": None}), started, finished)


async def _llm(c, at, cost, job_id, provider="bedrock-satellite", account=ACCOUNT, signal="{}"):
    await c.execute(
        """INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, provider, account, job_id,
                                            created_at, tokens_in, tokens_out)
           VALUES ($1, 'writer', 'probe-model', $2, $3::jsonb, $4, $5, $6::uuid, $7, 10, 20)""",
        STAGE, cost, signal, provider, account, job_id, at)


async def _dfs(c, at, cost, job_id):
    await c.execute(
        """INSERT INTO shared.dfs_call_log (endpoint, cost_usd, cache_hit, fetched_live, keyword_count, job_id,
                                            created_at)
           VALUES ($1, $2, false, true, 3, $3::uuid, $4)""", EP, cost, job_id, at)


async def _geo(c, at, job_id, credits=2):
    await c.execute(
        """INSERT INTO geo.geocode_call (called_at, provider, requests, cost_usd, job_id, endpoint, credits)
           VALUES ($1, $2, 1, 0, $3::uuid, 'search', $4)""", at, GEO, job_id, credits)


async def _decision(c, at, job_id, zone="accept", error=None):
    await c.execute(
        """INSERT INTO shared.decision_log (created_at, stage, question_key, subject_key, job_id, mode, zone, model,
                                            latency_ms, cost_usd, error, cached)
           VALUES ($1, $2, 'aa804_q', 'aa804_subject', $3::uuid, 'shadow', $4, 'probe-jev', 300, 0.001, $5, false)""",
        at, STAGE, job_id, zone, error)


@pytest.mark.asyncio
async def test_trace_unions_the_four_ledgers_inside_the_run_window(pool):
    start, end = _utc(2024, 7, 10, 1), _utc(2024, 7, 10, 3)
    async with pool.acquire() as c:
        job = await _job(c, start, end)
        other = await _job(c, start, end)
        await _llm(c, _utc(2024, 7, 10, 1, 5), 0.10, job)
        await _llm(c, _utc(2024, 7, 10, 2, 0), 0.20, job, signal='{"shadow": true}')
        await _llm(c, _utc(2024, 7, 10, 1, 30), 9.00, other)
        await _dfs(c, _utc(2024, 7, 10, 1, 10), 0.05, job)
        await _geo(c, _utc(2024, 7, 10, 1, 20), job)
        await _geo(c, _utc(2024, 7, 12, 1, 20), job)                  # outside the run window: not read
        await _decision(c, _utc(2024, 7, 10, 1, 15), job, zone="error", error="probe failure")
        await _decision(c, _utc(2024, 7, 9, 23, 0), job)               # before the start: not read
        await _decision(c, _utc(2024, 7, 10, 1, 16), other)

    out = await gw.trace_job(pool, job, now=_utc(2024, 7, 20))
    assert [e["source"] for e in out["events"]] == ["llm", "dfs", "jev", "geocoder", "llm"]
    assert out["events"][-1]["outcome"] == "shadow" and out["events"][2]["outcome"] == "error"
    assert out["sources"]["llm"]["events"] == 2 and out["sources"]["llm"]["cost_usd"] == 0.3
    assert out["sources"]["jev"]["events"] == 1 and out["sources"]["geocoder"]["events"] == 1
    assert out["totals"]["events"] == 5 and out["job"]["kind"] == KIND
    assert all(v["error"] is None for v in out["sources"].values())

    limited = await gw.trace_job(pool, job, limit=1, now=_utc(2024, 7, 20))
    assert limited["sources"]["llm"] == {"events": 2, "shown": 1, "cost_usd": 0.3, "truncated": True, "read": True,
                                         "error": None}

    assert await gw.trace_job(pool, "00000000-0000-4000-8000-000000000804") is None


@pytest.mark.asyncio
async def test_providers_queries_run_on_the_real_schema(pool):
    now = _utc(2024, 7, 20, 12)
    async with pool.acquire() as c:
        await _llm(c, _utc(2024, 7, 20, 9), 0.40, None, provider="openai")
        await _llm(c, _utc(2024, 7, 3, 9), 1.00, None, provider="openai")
        await _llm(c, _utc(2024, 6, 30, 23), 5.00, None, provider="openai")   # last month: not counted
        await _llm(c, _utc(2024, 7, 20, 8), 0.02, None, provider="typesafe", account=None)
        await _dfs(c, _utc(2024, 7, 20, 7), 0.30, None)
        await _geo(c, _utc(2024, 7, 20, 6), None, credits=5)

    day, month = gw.day_and_month_start(now)
    ledger = [dict(r) for r in await pool.fetch(gw.LEDGER_SQL, day, month, now)]
    mine = [r for r in ledger if r["account"] == ACCOUNT]
    assert len(mine) == 1 and mine[0]["month_usd"] == pytest.approx(1.4) and mine[0]["today_usd"] == pytest.approx(0.4)
    assert mine[0]["month_calls"] == 2
    geo = await pool.fetchrow(gw.GEO_SQL, day, month, now, GEO)
    assert geo["credits_today"] == 5 and geo["month_requests"] == 1

    out = await gw.read_providers(pool, now=now)
    assert out["errors"] == [], out["errors"]
    keys = [p["key"] for p in out["providers"]]
    assert keys[-3:] == ["jev", "dataforseo", "geoapify"]
    assert any(k.startswith("llm:") for k in keys), "the seeded connections are listed"
    # the probe account names no connection: its spend is the unattributed line
    assert out["unattributed_llm"]["month_usd"] >= 1.4
    by = {p["key"]: p for p in out["providers"]}
    assert by["jev"]["spend"]["month_usd"] >= 0.02
    assert by["dataforseo"]["spend"]["today_usd"] >= 0.3
    assert out["month_start"] == month.isoformat() == _utc(2024, 7, 1).isoformat()
