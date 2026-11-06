"""S271 (AA-741 / AA-900, lane l9-token-budget-core) — on real Postgres: the gateway's cut-reply row and the retry's
row land with their status and quality_signal keys, and route_watch's cut-rate query counts them per stage (probe
stages and shadow rows left out; a refused cut counted although nothing was billed; an eval row counted under its
real stage; other unbilled rows not counted).

Invented stage names only (zz271_*); every row this test reads sits in 2099 or is removed by the test.
"""
import asyncio
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

from shared.llm_client import call_log  # noqa: E402
from shared.llm_client import route_watch as rw  # noqa: E402
from shared.llm_client.models import LLMResponse  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "zz271_writer"
CALM = "zz271_judge"
PROBE = "adhoc_conn_test"
NOW = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
EVAL = "zz271_eval"                     # an eval run's label stage (call_log._relabel)
STAGES = [STAGE, CALM, EVAL]


@pytest_asyncio.fixture
async def pool(monkeypatch):
    real_connect = asyncpg.connect

    async def connect(*a, **k):                 # CI Postgres has no SSL; the code asks for it
        k.pop("ssl", None)
        return await real_connect(DSN, **{key: v for key, v in k.items() if key == "timeout"})
    monkeypatch.setattr(call_log.asyncpg, "connect", connect)
    monkeypatch.setattr(call_log, "get_database_url", lambda: DSN)
    monkeypatch.setattr(call_log, "_v2_missing_at", 0.0)
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    await _clean(p)
    yield p
    await _clean(p)
    await p.close()


async def _clean(p):
    await p.execute("DELETE FROM shared.llm_call_log WHERE stage = ANY($1::text[]) "
                    "OR (stage = $2 AND created_at >= '2099-01-01')", STAGES, PROBE)


async def _row(p, stage, *, status="ok", stop="end_turn", tokens_out=20, cost=0.01, shadow=False,
               at=NOW - timedelta(hours=1), signal=None, experiment=False):
    await p.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal, "
        "created_at, status, stop_reason, is_shadow, is_experiment, app) "
        "VALUES ($1, 'writer', 'zz-model', 10, $2, $3, $8::jsonb, $4, $5, $6, $7, $9, 'cis')",
        stage, tokens_out, cost, at, status, stop, shadow, json.dumps(signal or {}), experiment)


def _resp(**kw):
    base = dict(content="x", model_used="satellite-gpt-6-luna", provider="bedrock-satellite", input_tokens=10,
                output_tokens=4096, cost_usd=0.02, stop_reason="max_tokens", satellite_account="acc3",
                attempt_no=1, route_trace="t-271", route_position=0)
    base.update(kw)
    return LLMResponse(**base)


@pytest.mark.asyncio
async def test_the_cut_row_and_the_retry_row_land(pool):
    cut = call_log.cut_reply_row(_resp(), stage=STAGE, role="judge", max_tokens=4096, retry_max_tokens=8192,
                                 quality_signal_extra={"position": 0, "passed": True})
    await call_log.record_calls([cut])
    retry = _resp(content="whole", output_tokens=900, stop_reason="end_turn", truncation_retry_max_tokens=8192)
    await call_log.record_call(stage=STAGE, role="judge", model=retry.model_used, tokens_in=10, tokens_out=900,
                               cost_usd=0.01, quality_signal={"json_parsed": True}, stop_reason=retry.stop_reason,
                               **call_log.billed_fields(retry))
    rows = await pool.fetch("SELECT status, stop_reason, attempt_no, cost_usd::float AS cost, quality_signal "
                            "FROM shared.llm_call_log WHERE stage = $1 ORDER BY status DESC", STAGE)
    assert [(r["status"], r["stop_reason"], r["attempt_no"]) for r in rows] == \
        [("truncated", "max_tokens", 1), ("ok", "end_turn", 1)]
    q_cut, q_retry = (json.loads(r["quality_signal"]) if isinstance(r["quality_signal"], str)
                      else r["quality_signal"] for r in rows)
    assert q_cut["budget_cut"] is True and q_cut["gateway_attempt"] is True and "passed" not in q_cut
    assert (q_cut["max_tokens"], q_cut["retry_max_tokens"], q_cut["route_trace"]) == (4096, 8192, "t-271")
    assert q_retry["truncation_retry"] is True and q_retry["max_tokens"] == 8192 and q_retry["route_trace"] == "t-271"
    assert rows[0]["cost"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_the_retry_reason_and_the_degraded_marks_land_in_quality_signal(pool):
    """S271 W0 (AA-900 l13): the retry's row says why it ran; a degraded judge answer carries its marks; both through
    billed_fields, on the async row path and the record_calls row path."""
    marks = [{"name": "fallback_instrument", "reason": "rung", "stage": CALM, "model": "gpt-6-luna-openai",
              "position": 3, "ignored": "x"}]
    retry = _resp(content="whole", output_tokens=900, stop_reason="end_turn", truncation_retry_max_tokens=8192,
                  truncation_retry_reason="billed_unusable_cut", truncation_cut_cost_usd=0.13, route_degraded=marks)
    await call_log.record_call(stage=CALM, role="judge", model=retry.model_used, tokens_in=10, tokens_out=900,
                               cost_usd=0.01, quality_signal={"passed": True}, stop_reason=retry.stop_reason,
                               **call_log.billed_fields(retry))
    await call_log.record_calls([dict(stage=CALM, role="judge", model="zz-model", tokens_in=1, tokens_out=1,
                                      cost_usd=0.0, quality_signal={"passed": False},
                                      **call_log.billed_fields(_resp(route_degraded=marks)))])
    rows = await pool.fetch("SELECT model, quality_signal FROM shared.llm_call_log WHERE stage = $1", CALM)
    # id is a uuid: pick each row by its model, never by row order (S271: ORDER BY id made this test flaky in CI).
    by_model = {r["model"]: (json.loads(r["quality_signal"]) if isinstance(r["quality_signal"], str)
                             else r["quality_signal"]) for r in rows}
    stored = call_log._normalize_model_provider(retry.model_used, None)[0]    # the ledger strips "satellite-"
    q_retry, q_row = by_model[stored], by_model[call_log._normalize_model_provider("zz-model", None)[0]]
    assert q_retry["truncation_retry"] is True and q_retry["retry_reason"] == "billed_unusable_cut"
    assert q_retry["passed"] is True and "truncation_cut_cost_usd" not in q_retry
    want = [{"name": "fallback_instrument", "reason": "rung", "stage": CALM, "model": "gpt-6-luna-openai",
             "position": 3}]
    assert q_retry["degraded"] == want and q_row["degraded"] == want


@pytest.mark.asyncio
async def test_the_cut_rate_query_counts_cut_replies_per_stage(pool):
    for _ in range(8):
        await _row(pool, STAGE)
    await _row(pool, STAGE, status="truncated", stop="max_tokens")
    await _row(pool, STAGE, status="billed_unusable", stop="length")
    await _row(pool, STAGE, status="billed_unusable", stop="end_turn")           # unusable, not a cut
    await _row(pool, STAGE, status="truncated", stop=None, tokens_out=0, cost=0.0)   # refused cut: counted
    await _row(pool, STAGE, status="error", stop=None, tokens_out=0, cost=0.0)       # unbilled failure: left out
    await _row(pool, EVAL, status="truncated", stop=None, tokens_out=0, cost=0.0, experiment=True,
               signal={"real_stage": STAGE, "eval_run": "r-271"})                 # an eval refused cut: real stage
    await _row(pool, EVAL, experiment=True, signal={"real_stage": STAGE, "eval_run": "r-271"})
    await _row(pool, STAGE, status="truncated", stop="max_tokens", shadow=True)  # shadow: left out
    await _row(pool, STAGE, status="truncated", stop="max_tokens", at=NOW - timedelta(days=2))   # outside 24 h
    await _row(pool, CALM)
    await _row(pool, PROBE, status="truncated", stop="max_tokens")               # a probe: left out
    stats = await rw.read_cut_stats(pool, NOW - rw.COST_WINDOW)
    st = stats[STAGE]
    assert (st["replies"], st["cut"], st["refused_cut"], st["eval_rows"]) == (14, 4, 2, 2)
    assert st["cut_cost_usd"] == pytest.approx(0.02)                            # the billed part only
    assert (stats[CALM]["replies"], stats[CALM]["cut"]) == (1, 0) and PROBE not in stats and EVAL not in stats
    (f,) = rw.cut_findings({STAGE: st, CALM: stats[CALM]}, {})
    assert f.check == "route_cut." + STAGE and f.details["cut_pct"] == pytest.approx(28.6)


def test_the_module_runs_its_coroutines():
    assert asyncio.iscoroutinefunction(rw.read_cut_stats)
