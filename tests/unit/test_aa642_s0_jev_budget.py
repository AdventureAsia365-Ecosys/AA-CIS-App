"""AA-642 (S0) — the Jev daily cap before every ask (shared.spend_budget jev/global per_day_usd, migration 318).

Over a hard cap: zone 'error' / error 'budget_exceeded', no TypeSafe call, the decision_log rows still written, the
stage keeps its own rule. The budget is read at most once per TTL per process and charged with this process's own
asks in between. An unreadable budget fails open. Mock-only: no TypeSafe call, no DB.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared import cost_guard
from shared.cost_guard import RunBudget
from shared.llm_client import decide as d


def _q(key="kw_belongs", mode="enforce"):
    return d.Question(key=key, stage="a3_research", kind="noul", instructions="Is it about this valley?",
                      criteria=None, mode=mode, accept_floor=0.8, reject_ceiling=0.2, threshold_version=1)


def _conn(questions):
    conn = MagicMock()

    async def fetch(sql, *args):
        if "decision_question" in sql:
            return [{"question_key": q.key, "stage": q.stage, "kind": q.kind, "instructions": q.instructions,
                     "criteria": q.criteria, "mode": q.mode, "accept_floor": q.accept_floor,
                     "reject_ceiling": q.reject_ceiling, "threshold_version": q.threshold_version,
                     "question_hash": "h-" + q.key}
                    for q in questions]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.executemany = AsyncMock()
    return conn


def _budget(per_day=5.0, spent=0.0, hard=True):
    return RunBudget(provider="jev", scope="job:decide", per_run_usd=None, per_day_usd=per_day,
                     hard_stop=hard, hard_stop_day=hard, day_spent_at_start=spent)


_ANSWER = {"model": "jev-1.13.0", "usage": {"input_tokens": 1000},
           "answers": {"kw_belongs": {"type": "noul", "noul": 0.9}}}


@pytest.fixture(autouse=True)
def _fresh():
    d._questions_loaded_at = 0.0
    d._allowlist_loaded_at = 0.0
    d.reset_decision_memo()
    yield
    d.reset_decision_memo()


async def _ask(conn, *, budget_mock, answer=_ANSWER, alert=None):
    with patch.object(cost_guard, "load_run_budget", new=budget_mock), \
         patch.object(cost_guard, "maybe_alert", new=alert or AsyncMock(return_value=False)), \
         patch.object(d, "_call_jev", new=AsyncMock(return_value=answer)) as m_call, \
         patch.object(d, "_price_in_per_mtok", return_value=0.042), \
         patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:US:quiet valley", "quiet valley",
                              ["kw_belongs"], None)
    return dec, m_call, m_log


@pytest.mark.asyncio
async def test_over_the_daily_cap_no_call_error_zone_and_the_row_is_still_logged():
    conn = _conn([_q()])
    alert = AsyncMock(return_value=True)
    dec, m_call, m_log = await _ask(conn, budget_mock=AsyncMock(return_value=_budget(spent=5.0)), alert=alert)
    m_call.assert_not_awaited()                                         # no TypeSafe call
    v = dec.verdicts["kw_belongs"]
    assert (v.zone, v.error) == ("error", d.JEV_BUDGET_EXCEEDED)
    assert not dec.accepted("kw_belongs") and not dec.rejected("kw_belongs")   # the stage keeps its own rule
    rows = conn.executemany.await_args.args[1]                          # decision_log row still written
    assert rows[0][1] == "kw_belongs" and rows[0][6] == "error" and rows[0][14] == d.JEV_BUDGET_EXCEEDED
    assert rows[0][13] == 0.0                                           # nothing billed
    # AA-804 (ledger v2): no call, but the budget refusal has its 0 USD llm_call_log row
    m_log.assert_awaited_once()
    row = m_log.await_args.kwargs
    assert (row["cost_usd"], row["status"], row["error_class"]) == (0.0, "refused", "budget")
    # one stop alert with a reason (cost_guard throttles it to one per provider per 24 h)
    assert alert.await_args.kwargs["reason"].startswith("Jev daily budget $5.00 reached")


@pytest.mark.asyncio
async def test_under_the_cap_asks_jev_as_before():
    conn = _conn([_q()])
    dec, m_call, m_log = await _ask(conn, budget_mock=AsyncMock(return_value=_budget(spent=1.0)))
    m_call.assert_awaited_once()
    assert dec.accepted("kw_belongs")
    m_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_alert_only_cap_never_blocks():
    conn = _conn([_q()])
    alert = AsyncMock(return_value=True)
    dec, m_call, _ = await _ask(conn, budget_mock=AsyncMock(return_value=_budget(spent=9.0, hard=False)), alert=alert)
    m_call.assert_awaited_once()
    assert dec.accepted("kw_belongs")
    assert alert.await_args.kwargs["reason"] is None                    # the 80 % alert path, not a stop


@pytest.mark.asyncio
async def test_no_cap_row_never_blocks():
    conn = _conn([_q()])
    _, m_call, _ = await _ask(conn, budget_mock=AsyncMock(return_value=_budget(per_day=None, spent=100.0)))
    m_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unreadable_budget_fails_open():
    conn = _conn([_q()])
    _, m_call, _ = await _ask(conn, budget_mock=AsyncMock(side_effect=RuntimeError("relation does not exist")))
    m_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_budget_is_read_once_per_ttl_and_charged_with_own_asks_in_between():
    loads = AsyncMock(return_value=_budget(per_day=5.0, spent=4.99))
    conn = _conn([_q()])
    answer = {**_ANSWER, "usage": {"input_tokens": 1_000_000}}          # 0.042 USD at 0.042 / MTok
    _, m_call, _ = await _ask(conn, budget_mock=loads, answer=answer)
    m_call.assert_awaited_once()                                        # 4.99 < 5.00: asked
    assert loads.await_count == 1
    d._memo.clear()                                                     # not a verdict-cache hit this time
    # same process, inside the TTL: no new read, but its own ask pushed today's spend over the cap
    dec, m_call2, _ = await _ask(_conn([_q()]), budget_mock=loads, answer=answer)
    assert loads.await_count == 1
    m_call2.assert_not_awaited()
    assert dec.verdicts["kw_belongs"].error == d.JEV_BUDGET_EXCEEDED


@pytest.mark.asyncio
async def test_an_unreadable_refresh_keeps_the_last_budget():
    conn = _conn([_q()])
    await _ask(conn, budget_mock=AsyncMock(return_value=_budget(spent=6.0)))
    d._jev_budget_read_at = float("-inf")                               # TTL elapsed
    dec, m_call, _ = await _ask(_conn([_q()]), budget_mock=AsyncMock(side_effect=RuntimeError("db blip")))
    m_call.assert_not_awaited()                                         # still over the last known cap
    assert dec.verdicts["kw_belongs"].error == d.JEV_BUDGET_EXCEEDED


@pytest.mark.asyncio
async def test_cache_hits_and_skips_never_read_the_budget():
    loads = AsyncMock(return_value=_budget(spent=6.0))
    conn = _conn([_q(mode="off")])
    _, m_call, _ = await _ask(conn, budget_mock=loads)
    m_call.assert_not_awaited()
    loads.assert_not_awaited()                                          # nothing to ask: no budget read


@pytest.mark.asyncio
async def test_the_budget_reads_the_jev_global_row():
    loads = AsyncMock(return_value=_budget(spent=0.0))
    await _ask(_conn([_q()]), budget_mock=loads)
    args = loads.await_args.args
    assert args[1:] == ("jev", "decide")                                # provider jev; global + job:decide rows


@pytest.mark.asyncio
async def test_cost_guard_day_spend_reads_typesafe_rows_like_the_overview():
    from api.routers import admin_overview

    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=2.2)
    assert await cost_guard.day_spend(conn, "jev") == pytest.approx(2.2)
    sql = conn.fetchval.await_args.args[0]
    assert "shared.llm_call_log" in sql and "lower(provider) IN ('typesafe', 'jev')" in sql
    # the same filter the admin Overview's Jev figure uses, so the two numbers agree
    assert "lower(provider) IN ('typesafe', 'jev')" in admin_overview._JEV_DAY_SPEND_SQL


# ── the daily canary writes its ledger row ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_successful_jev_canary_writes_one_ledger_row(monkeypatch):
    from api.routers import admin_llm_ops
    from shared.llm_client import call_log

    monkeypatch.setattr(admin_llm_ops, "verify_admin_secret", lambda _s: None)
    pool = MagicMock()
    request = MagicMock()
    request.app.state.pool = pool
    reply = {"model": "jev-1.13.0", "usage": {"input_tokens": 120}, "answers": {}}
    with patch.object(d, "_call_jev", AsyncMock(return_value=reply)), patch.object(d, "reset_jev_breaker"), \
         patch.object(d, "jev_breaker_open", return_value=False), \
         patch.object(d, "_price_in_per_mtok", return_value=0.042), \
         patch.object(call_log, "record_call_with_pool", new=AsyncMock()) as m_log:
        out = await admin_llm_ops.check_jev_canary(request, x_admin_secret="x")
    assert out["credit_ok"] is True
    m_log.assert_awaited_once()
    assert m_log.await_args.args[0] is pool
    row = m_log.await_args.kwargs
    assert row["stage"] == "adhoc_jev_canary" and row["role"] == "validate" and row["provider"] == "typesafe"
    assert row["model"] == "jev-1.13.0" and row["tokens_in"] == 120 and row["tokens_out"] == 0
    assert row["cost_usd"] == pytest.approx(120 * 0.042 / 1_000_000)


@pytest.mark.asyncio
async def test_a_402_canary_writes_no_ledger_row(monkeypatch):
    from api.routers import admin_llm_ops
    from shared.llm_client import call_log

    monkeypatch.setattr(admin_llm_ops, "verify_admin_secret", lambda _s: None)
    request = MagicMock()
    request.app.state.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    request.app.state.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch.object(d, "_call_jev", AsyncMock(side_effect=d.JevBillingError("402"))), \
         patch.object(d, "_trip_jev_breaker"), patch.object(d, "jev_breaker_open", return_value=True), \
         patch.object(d, "maybe_alert_jev_credit", AsyncMock(return_value=False)), \
         patch.object(call_log, "record_call_with_pool", new=AsyncMock()) as m_log:
        out = await admin_llm_ops.check_jev_canary(request, x_admin_secret="x")
    assert out["billing_error"] is True
    m_log.assert_not_awaited()                                          # a 402 is not billed
