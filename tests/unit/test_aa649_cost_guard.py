"""AA-649 — cost guard: budgets are checked BEFORE every paid call and stop a run cleanly.
Mock-only (no DB, no network)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared import cost_guard as cg
from shared.llm_client.models import LLMResponse


def _budget(**kw):
    base = dict(provider="dfs", scope="job:segment_research", per_run_usd=1.0, per_day_usd=10.0)
    base.update(kw)
    return cg.RunBudget(**base)


# ── RunBudget ──────────────────────────────────────────────────────────────────────────────────

def test_check_allows_within_run_budget():
    b = _budget()
    b.charge(0.9)
    b.check(0.05)  # 0.95 <= 1.0


def test_check_blocks_per_run_breach():
    b = _budget()
    b.charge(0.95)
    with pytest.raises(cg.BudgetExceeded) as info:
        b.check(0.075)
    assert info.value.limit == "per_run" and info.value.provider == "dfs"


def test_check_blocks_per_day_breach_including_spend_before_run():
    b = _budget(per_run_usd=None, per_day_usd=10.0, day_spent_at_start=9.97)
    with pytest.raises(cg.BudgetExceeded) as info:
        b.check(0.075)
    assert info.value.limit == "per_day"


def test_soft_budget_never_raises():
    b = _budget(hard_stop=False, per_run_usd=0.0)
    b.check(5.0)


def test_remaining_is_smallest_axis():
    b = _budget(per_run_usd=5.0, per_day_usd=10.0, day_spent_at_start=8.0)
    assert b.remaining_usd() == pytest.approx(2.0)
    assert _budget(per_run_usd=None, per_day_usd=None).remaining_usd() is None


# ── load_run_budget ────────────────────────────────────────────────────────────────────────────

def _pool(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


@pytest.mark.asyncio
async def test_load_merges_job_per_run_and_smallest_daily_cap():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"scope": "global", "per_run_usd": None, "per_day_usd": 10.0, "hard_stop": True, "alert_pct": 80},
        {"scope": "job:segment_research", "per_run_usd": 5.0, "per_day_usd": 8.0, "hard_stop": True,
         "alert_pct": 90},
    ])
    conn.fetchval = AsyncMock(return_value=3.5)
    b = await cg.load_run_budget(_pool(conn), "dfs", "segment_research")
    assert (b.per_run_usd, b.per_day_usd, b.day_spent_at_start, b.alert_pct) == (5.0, 8.0, 3.5, 90)


@pytest.mark.asyncio
async def test_load_falls_back_to_safe_defaults_when_table_missing():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=Exception('relation "shared.spend_budget" does not exist'))
    conn.fetchval = AsyncMock(return_value=0)
    b = await cg.load_run_budget(_pool(conn), "dfs", "segment_research")
    assert b.per_run_usd == 5.0 and b.per_day_usd == 10.0 and b.hard_stop


# ── DataForSEO client hook ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dfs_client_prechecks_before_any_http_call():
    from services.seo_intelligence import dataforseo_client as dfs

    client = dfs.DataForSEOClient(login="x", password="y", budget=_budget(per_run_usd=0.01))
    fake = MagicMock()
    with patch.object(dfs.httpx, "AsyncClient", fake):
        with pytest.raises(cg.BudgetExceeded):
            await client.fetch_volumes_bulk(["kyoto"], raise_on_error=True)
        with pytest.raises(cg.BudgetExceeded):
            await client.fetch_keyword_ideas("kyoto", raise_on_error=True)
    fake.assert_not_called()  # nothing was sent


@pytest.mark.asyncio
async def test_dfs_client_charges_real_response_cost():
    from services.seo_intelligence import dataforseo_client as dfs

    budget = _budget()
    client = dfs.DataForSEOClient(login="x", password="y", budget=budget)
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"status_code": 20000, "cost": 0.0575,
                                        "tasks": [{"status_code": 20000, "result": []}]})
    http = MagicMock()
    http.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=http)
    ctx.__aexit__ = AsyncMock(return_value=False)
    with patch.object(dfs.httpx, "AsyncClient", MagicMock(return_value=ctx)), \
         patch.object(dfs, "record_dfs_call_sync"):
        await client.fetch_volumes_bulk(["kyoto"], raise_on_error=True)
    assert budget.run_spent == pytest.approx(0.0575)


# ── research loop ──────────────────────────────────────────────────────────────────────────────

def test_budget_stop_aborts_the_run():
    from services.acp_contract import segment_research as sr

    guard = sr._RunGuard()
    guard.record(cg.BudgetExceeded("dfs per-run budget $5.00 reached", "dfs", "per_run"))
    assert guard.aborted and "per-run budget" in guard.reason


@pytest.mark.asyncio
async def test_llm_budget_stops_before_the_llm_call():
    from services.acp_contract import segment_research as sr

    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = _pool(conn)
    fake_llm = MagicMock()
    guard = sr._RunGuard()
    llm_budget = _budget(provider="bedrock", per_run_usd=0.001)
    with patch.object(sr, "LLMClient", return_value=fake_llm):
        result = await sr._research_place(
            "kyoto", ["explore"], ["US"], [(2840, "United States", "en")], {}, MagicMock(),
            pool, asyncio.Semaphore(1), guard, llm_budget,
        )
    fake_llm.generate.assert_not_called()
    assert result.failed and guard.aborted
    assert not [c for c in conn.execute.call_args_list if "segment_research_log" in c.args[0]]


@pytest.mark.asyncio
async def test_llm_budget_is_charged_per_turn():
    from services.acp_contract import segment_research as sr

    conn = MagicMock()
    conn.execute = AsyncMock()
    fake_llm = MagicMock()
    fake_llm.generate.side_effect = [LLMResponse(
        content='{"thought": "t", "tool": "done", "keywords": []}', model_used="haiku-4-5",
        provider="bedrock", input_tokens=10, output_tokens=5, cost_usd=0.0013)]
    llm_budget = _budget(provider="bedrock", per_run_usd=2.0)
    with patch.object(sr, "LLMClient", return_value=fake_llm), \
         patch.object(sr, "record_call_with_pool", new=AsyncMock()):
        await sr._research_place(
            "kyoto", ["explore"], ["US"], [(2840, "United States", "en")], {}, MagicMock(),
            _pool(conn), asyncio.Semaphore(1), None, llm_budget,
        )
    assert llm_budget.run_spent == pytest.approx(0.0013)


# ── alerts ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alert_at_threshold_inserts_notification_once():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    b = _budget(per_day_usd=10.0, day_spent_at_start=8.5)
    assert await cg.maybe_alert(_pool(conn), b) is True
    conn.execute.assert_awaited_once()
    conn.fetchval = AsyncMock(return_value=1)  # an unread alert already exists
    assert await cg.maybe_alert(_pool(conn), b) is False


@pytest.mark.asyncio
async def test_no_alert_below_threshold():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    assert await cg.maybe_alert(_pool(conn), _budget(per_day_usd=10.0, day_spent_at_start=1.0)) is False
    conn.execute.assert_not_awaited()


# ── admin budgets API validation ───────────────────────────────────────────────────────────────

def test_budget_scope_validation():
    from fastapi import HTTPException

    from api.routers.admin_budgets import _valid

    _valid("dfs", "global")
    _valid("bedrock", "job:segment_research")
    with pytest.raises(HTTPException):
        _valid("aws", "global")
    with pytest.raises(HTTPException):
        _valid("dfs", "tenant:x")


# ── KAN-90: daily cap alert-only, per-run cap still hard ──────────────────────────────────────

def test_alert_only_daily_cap_never_stops_but_per_run_cap_does():
    b = _budget(per_run_usd=5.0, per_day_usd=10.0, hard_stop=True, hard_stop_day=False,
                day_spent_at_start=12.0)
    b.check(1.0)  # already over the daily cap: alert-only, so no stop
    b.charge(4.5)
    with pytest.raises(cg.BudgetExceeded) as e:
        b.check(1.0)  # 4.5 + 1.0 > 5.0 per run
    assert e.value.limit == "per_run"


def test_remaining_ignores_an_alert_only_daily_cap():
    b = _budget(per_run_usd=5.0, per_day_usd=10.0, hard_stop=True, hard_stop_day=False,
                day_spent_at_start=9.9)
    assert b.remaining_usd() == pytest.approx(5.0)
    assert b.summary()["hard_stop_day"] is False and b.summary()["hard_stop"] is True


def test_hard_stop_day_defaults_to_hard_stop():
    assert _budget(hard_stop=True).day_is_hard is True
    assert _budget(hard_stop=False).day_is_hard is False


@pytest.mark.asyncio
async def test_load_global_alert_only_keeps_job_per_run_hard():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"scope": "global", "per_run_usd": None, "per_day_usd": 10.0, "hard_stop": False, "alert_pct": 80},
        {"scope": "job:segment_research", "per_run_usd": 5.0, "per_day_usd": None, "hard_stop": True,
         "alert_pct": 80},
    ])
    conn.fetchval = AsyncMock(return_value=11.0)  # already past the daily cap today
    b = await cg.load_run_budget(_pool(conn), "dfs", "segment_research")
    assert (b.hard_stop, b.day_is_hard) == (True, False)
    assert b.remaining_usd() == pytest.approx(5.0)  # a run can still start, bounded by $5
    b.check(4.0)
    b.charge(4.0)
    with pytest.raises(cg.BudgetExceeded):
        b.check(2.0)


@pytest.mark.asyncio
async def test_load_daily_cap_is_hard_if_any_row_setting_it_is_hard():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"scope": "global", "per_run_usd": None, "per_day_usd": 10.0, "hard_stop": False, "alert_pct": 80},
        {"scope": "job:segment_research", "per_run_usd": 5.0, "per_day_usd": 8.0, "hard_stop": True,
         "alert_pct": 80},
    ])
    conn.fetchval = AsyncMock(return_value=0.0)
    b = await cg.load_run_budget(_pool(conn), "dfs", "segment_research")
    assert b.day_is_hard is True and b.per_day_usd == 8.0
