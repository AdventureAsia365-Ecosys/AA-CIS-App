"""AA-1048 (S281 lane l8) — the two-step stop rule's ledger read (services/ground_truth/two_step.py PRIOR_RUNS_SQL) on
real Postgres (conftest replays every migration, so shared.llm_call_log is the live shape).

One transaction, rolled back. The rows are invented and sit on a test-only stage, so rows another test left in this
database do not matter. What it proves: the JSON operators and the GROUP BY run on the replayed schema; a run gives one
row per prompt version with its prereg, pool, run kind and call count; rows without a run kind (the one-prompt r1
runs on the same stage) and rows of another issue are left out; and stop_rule reads those rows the way the CLI does
(a gated run of the pre-registration refuses the next one, trials do not)."""
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

from services.ground_truth import two_step as ts  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "adhoc_aa1048_it"
PREREG = "aa1048-two-step-v5"
V5C, V5J = "aa1048-r1-v5c@50704a977e52", "aa1048-r1-v5j@0c3529d50a55"
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


async def _row(conn, signal, minutes, pv):
    await conn.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, created_at, status, "
        "prompt_version) VALUES ($1, 'judge', 'zz-model', 0.0002, $2::jsonb, $3, 'ok', $4)",
        STAGE, json.dumps(signal), T0 + timedelta(minutes=minutes), pv)


def _sig(run, kind, pv, pool="a" * 16, issue="AA-1048"):
    return {"app": "cis", "issue": issue, "run_id": run, "prereg": PREREG, "run_kind": kind, "pool": pool,
            "prompt_version": pv, "step": "classify" if pv == V5C else "judge"}


@pytest.mark.asyncio
async def test_prior_runs_sql_groups_runs_and_feeds_the_stop_rule(conn):
    for k in range(3):
        await _row(conn, _sig("11111111-trial", "trial", V5C), k, V5C)
    for k in range(2):
        await _row(conn, _sig("22222222-full", "full", V5C), 10 + k, V5C)
    await _row(conn, _sig("22222222-full", "full", V5J), 12, V5J)
    # a one-prompt r1 row on the same stage (no run kind) and a row of another issue are not two-step runs
    await _row(conn, {"app": "cis", "issue": "AA-1048", "run_id": "33333333-r1", "prompt_version": "v2"}, 20, "v2")
    await _row(conn, _sig("44444444-other", "full", V5C, issue="AA-0000"), 21, V5C)

    rows = [dict(r) for r in await conn.fetch(ts.PRIOR_RUNS_SQL, STAGE)]
    got = {(r["run_id"], r["run_kind"], r["prompt_version"]): r["calls"] for r in rows}
    assert got == {("11111111-trial", "trial", V5C): 3, ("22222222-full", "full", V5C): 2,
                   ("22222222-full", "full", V5J): 1}
    assert all(r["prereg"] == PREREG and r["pool"] == "a" * 16 for r in rows)
    assert rows[0]["run_id"] == "11111111-trial"                                       # ordered by first call

    rec = ts.stop_rule(rows, kind="full", prereg_id=PREREG, pool="b" * 16, classify_tag=V5C)
    assert rec["prior_trials"] == 1 and [g["run"] for g in rec["prior_gated_runs"]] == ["22222222"]
    assert "one gated run" in rec["refused"]
    assert ts.stop_rule(rows, kind="trial", prereg_id=PREREG, pool="a" * 16, classify_tag=V5C)["refused"] is None


@pytest.mark.asyncio
async def test_prior_runs_sql_reads_nothing_on_an_unused_stage(conn):
    assert await conn.fetch(ts.PRIOR_RUNS_SQL, STAGE + "_unused") == []
    rec = ts.stop_rule([], kind="full", prereg_id=PREREG, pool="a" * 16, classify_tag=V5C)
    assert rec["refused"] is None and rec["ledger_read"] is True
