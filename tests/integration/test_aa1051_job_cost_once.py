"""AA-1051 follow-up (S286) — a job's LLM spend is counted once in shared.job.cost_usd, against a real Postgres.

The W5 proofs showed shared.job.cost_usd at exactly twice the ledger (S1 proof 6.22 vs 3.11 USD): the writing-memory
R2 / shadow handlers passed the run's llm_call_log sum to ctx.add_cost, and the runner (AA-1078 cost stamp) adds the
job's ledger rows on top. Here the REAL handlers run in the worker, their run function faked to write llm_call_log rows
(the worker binds the job id) and return that spend: the column, the Jobs page read and the stamp all equal the ledger
sum once. A handler's non-LLM spend (ctx.add_cost) still adds to it. No AWS, no paid call.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import queue, registry  # noqa: E402
from shared.jobs.worker import Worker  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "adhoc_aa1051"
CALLS = (0.0111, 0.0200)            # two logged calls: the run's ledger sum is 0.0311
LEDGER = round(sum(CALLS), 6)


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    yield p
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
    await p.close()


async def _logged_calls(pool) -> float:
    """What a paid run does inside the job: gateway calls, each logged (the worker bound the job id)."""
    from shared.llm_client import call_log
    for usd in CALLS:
        resp = SimpleNamespace(model_used="haiku-4-5", cost_usd=usd, pricing_mode="catalog", cache_read_tokens=0,
                               cache_write_tokens=0, connection_key=None)
        await call_log.record_call_with_pool(pool, stage=STAGE, role="writer", model=resp.model_used, tokens_in=10,
                                             tokens_out=5, cost_usd=resp.cost_usd, quality_signal={"issue": "AA-1051"},
                                             **call_log.billed_fields(resp))
    return LEDGER


@pytest.fixture
def kinds(monkeypatch):
    """The real writing-memory handlers, their paid run faked; plus a handler with non-LLM spend on top."""
    from services.jobs import writing_memory_job as wm
    from services.writing_memory import r2, shadow

    monkeypatch.setattr(registry, "_KINDS", {})
    registry.job_kind(wm.R2, max_attempts=1)(wm.run_r2_job)
    registry.job_kind(wm.SHADOW, max_attempts=1)(wm.run_shadow_job)

    def conf(payload):
        return SimpleNamespace(triggered_by="manual")

    async def fake_r2(pool, conf, progress=None):
        cost = await _logged_calls(pool)
        return SimpleNamespace(run_id="r2-run", verdict="non_inferior", stop_reason=None, stopped_phase=None,
                               cost_usd=cost, judge_kappa=None, judge_counts={}, kappa_strata_below=[], counts={})

    async def fake_shadow(pool, conf, progress=None):
        return {"run_id": "shadow-run", "verdict": "pass", "stop_reason": None, "cost_usd": await _logged_calls(pool)}

    monkeypatch.setattr(r2, "config_from_payload", conf)
    monkeypatch.setattr(r2, "run_with_pool", fake_r2)
    monkeypatch.setattr(shadow, "config_from_payload", conf)
    monkeypatch.setattr(shadow, "run_shadow", fake_shadow)

    @registry.job_kind("aa1051_mixed", max_attempts=1)
    async def mixed(ctx):
        ctx.add_cost(0.5)                   # non-LLM spend the handler reports (e.g. DataForSEO)
        await _logged_calls(ctx.pool)       # its LLM spend reaches the job through the ledger only
        return {"ok": True}
    return wm


async def _done(pool, job_id: str, timeout: float = 10.0) -> dict:
    for _ in range(int(timeout / 0.1)):
        row = await pool.fetchrow("SELECT status, error, cost_usd::float AS cost_usd, progress FROM shared.job "
                                  "WHERE id = $1::uuid", job_id)
        if row["status"] in ("succeeded", "failed"):
            return {**dict(row), "progress": queue._json(row["progress"])}
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish: {dict(row)}")


async def _ledger(pool, job_id: str) -> float:
    return float(await pool.fetchval("SELECT coalesce(sum(cost_usd), 0)::float FROM shared.llm_call_log "
                                     "WHERE job_id = $1::uuid", job_id))


@pytest.mark.asyncio
async def test_writing_memory_jobs_count_their_llm_spend_once(pool, kinds):
    wm = kinds
    r2_job, _ = await registry.enqueue(pool, wm.R2, {})
    shadow_job, _ = await registry.enqueue(pool, wm.SHADOW, {})
    worker = Worker(pool, poll_seconds=0.1, heartbeat_seconds=0.2)
    runner = asyncio.create_task(worker.run())
    try:
        rows = {j: await _done(pool, j) for j in (r2_job, shadow_job)}
    finally:
        await worker.shutdown()
        await runner
    for job_id, row in rows.items():
        assert row["status"] == "succeeded", row["error"]
        assert await _ledger(pool, job_id) == pytest.approx(LEDGER)
        # was 2 x LEDGER (the run's ledger sum through ctx.add_cost + the same rows stamped by the runner)
        assert row["cost_usd"] == pytest.approx(LEDGER)
        assert row["progress"]["cost_stamp"]["llm_usd"] == pytest.approx(LEDGER)
        got = await queue.get(pool, job_id)
        assert got["cost_usd"] == pytest.approx(LEDGER) and got["llm_cost_usd"] == pytest.approx(LEDGER)
        job = queue._to_job(await pool.fetchrow("SELECT * FROM shared.job WHERE id = $1::uuid", job_id))
        assert await registry.JobContext(pool, job).llm_spend() == pytest.approx(LEDGER)


@pytest.mark.asyncio
async def test_non_llm_spend_still_adds_to_the_ledger_once(pool, kinds):
    job_id, _ = await registry.enqueue(pool, "aa1051_mixed", {})
    worker = Worker(pool, poll_seconds=0.1, heartbeat_seconds=0.2)
    runner = asyncio.create_task(worker.run())
    try:
        row = await _done(pool, job_id)
    finally:
        await worker.shutdown()
        await runner
    assert row["status"] == "succeeded", row["error"]
    assert row["cost_usd"] == pytest.approx(0.5 + LEDGER)
    assert (await queue.get(pool, job_id))["cost_usd"] == pytest.approx(0.5 + LEDGER)


@pytest.mark.asyncio
async def test_llm_spend_of_a_job_without_rows_is_zero(pool):
    job_id, _ = await queue.enqueue(pool, "aa1051_mixed", {})
    job = queue._to_job(await pool.fetchrow("SELECT * FROM shared.job WHERE id = $1::uuid", job_id))
    assert await registry.JobContext(pool, job).llm_spend() == 0.0
