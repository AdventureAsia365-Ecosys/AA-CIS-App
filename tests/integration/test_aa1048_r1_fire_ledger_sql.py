"""AA-1048 (S281 lane l15) — the fire run's ledger reads (services/ground_truth/r1_fire.py LEDGER_CALLS_SQL, which a
resume reconciles its checkpoint with, and LEDGER_ROWS_SQL) on real Postgres (conftest replays every migration, so
shared.llm_call_log is the live shape).

One transaction, rolled back. The rows are invented and sit on a test-only stage. What it proves: the JSON operators
run on the replayed schema; the reads return the call id of every row of one run (an unusable reply's row too) and
only that run's rows on that stage; and reconcile accepts the checkpoint whose billed replies are those rows and
refuses one that lost a row."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.ground_truth import r1_fire as rf  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "adhoc_aa1048_fire_it"
RUN, OTHER = "aaaaaaaa-fire-run", "bbbbbbbb-fire-run"
T0 = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)


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


async def _row(conn, run, n, status="ok", stage=STAGE):
    signal = {"app": "cis", "issue": "AA-1048", "run_id": run, "call_id": f"{run}:{n}", "purpose": rf.PURPOSE,
              "fire_kind": "full"}
    await conn.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, created_at, status) "
        "VALUES ($1, 'judge', 'zz-model', 0.0004, $2::jsonb, $3, $4)",
        stage, json.dumps(signal), T0 + timedelta(minutes=n), status)


@pytest.mark.asyncio
async def test_the_ledger_reads_give_one_runs_call_ids_and_feed_reconcile(conn):
    for n in (1, 2, 4):
        await _row(conn, RUN, n)
    await _row(conn, RUN, 5, status="billed_unusable")
    await _row(conn, OTHER, 1)
    await _row(conn, RUN, 9, stage="adhoc_aa1048_other_it")

    ids = sorted(r["call_id"] for r in await conn.fetch(rf.LEDGER_CALLS_SQL, STAGE, RUN))
    assert ids == [f"{RUN}:{n}" for n in (1, 2, 4, 5)]
    assert await conn.fetchval(rf.LEDGER_ROWS_SQL, STAGE, RUN) == 4

    ckpt = [{"call_id": f"{RUN}:1", "outcome": "vote"}, {"call_id": f"{RUN}:2", "outcome": "vote"},
            {"call_id": f"{RUN}:3", "outcome": "caller_error"}, {"call_id": f"{RUN}:4", "outcome": "identity_mismatch"},
            {"call_id": f"{RUN}:5", "outcome": "unusable"}]
    assert rf.reconcile(ckpt, ids) is None
    assert "not in the checkpoint" in rf.reconcile(ckpt[:-1], ids)
    assert await conn.fetch(rf.LEDGER_CALLS_SQL, STAGE, "cccccccc-none") == []
