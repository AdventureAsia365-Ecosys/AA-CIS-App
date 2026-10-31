"""AA-1016 (review S262) — a T-stage job stopped for staff approval of its tenant's AI budget really resumes.

Before the fix the stop never reached the job runner for the two main T-stage kinds (the T2 graph and the T9 write
turned it into a failed draft), and when it did, the kinds' on_terminal hooks marked the piece / version 'failed', so
the approval re-queue ran a job that skipped itself ("succeeded", nothing written). These tests drive the whole wait:
stop -> stopped_budget -> on_terminal -> approval re-queue -> the handler runs again and writes.

No database (an in-memory store behind a fake pool), no provider call. Invented tenants and places only.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared import cost_guard
from shared.jobs import queue
from shared.jobs.registry import JobContext
from shared.llm_client import tenant_budget

TENANT = "7a3e1c52-0f4b-4d2e-9c11-5b6a7d8e9f01"
PIECE_ID = "77777777-7777-7777-7777-777777777777"
REQUEST_ID = "88888888-8888-8888-8888-888888888888"
VERSION_ID = "66666666-6666-6666-6666-666666666666"
JOB_ID = "00000000-0000-0000-0000-0000000000a1"
T0 = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def _budget(monthly=10.0, spent=16.0, hard=True):
    return cost_guard.TenantAIBudget(tenant_id=TENANT, monthly_usd=monthly, month_spent_usd=spent, alert_pct=100,
                                     approval_pct=150, hard_stop=hard, month_calls=3)


def _stop():
    return tenant_budget.approval_required(_budget())


class Store:
    """One job row and its domain row (a T9 content piece or a T2 tour version)."""

    def __init__(self, kind, domain_status, payload, budget):
        self.job = {"id": JOB_ID, "kind": kind, "payload": payload, "status": "running", "error": None,
                    "finished_at": None}
        self.domain = domain_status
        self.budget = budget


class Conn:
    def __init__(self, store):
        self.s = store

    async def fetchval(self, sql, *args):
        if "acp_shared.content_piece" in sql or "gold_aa_internal.tenant_tour_versions" in sql:
            return self.s.domain
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "SET status = 'failed'" in sql:
            running = "processing" if "content_piece" in sql else "pending"
            if self.s.domain == running:
                self.s.domain = "failed"
            return "UPDATE 1"
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "quality_score" in sql:                                      # the T2 job's final read
            return {"status": self.s.domain, "score": 0.91, "qa_status": "passed"}
        if "tenant_ai_budget" in sql or "monthly_usd" in sql:           # cost_guard.tenant_budget_check
            b = self.s.budget
            return None if b is None else {
                "tenant_id": TENANT, "monthly_usd": b.monthly_usd, "alert_pct": b.alert_pct,
                "approval_pct": b.approval_pct, "hard_stop": b.hard_stop, "spent": b.month_spent_usd, "calls": 3}
        return {"id": "p-1", "tour_id": "tour-1", "aa_name": "Velmora Ridge Trek", "country": "VN"}   # _PT_SQL


class Pool:
    def __init__(self, store):
        self.s = store

    def acquire(self):
        conn = Conn(self.s)

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return Ctx()

    async def fetch(self, sql, *args):                                   # queue._RETRY_CANDIDATES_SQL
        assert "FROM shared.job j" in sql
        j = self.s.job
        ok = j["status"] == args[4] and (j["error"] or "").startswith(args[0])
        return [dict(j)] if ok else []

    async def fetchrow(self, sql, *args):                                # queue._RETRY_ONE_SQL
        assert "SET status = 'queued'" in sql
        if self.s.job["status"] != args[2]:
            return None
        self.s.job.update(status="queued", error=None, finished_at=None)
        return {"id": args[0]}


def _ctx(pool, kind, payload):
    job = queue.Job(id=JOB_ID, kind=kind, payload=payload, status="running", attempt=1, max_attempts=2,
                    progress={}, cost_usd=0.0)
    ctx = JobContext(pool, job, {"redis": None})
    ctx.progress = AsyncMock()
    return ctx


async def _stop_like_the_worker(store, kind_mod, exc):
    """What Worker._execute_job does with the handler's exception (the shared helper, then the kind's hook)."""
    stop = tenant_budget.approval_stop_in(exc)
    assert stop is not None, "the approval stop must reach the job runner"
    store.job.update(status="stopped_budget", error=str(stop), finished_at=T0)
    hook = {"t9_write": "mark_piece_failed", "t2_rewrite": "mark_version_failed"}[store.job["kind"]]
    await getattr(kind_mod, hook)(Pool(store), dict(store.job))


async def _approve_and_requeue(store):
    store.budget = _budget(monthly=40.0)                                 # staff raised the budget
    with patch("shared.jobs.worker.load_kinds", lambda: None):
        out = await queue.retry_by_reason(Pool(store), queue.RETRY_REASON_TENANT_BUDGET,
                                          since=T0 - timedelta(days=1), dry_run=False, rand=lambda: 0.0)
    assert out["requeued"] == 1 and store.job["status"] == "queued", out
    return out


@pytest.fixture(autouse=True)
def _registered_kinds():
    import services.jobs.t2_rewrite_job  # noqa: F401 — registers the kinds
    import services.jobs.t9_write_job  # noqa: F401


# ── the helpers ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_stop_is_found_in_the_explicit_cause_chain_only():
    stop = _stop()
    assert tenant_budget.approval_stop_in(stop) is stop
    try:
        try:
            raise stop
        except Exception as inner:
            raise RuntimeError("node failed") from inner
    except RuntimeError as wrapped:
        assert tenant_budget.approval_stop_in(wrapped) is stop
    try:
        try:
            raise stop
        except Exception:
            raise ValueError("cleanup failed")       # implicit context only: an unrelated error, not the stop
    except ValueError as unrelated:
        assert tenant_budget.approval_stop_in(unrelated) is None
    assert tenant_budget.approval_stop_in(None) is None


def test_only_an_approval_stopped_job_waits():
    err = str(_stop())
    assert tenant_budget.is_approval_stop({"status": "stopped_budget", "error": err})
    assert not tenant_budget.is_approval_stop({"status": "cancelled", "error": "cancelled by admin"})
    assert not tenant_budget.is_approval_stop({"status": "failed", "error": err})
    assert not tenant_budget.is_approval_stop({"status": "stopped_budget", "error": "Run budget reached"})
    assert not tenant_budget.is_approval_stop(None)


# ── T9: stop, wait, approval, the write really runs ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_t9_write_stopped_for_approval_is_written_after_the_requeue():
    from services.jobs import t9_write_job as t9
    payload = {"request_id": REQUEST_ID, "piece_id": PIECE_ID, "tenant_id": TENANT,
               "context": {"channel": "facebook", "tenant_id": TENANT}}
    store = Store("t9_write", "processing", payload, _budget())
    finishes = []
    real_finish = t9.WritingProgress.finish

    async def spy_finish(self, ok, message=None):
        finishes.append((ok, message))
        await real_finish(self, ok, message)

    with patch.object(t9.WritingProgress, "finish", spy_finish), \
            patch.object(t9.service, "run_write_background", AsyncMock(side_effect=_stop())):
        with pytest.raises(cost_guard.BudgetExceeded) as ei:
            await t9.run(_ctx(Pool(store), "t9_write", payload))
    assert finishes[-1] == (False, tenant_budget.ON_HOLD_MESSAGE)     # the tenant is not told to try again
    await _stop_like_the_worker(store, t9, ei.value)
    assert store.domain == "processing"                                  # waits, not 'failed'

    await _approve_and_requeue(store)

    async def writes(request_id, piece_id, context, pool):
        store.domain = "held"
    with patch.object(t9.service, "run_write_background", AsyncMock(side_effect=writes)) as bg:
        out = await t9.run(_ctx(Pool(store), "t9_write", payload))
    bg.assert_awaited_once()
    assert out == {"piece_id": PIECE_ID, "piece_status": "held"} and "skipped" not in out


@pytest.mark.asyncio
async def test_a_declined_t9_write_marks_its_piece_failed():
    from services.jobs import t9_write_job as t9
    store = Store("t9_write", "processing", {"piece_id": PIECE_ID}, _budget())
    store.job.update(status="cancelled", error="cancelled by admin")    # admin Cancel of the waiting job
    await t9.mark_piece_failed(Pool(store), dict(store.job))
    assert store.domain == "failed"


@pytest.mark.asyncio
async def test_the_t9_write_raises_the_stop_instead_of_a_failed_piece():
    from services.acp_content_writing import service as writing_service
    ctx = {"atom_text": "Velmora Ridge has a lake.", "goal": {"key": "promotion"},
           "channel_style": {"channel": "facebook"}, "brand_audience": {}, "chosen": {}, "cta": "Ask us.",
           "destination": None, "trip_name": None, "brand_rubric_text": "", "channel": "facebook", "atom_id": "a1"}
    stop = _stop()
    with patch.object(writing_service, "write_content", side_effect=stop), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        with pytest.raises(tenant_budget.TenantBudgetApprovalRequired):
            await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    fin.assert_not_awaited()
    with patch.object(writing_service, "write_content", side_effect=RuntimeError("provider outage")), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    assert fin.await_args.kwargs["status"] == "failed"                   # any other failure: as before


# ── T2: stop, wait, approval, the rewrite really runs ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_t2_rewrite_stopped_for_approval_is_written_after_the_requeue():
    from services.jobs import t2_rewrite_job as t2
    payload = {"version_id": VERSION_ID, "tenant_id": TENANT, "published_tour_id": "p-1",
               "rewrite_language": "EN-US"}
    store = Store("t2_rewrite", "pending", payload, _budget())
    prepare = AsyncMock(return_value=({"name": "Velmora Ridge Trek"}, {"_brand_loaded": True}))
    facts = AsyncMock(side_effect=lambda pool, tour, tenant: (tour, None))
    with patch.object(t2, "_prepare", prepare), \
            patch("services.content_generation.t2_facts.with_facts", facts), \
            patch.object(t2, "_rewrite_and_save", AsyncMock(side_effect=_stop())):
        with pytest.raises(cost_guard.BudgetExceeded) as ei:
            await t2.run(_ctx(Pool(store), "t2_rewrite", payload))
    await _stop_like_the_worker(store, t2, ei.value)
    assert store.domain == "pending"                                     # waits, not 'failed'

    await _approve_and_requeue(store)

    async def rewrites(*_a, **_k):
        store.domain = "ai_generated"
        return {}
    with patch.object(t2, "_prepare", prepare), \
            patch("services.content_generation.t2_facts.with_facts", facts), \
            patch.object(t2, "_rewrite_and_save", AsyncMock(side_effect=rewrites)) as rw:
        out = await t2.run(_ctx(Pool(store), "t2_rewrite", payload))
    rw.assert_awaited_once()
    assert out["version_status"] == "ai_generated" and "skipped" not in out


@pytest.mark.asyncio
async def test_a_failed_t2_rewrite_still_marks_its_version_failed():
    from services.jobs import t2_rewrite_job as t2
    store = Store("t2_rewrite", "pending", {"version_id": VERSION_ID}, _budget())
    store.job.update(status="failed", error="RuntimeError: rewrite did not succeed")
    await t2.mark_version_failed(Pool(store), dict(store.job))
    assert store.domain == "failed"


def test_the_t2_graph_and_rewrite_tour_raise_the_stop():
    """graph.generate_node re-raises it; _rewrite_tour must not turn it into a 'failed' dict."""
    from api.routers import v1_pipeline as vp
    from services.content_generation import graph
    stop = _stop()
    state = {"tour": {"name": "Velmora Ridge Trek", "country": "VN", "duration": "2 Days", "itineraries": ""},
             "seo": {}, "few_shots": [], "generated": {}, "retry_count": 0, "feedback": "", "error": "",
             "cost_usd": 0.0, "model_used": "", "brand_system_prompt": "", "brand_style_guide": "",
             "brand_forbidden_words": [], "rewrite_language": "en-US", "model_tier": "haiku",
             "is_tenant_rewrite": True, "is_branded": True, "tenant_id": TENANT}
    with patch("services.content_generation.graph.LLMClient") as client:
        client.return_value.generate.side_effect = stop
        with pytest.raises(tenant_budget.TenantBudgetApprovalRequired):
            graph.generate_node(state)

    class _Graph:
        async def astream(self, *_a, **_k):
            raise RuntimeError("node failed") from stop
            yield  # pragma: no cover

    with patch.object(vp, "build_graph", return_value=_Graph()):
        with pytest.raises(tenant_budget.TenantBudgetApprovalRequired) as ei:
            asyncio.run(vp._rewrite_tour({"name": "T", "country": "VN", "duration": "2 days"}, 0, 1,
                                         brand_rules={}, seo={}))
    assert ei.value is stop


# ── the worker and the decline ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_worker_ends_a_wrapped_stop_stopped_budget(monkeypatch):
    from shared.jobs import registry, worker as worker_mod
    stop = _stop()

    async def handler(ctx):
        try:
            raise stop
        except Exception as e:
            raise RuntimeError("pipeline failed") from e
    monkeypatch.setitem(registry._KINDS, "aa1016_wrapped",
                        registry.JobKind("aa1016_wrapped", handler, max_attempts=2))
    stopped, failed, hooks = AsyncMock(return_value=True), AsyncMock(return_value="queued"), AsyncMock()
    monkeypatch.setattr(worker_mod.queue, "stop_budget", stopped)
    monkeypatch.setattr(worker_mod.queue, "fail", failed)
    monkeypatch.setattr(worker_mod, "run_terminal_hook", hooks)
    w = worker_mod.Worker(MagicMock(), worker_id="w-test")
    job = queue.Job(id=JOB_ID, kind="aa1016_wrapped", payload={"tenant_id": TENANT}, status="running", attempt=1,
                    max_attempts=2, progress={}, cost_usd=0.0)
    await w._execute_job(job)
    assert stopped.await_args.args[3] == str(stop) and str(stop).startswith(tenant_budget.APPROVAL_REASON)
    failed.assert_not_awaited()
    hooks.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_takes_a_stopped_budget_job():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"status": "cancelled"})
    assert await queue.request_cancel(pool, JOB_ID) == "cancelled"
    sql = pool.fetchrow.await_args.args[0]
    assert "status IN ('queued', 'running', 'stopped_budget')" in sql
    assert "WHEN status IN ('queued', 'stopped_budget') THEN 'cancelled'" in sql
