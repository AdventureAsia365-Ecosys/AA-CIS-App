"""AA-900 — scripts/llm_ops/eval_run_report.py's SQL on real Postgres (one transaction, rolled back).

The unit tests use a fake connection, so they cannot see a typing error: `created_at >= $3 - interval '10 minutes'`
made Postgres type $3 as an interval and every real run failed (UndefinedFunctionError). Here the report runs against
real shared.prompt_eval_runs / shared.llm_call_log rows:

  * report() sums only the run's own logged spend (exact eval_run filter, both eval labels, other runs ignored);
  * --latest picks the newest FINISHED run of the asked label: a newer run still in progress and a newer run of the
    other label are skipped.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from scripts.llm_ops import eval_run_report as rep  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
NOW = datetime.now(timezone.utc)
WRITER = "t9_blog"


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


async def _set(conn):
    return await conn.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, source_uri, source_sha256, seed, item_count, imported_by) "
        "VALUES ($1, '0.0.1', 'file:zz.json', $2, 1, 0, 'pytest') RETURNING set_id",
        f"zz900_{uuid.uuid4().hex[:8]}", "a" * 64)


async def _run(conn, set_id, *, stage, created_at, finished):
    return await conn.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by, "
        "run_kind, writer, golden_set_id, created_at, finished_at, stop_reason, verdict) "
        "VALUES ('writing_memory', 'zz900', 0, 0, $1::jsonb, 'pytest', 'r2', $2, $3, $4::timestamptz, "
        "CASE WHEN $5::boolean THEN $4::timestamptz + interval '5 minutes' END, "
        "CASE WHEN $5::boolean THEN 'completed' END, CASE WHEN $5::boolean THEN 'pass' END) RETURNING id::text",
        f'{{"log_stage": "{stage}"}}', WRITER, set_id, created_at, finished)


async def _call(conn, run_id, *, stage, role, cost, created_at):
    await conn.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, created_at) "
        "VALUES ($1, $2, 'zz900-model', $3, $4::jsonb, $5)",
        stage, role, cost, f'{{"eval_run": "{run_id}", "real_stage": "t9_write"}}', created_at)


@pytest.mark.asyncio
async def test_report_runs_on_real_postgres_and_sums_only_the_run(conn):
    set_id = await _set(conn)
    start = NOW - timedelta(hours=1)
    run = await _run(conn, set_id, stage="adhoc_aa900", created_at=start, finished=True)
    other = await _run(conn, set_id, stage="adhoc_aa900", created_at=start, finished=True)
    await _call(conn, run, stage="adhoc_aa900", role="writer", cost=0.10, created_at=start + timedelta(minutes=1))
    await _call(conn, run, stage="adhoc_aa900", role="judge", cost=0.02, created_at=start + timedelta(minutes=2))
    # a row written in the margin before the run's created_at (transaction start) still counts
    await _call(conn, run, stage="adhoc_aa900", role="judge", cost=0.01, created_at=start - timedelta(minutes=2))
    await _call(conn, other, stage="adhoc_aa900", role="writer", cost=5.0, created_at=start + timedelta(minutes=1))

    out = await rep.report(conn, run)

    assert out["run"]["run_id"] == run and out["run"]["log_stage"] == "adhoc_aa900"
    assert out["logged_calls"] == 3
    assert out["logged_cost_usd"] == pytest.approx(0.13)


@pytest.mark.asyncio
async def test_latest_skips_unfinished_runs_and_other_labels(conn):
    # future timestamps: newer than any run another test left in the shared DB
    later = NOW + timedelta(days=30)
    set_id = await _set(conn)
    wanted = await _run(conn, set_id, stage="adhoc_aa900", created_at=later, finished=True)
    await _run(conn, set_id, stage="adhoc_aa900", created_at=later + timedelta(hours=2), finished=False)
    release = await _run(conn, set_id, stage="writing_memory_eval", created_at=later + timedelta(hours=1),
                         finished=True)

    assert await conn.fetchval(rep.LATEST_SQL, WRITER, "adhoc_aa900") == wanted
    assert await conn.fetchval(rep.LATEST_SQL, WRITER, "writing_memory_eval") == release
