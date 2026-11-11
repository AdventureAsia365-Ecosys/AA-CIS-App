"""AA-1076 — the gateway batch lane against a real Postgres (migrations applied by conftest):

- queue.reschedule: a running row of ours back to queued, due later, the attempt not counted, progress merged; an
  admin cancel asked meanwhile wins; the max-count reschedule fails the job; another worker's row is untouched;
- batch_lane.ledgered + record_calls: the rows of a batch (one per request, pricing_mode '+batch') are found by its
  batch id and stage, never another batch's;
- the wave report counts an `llm_batch` job (payload `tour_ids`) as each listed tour's job: `running` while the batch
  is out, its ledger rows in the wave's cost, and the batch counted once.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.waves import report as wr  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.llm_client import batch_lane  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = wr.MASTER_TENANT_ID


async def _no_fk(conn):
    await conn.execute("SET session_replication_role = replica")


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3, setup=_no_fk)
    await p.execute("DELETE FROM shared.job WHERE kind IN ('llm_batch', 'aa1076_probe')")
    yield p
    await p.execute("DELETE FROM shared.job WHERE kind IN ('llm_batch', 'aa1076_probe')")
    await p.close()


async def _running(pool, worker="w-1076", progress=None):
    job_id, _ = await queue.enqueue(pool, "aa1076_probe", {"x": 1}, max_attempts=3)
    if progress:
        await queue.set_progress(pool, job_id, progress)
    job = await queue.claim(pool, worker, {"aa1076_probe": 1}, 60)
    assert job is not None and job.id == job_id and job.attempt == 1
    return job_id


async def _row(pool, job_id):
    row = await pool.fetchrow("SELECT status, attempt, run_after, locked_by, error, progress, finished_at "
                              "FROM shared.job WHERE id = $1::uuid", job_id)
    out = dict(row)
    out["progress"] = json.loads(out["progress"]) if isinstance(out["progress"], str) else dict(out["progress"])
    return out


@pytest.mark.asyncio
async def test_reschedule_requeues_the_same_row_without_counting_the_attempt(pool):
    job_id = await _running(pool, progress={"phase": "submitting", "keep": "me"})
    status = await queue.reschedule(pool, job_id, "w-1076", 120, {"phase": "submitted", "provider_batch_id": "p1"},
                                    0.0, max_count=600)
    assert status == "queued"
    row = await _row(pool, job_id)
    assert row["status"] == "queued" and row["attempt"] == 0 and row["locked_by"] is None
    assert row["run_after"] > datetime.now(timezone.utc) + timedelta(seconds=100)
    assert row["progress"]["phase"] == "submitted" and row["progress"]["provider_batch_id"] == "p1"
    assert row["progress"]["keep"] == "me" and row["progress"]["reschedules"] == 1
    assert row["finished_at"] is None
    # not due yet: no worker claims it before run_after
    assert await queue.claim(pool, "w-other", {"aa1076_probe": 1}, 60) is None


@pytest.mark.asyncio
async def test_reschedule_is_fenced_on_the_lease_and_ends_on_cancel_or_count(pool):
    job_id = await _running(pool)
    assert await queue.reschedule(pool, job_id, "w-someone-else", 60, {}, None, max_count=600) == ""
    await pool.execute("UPDATE shared.job SET cancel_requested = true WHERE id = $1::uuid", job_id)
    assert await queue.reschedule(pool, job_id, "w-1076", 60, {}, None, max_count=600) == "cancelled"
    row = await _row(pool, job_id)
    assert row["status"] == "cancelled" and row["finished_at"] is not None

    job2 = await _running(pool, progress={"reschedules": 3})
    assert await queue.reschedule(pool, job2, "w-1076", 60, {}, None, max_count=3) == "failed"
    row = await _row(pool, job2)
    assert row["status"] == "failed" and "rescheduled 3 times" in row["error"]


@pytest.fixture
def ledger(monkeypatch):
    from shared.llm_client import call_log, pricing
    real_connect = asyncpg.connect

    async def connect(*a, **k):                 # CI Postgres has no SSL; the code asks for it
        return await real_connect(DSN, **{key: v for key, v in k.items() if key == "timeout"})
    monkeypatch.setattr(call_log.asyncpg, "connect", connect)
    monkeypatch.setattr(call_log, "get_database_url", lambda: DSN)
    monkeypatch.setattr(call_log, "_v2_missing_at", 0.0)
    monkeypatch.setattr(pricing, "_catalog_rates", lambda m: {"in": 0.002, "out": 0.010, "cache_read": 0.0002,
                                                              "cache_write": 0.0025})
    monkeypatch.setattr(pricing, "deployment_for", lambda *a, **k: None)
    return call_log


@pytest.mark.asyncio
async def test_a_batch_ledger_is_found_by_its_batch_id(pool, ledger):
    call_log = ledger
    tour = str(uuid.uuid4())
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    target = batch_lane.BatchTarget(stage="s1_generate", role="writer", model_key="sonnet-5-5",
                                    connection_key="anthropic-direct", provider="anthropic",
                                    wire_id="claude-sonnet-5-5")
    results = {"a": batch_lane.ItemResult(custom_id="a", outcome="succeeded", text="{}", stop_reason="end_turn",
                                          input_tokens=1000, output_tokens=200),
               "b": batch_lane.ItemResult(custom_id="b", outcome="expired")}
    batch_id = str(uuid.uuid4())
    rows = batch_lane.ledger_rows(target, results, [{"custom_id": "a", "tour_id": tour}, {"custom_id": "b",
                                                                                          "tour_id": tour}],
                                  batch_id=batch_id, provider_batch_id="msgbatch_x", consumer="s1_attempt1")
    await call_log.record_calls(rows)
    async with pool.acquire() as conn:
        seen = await batch_lane.ledgered(conn, "s1_generate", since, batch_id)
        assert seen["rows"] == 2 and seen["billed"] == 1 and seen["usd"] > 0
        assert (await batch_lane.ledgered(conn, "s1_generate", since, str(uuid.uuid4())))["rows"] == 0
        modes = await conn.fetch("SELECT pricing_mode, status, unit_kind, tour_id::text AS tour FROM "
                                 "shared.llm_call_log WHERE quality_signal->>'batch_id' = $1 ORDER BY status",
                                 batch_id)
    await pool.execute("DELETE FROM shared.llm_call_log WHERE quality_signal->>'batch_id' = $1", batch_id)
    assert [m["status"] for m in modes] == ["error", "ok"]
    assert modes[1]["pricing_mode"].endswith("+batch") and modes[1]["unit_kind"] == "batch_request"
    assert {m["tour"] for m in modes} == {tour}


async def _raw(conn, name):
    tid = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO silver_aa_internal.raw_tours
               (tour_id, tenant_id, batch_id, src_name, country, src_itineraries, source_status)
           VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'Laos', $5, 'active')""",
        tid, MASTER, str(uuid.uuid4()), name, "Day 1: " + "x" * 800)
    return tid


@pytest.mark.asyncio
async def test_the_wave_report_counts_a_batch_as_each_tours_job(pool):
    async with pool.acquire() as conn:
        t1, t2, t3 = await _raw(conn, "Batch A"), await _raw(conn, "Batch B"), await _raw(conn, "Batch C")
        wave_id = await conn.fetchval(
            "INSERT INTO shared.rerun_wave (name, country, expected_tour_ids) VALUES ('Laos', 'Laos', $1::uuid[]) "
            "RETURNING id::text", [t1, t2, t3])
        job = await conn.fetchval(
            "INSERT INTO shared.job (kind, payload, status) VALUES ('llm_batch', $1::jsonb, 'queued') "
            "RETURNING id::text",
            json.dumps({"batch_id": "B", "consumer": "s1_attempt1", "cap_usd": 0.2, "tour_ids": [t1, t2]}))
        await conn.execute(
            """INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, job_id, provider)
               VALUES ('s1_generate', 'writer', 'sonnet-5-5', 0.01, '{"batch": true}', $1::uuid, 'anthropic')""",
            job)
        rep = await wr.report(conn, wave_id)
    states = {r["name"]: r["state"] for r in rep["tours"]}
    assert states["Batch A"] == "running" and states["Batch B"] == "running"
    assert states["Batch C"] == "not_started"
    assert rep["cost"]["llm_usd"] == 0.01                      # the batch row counted once, not once per tour
    totals = {(j["kind"], j["status"]): j["n"] for j in rep["jobs"]}
    assert totals == {("llm_batch", "queued"): 1}               # one batch job, not one per listed tour
    assert rep["jobs_finished"] is False and rep["complete"] is False
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM shared.llm_call_log WHERE job_id = $1::uuid", job)
        await conn.execute("DELETE FROM shared.rerun_wave WHERE id = $1::uuid", wave_id)


@pytest.mark.asyncio
async def test_the_measured_basis_reads_on_demand_calls_only(pool, ledger):
    stage = "adhoc_aa1076_basis"
    target = batch_lane.BatchTarget(stage=stage, role="writer", model_key="sonnet-5-5",
                                    connection_key="anthropic-direct", provider="anthropic", wire_id="w")
    async with pool.acquire() as conn:
        for usd in [0.01] * 9 + [0.10]:
            await conn.execute(
                "INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, status) "
                "VALUES ($1, 'writer', 'sonnet-5-5', $2, '{}', 'ok')", stage, usd)
        # a batch row and a failed row never count
        await conn.execute("INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, status) "
                           "VALUES ($1, 'writer', 'sonnet-5-5', 5.0, '{\"batch\": true}', 'ok')", stage)
        await conn.execute("INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, status) "
                           "VALUES ($1, 'writer', 'sonnet-5-5', 5.0, '{}', 'billed_unusable')", stage)
        basis = await batch_lane.history_basis(conn, target)
        await conn.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", stage)
    assert basis["n"] == 10 and 0.01 < basis["p95"] <= 0.10
