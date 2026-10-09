"""AA-650 — job runner pieces that need no database: backoff, flags, registry, the
segment_research handler and the research router's enqueue path. The SQL (claim, retry, reaper,
idempotency) is covered against real Postgres in tests/integration/test_aa650_job_queue.py."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.jobs import segment_research_job as srj
from shared.cost_guard import BudgetExceeded, RunBudget
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext, NonRetryable
from shared.jobs.worker import in_api_enabled


def test_backoff_doubles_and_is_capped():
    assert [queue.backoff_seconds(n) for n in (1, 2, 3)] == [30, 60, 120]
    assert queue.backoff_seconds(20) == queue.BACKOFF_MAX_SECONDS


@pytest.mark.parametrize("value,expected", [(None, False), ("true", True), ("1", True),
                                            ("false", False), ("0", False)])
def test_in_api_worker_flag(monkeypatch, value, expected):
    # AA-735 (ADR 0003 nac 5): the in-API worker is OFF by default — unset or any falsy value is
    # False; only an explicit truthy JOB_WORKER_IN_API runs it (local/dev).
    if value is None:
        monkeypatch.delenv("JOB_WORKER_IN_API", raising=False)
    else:
        monkeypatch.setenv("JOB_WORKER_IN_API", value)
    assert in_api_enabled() is expected


def test_segment_research_kind_is_registered_with_cap_one():
    k = registry.get_kind("segment_research")
    assert k is not None and k.concurrency == 1 and k.max_attempts == 2


@pytest.mark.asyncio
async def test_enqueue_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        await registry.enqueue(MagicMock(), "nope", {})


# ── segment_research handler ─────────────────────────────────────────────────────────────────

def _ctx(payload=None):
    job = queue.Job(id="j1", kind="segment_research", payload=payload or {"markets": ["US"], "max_places": 5},
                    status="running", attempt=1, max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(MagicMock(), job)
    ctx.progress = AsyncMock()
    return ctx


def _budget(provider, per_run=5.0, spent=0.0):
    return RunBudget(provider=provider, scope="job:segment_research", per_run_usd=per_run,
                     per_day_usd=None, run_spent=spent)


async def _run(ctx, *, result=None, balance=100.0, budgets=None, spend=(0.3, 0.02)):
    dfs, llm = budgets or (_budget("dfs"), _budget("bedrock"))

    async def fake_research(markets, pool, *, dfs_budget, llm_budget, **kw):
        dfs_budget.charge(spend[0])
        llm_budget.charge(spend[1])
        return result or {"places_selected": 5, "aborted": False, "abort_reason": None}

    with patch.object(srj, "load_budgets", AsyncMock(return_value=(dfs, llm))), \
         patch.object(srj, "dfs_balance", AsyncMock(return_value=balance)), \
         patch.object(srj, "maybe_alert", AsyncMock()), \
         patch("services.acp_contract.segment_research.run_segment_research", fake_research):
        return await srj.run(ctx)


@pytest.mark.asyncio
async def test_handler_returns_result_and_records_cost():
    ctx = _ctx()
    out = await _run(ctx)
    assert out["places_selected"] == 5
    # Only the DFS spend (0.3): the 0.02 Bedrock spend reaches the job via llm_call_log.job_id.
    assert ctx.cost_usd == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_handler_without_markets_is_not_retryable():
    with pytest.raises(NonRetryable):
        await _run(_ctx({"max_places": 5}))


@pytest.mark.asyncio
async def test_handler_refuses_when_dfs_balance_is_too_low():
    with pytest.raises(NonRetryable, match="balance"):
        await _run(_ctx(), balance=1.0)


@pytest.mark.asyncio
async def test_handler_budget_already_used_stops_before_any_call():
    with pytest.raises(BudgetExceeded):
        await _run(_ctx(), budgets=(_budget("dfs", per_run=5.0, spent=5.0), _budget("bedrock")))


@pytest.mark.asyncio
async def test_handler_budget_abort_keeps_the_partial_result():
    ctx = _ctx()
    aborted = {"places_selected": 5, "aborted": True, "abort_reason": "dfs per-run budget $5.00 reached"}
    with pytest.raises(BudgetExceeded):
        await _run(ctx, result=aborted)
    assert ctx.result == aborted


@pytest.mark.asyncio
async def test_handler_fatal_dfs_abort_is_not_retryable():
    aborted = {"places_selected": 5, "aborted": True, "abort_reason": "DataForSEO 402 payment required"}
    with pytest.raises(NonRetryable):
        await _run(_ctx(), result=aborted)


# ── research router ───────────────────────────────────────────────────────────────────────────

def _request():
    req = MagicMock()
    req.app.state.pool = MagicMock()
    return req


@pytest.mark.asyncio
async def test_run_refuses_while_a_research_job_is_active():
    from api.routers import admin_segment_research as r
    scope = r.ResearchScope(markets=["US"], max_places=5)
    with patch.object(r, "verify_admin_secret"), \
         patch.object(r.job_queue, "latest", AsyncMock(return_value={"id": "j9", "status": "running"})):
        with pytest.raises(HTTPException) as e:
            await r.run_research(scope, _request(), "secret", "admin-1")
    assert e.value.status_code == 409 and "j9" in e.value.detail


@pytest.mark.asyncio
async def test_run_enqueues_a_job_and_returns_its_id():
    from api.routers import admin_segment_research as r
    scope = r.ResearchScope(markets=["us"], max_places=5)
    preview = {"places_selected": 3}
    with patch.object(r, "verify_admin_secret"), \
         patch.object(r.job_queue, "latest", AsyncMock(return_value=None)), \
         patch("services.acp_contract.segment_research.run_segment_research", AsyncMock(return_value=preview)), \
         patch.object(r, "load_budgets", AsyncMock(return_value=(_budget("dfs"), _budget("bedrock")))), \
         patch.object(r, "dfs_balance", AsyncMock(return_value=50.0)), \
         patch.object(r, "enqueue", AsyncMock(return_value=("job-1", True))) as m_enqueue:
        out = await r.run_research(scope, _request(), "secret", "admin-1")
    assert out["started"] is True and out["job_id"] == "job-1"
    args, kwargs = m_enqueue.call_args
    assert args[1] == "segment_research" and args[2]["markets"] == ["US"] and args[2]["max_places"] == 5
    assert kwargs["created_by"] == "admin:admin-1"
