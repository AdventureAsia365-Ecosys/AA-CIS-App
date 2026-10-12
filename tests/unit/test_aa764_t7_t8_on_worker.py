"""AA-764 (G5) — T7 slate proposal, T8 angle generation and T9 fact-select run on the worker,
not inside HTTP requests (ADR 0003)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from api.routers import v1_angle_gate, v1_planning
from services.acp_angle_gate import service as ag
from services.acp_content_writing import service as cw
from services.acp_shared import slate
from services.jobs import t7_slate_job, t8_angles_job
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext, NonRetryable

TENANT = UUID("11111111-1111-1111-1111-111111111111")
REQUEST = UUID("22222222-2222-2222-2222-222222222222")
T8_PAYLOAD = {"request_id": str(REQUEST), "tenant_id": str(TENANT), "goal": "promotion"}


def _pool(conn):
    acq = AsyncMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool


def _ctx(pool, kind, payload):
    job = queue.Job(id="j1", kind=kind, payload=payload, status="running", attempt=1,
                    max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(pool, job, {"redis": None})
    ctx.progress = AsyncMock()
    return ctx


def test_kinds_registered():
    kinds = registry.kinds()
    assert {"t7_slate", "t8_angles"} <= set(kinds)
    assert kinds["t8_angles"].on_terminal is t8_angles_job.clear_goal


# ── T8: POST .../goal records the goal and enqueues ──────────────────────────────────────────

@pytest.mark.asyncio
class TestStartGoalGeneration:
    async def test_enqueues_t8_job_with_top_level_tenant(self):
        conn = AsyncMock()
        conn.fetchval.return_value = REQUEST  # conditional UPDATE claimed the request
        pool = _pool(conn)
        with patch.object(ag, "_fetch_request_row", new=AsyncMock(return_value={"status": "pending_goal"})), \
             patch("shared.jobs.registry.enqueue", new=AsyncMock(return_value=("job-1", True))) as enq, \
             patch.object(ag, "fetch_request", new=AsyncMock(return_value={"generating": True})):
            out = await ag.start_goal_generation(TENANT, REQUEST, "promotion", pool)
        assert out == {"generating": True}
        assert enq.call_args.args[0] is conn            # same transaction as the UPDATE
        assert enq.call_args.args[1] == "t8_angles"
        assert enq.call_args.args[2] == T8_PAYLOAD
        assert "goal IS NULL" in conn.fetchval.call_args.args[0]

    async def test_double_click_is_409(self):
        conn = AsyncMock()
        conn.fetchval.return_value = None  # goal already set — a job is in flight
        with patch.object(ag, "_fetch_request_row", new=AsyncMock(return_value={"status": "pending_goal"})), \
             patch("shared.jobs.registry.enqueue", new=AsyncMock()) as enq:
            with pytest.raises(ag.WrongStatusError):
                await ag.start_goal_generation(TENANT, REQUEST, "promotion", _pool(conn))
        enq.assert_not_called()

    async def test_wrong_status_and_unknown_goal(self):
        with patch.object(ag, "_fetch_request_row", new=AsyncMock(return_value={"status": "pending_choice"})):
            with pytest.raises(ag.WrongStatusError):
                await ag.start_goal_generation(TENANT, REQUEST, "promotion", MagicMock())
        with patch.object(ag, "_fetch_request_row", new=AsyncMock(return_value={"status": "pending_goal"})):
            with pytest.raises(ag.InvalidGoalError):
                await ag.start_goal_generation(TENANT, REQUEST, "not_a_goal", MagicMock())

    async def test_router_returns_service_result(self):
        req = MagicMock()
        with patch.object(v1_angle_gate.service, "start_goal_generation",
                          new=AsyncMock(return_value={"generating": True})) as start, \
             patch.object(v1_angle_gate.service, "set_goal_and_generate", new=AsyncMock()) as direct:
            out = await v1_angle_gate.set_goal(REQUEST, v1_angle_gate.SetGoalBody(goal="promotion"),
                                               req, tenant={"sub": str(TENANT)})
        assert out == {"generating": True}
        start.assert_awaited_once()
        direct.assert_not_called()  # no LLM work in the request


@pytest.mark.asyncio
class TestT8Job:
    async def test_runs_generation(self):
        with patch.object(ag, "fetch_request", new=AsyncMock(return_value={"status": "pending_goal"})), \
             patch.object(ag, "set_goal_and_generate", new=AsyncMock(return_value={
                 "angles": [{"idx": 0, "recommended": False}, {"idx": 1, "recommended": True}]})) as gen:
            out = await t8_angles_job.run(_ctx(MagicMock(), "t8_angles", T8_PAYLOAD))
        assert gen.await_args.args[:3] == (TENANT, REQUEST, "promotion")
        assert out["recommended_index"] == 1

    async def test_already_done_is_skipped(self):
        with patch.object(ag, "fetch_request", new=AsyncMock(return_value={"status": "pending_choice"})), \
             patch.object(ag, "set_goal_and_generate", new=AsyncMock()) as gen:
            out = await t8_angles_job.run(_ctx(MagicMock(), "t8_angles", T8_PAYLOAD))
        assert out["skipped"] is True
        gen.assert_not_called()

    async def test_bad_payload_is_non_retryable(self):
        with pytest.raises(NonRetryable):
            await t8_angles_job.run(_ctx(MagicMock(), "t8_angles", {"request_id": str(REQUEST)}))

    async def test_on_terminal_clears_goal(self):
        conn = AsyncMock()
        await t8_angles_job.clear_goal(_pool(conn), {"id": "j1", "status": "failed",
                                                     "payload": {"request_id": str(REQUEST)}})
        sql = conn.execute.call_args.args[0]
        assert "SET goal = NULL" in sql and "status = 'pending_goal'" in sql


# ── T7: GET /v1/slate only reads ──────────────────────────────────────────────────────────────

def _job(status, finished=None):
    return {"id": "j", "status": status, "result": {}, "created_at": datetime.now(timezone.utc),
            "finished_at": finished}


@pytest.mark.asyncio
class TestRequestSlateProposal:
    async def test_in_flight_job_is_reused(self):
        conn = AsyncMock()
        with patch.object(slate, "latest_slate_job", new=AsyncMock(return_value=_job("running"))), \
             patch("shared.jobs.registry.enqueue", new=AsyncMock()) as enq:
            out = await slate.request_slate_proposal(TENANT, _pool(conn), only_if_stale=False)
        assert out["status"] == "running"
        enq.assert_not_called()
        assert "pg_advisory_xact_lock" in conn.execute.call_args.args[0]

    async def test_fresh_proposal_is_not_redone_on_get(self):
        conn = AsyncMock()
        conn.fetchval.return_value = True  # finished_at within the stale window
        done = _job("succeeded", datetime.now(timezone.utc))
        with patch.object(slate, "latest_slate_job", new=AsyncMock(return_value=done)), \
             patch.object(slate, "last_proposal",
                          new=AsyncMock(return_value={"result": {}, "finished_at": done["finished_at"]})), \
             patch("shared.jobs.registry.enqueue", new=AsyncMock()) as enq:
            await slate.request_slate_proposal(TENANT, _pool(conn), only_if_stale=True)
        enq.assert_not_called()

    async def test_never_proposed_enqueues(self):
        conn = AsyncMock()
        with patch.object(slate, "latest_slate_job", new=AsyncMock(side_effect=[None, _job("queued")])), \
             patch.object(slate, "last_proposal", new=AsyncMock(return_value=None)), \
             patch("shared.jobs.registry.enqueue", new=AsyncMock(return_value=("j", True))) as enq:
            out = await slate.request_slate_proposal(TENANT, _pool(conn), only_if_stale=True)
        assert enq.call_args.args[1:3] == ("t7_slate", {"tenant_id": str(TENANT)})
        assert out["status"] == "queued"

    async def test_refresh_ignores_freshness(self):
        conn = AsyncMock()
        done = _job("succeeded", datetime.now(timezone.utc))
        with patch.object(slate, "latest_slate_job", new=AsyncMock(side_effect=[done, _job("queued")])), \
             patch.object(slate, "last_proposal", new=AsyncMock()) as last, \
             patch("shared.jobs.registry.enqueue", new=AsyncMock(return_value=("j", True))) as enq:
            await slate.request_slate_proposal(TENANT, _pool(conn), only_if_stale=False)
        enq.assert_awaited_once()
        last.assert_not_called()


@pytest.mark.asyncio
async def test_get_slate_never_proposes_inline():
    request = MagicMock()
    request.app.state.pool = _pool(AsyncMock())
    with patch.object(v1_planning, "request_slate_proposal",
                      new=AsyncMock(return_value=_job("running"))) as req, \
         patch.object(slate, "propose_slate", new=AsyncMock()) as propose, \
         patch.object(v1_planning, "_resolve_config",
                      new=AsyncMock(return_value=MagicMock(capacity_posts_per_week=3))), \
         patch.object(v1_planning, "fetch_slate", new=AsyncMock(return_value={"blog": {}})), \
         patch.object(v1_planning, "last_proposal",
                      new=AsyncMock(return_value={"result": {"unmatched_markets": ["XX"]}, "finished_at": None})):
        out = await v1_planning.get_slate(request, tenant={"sub": str(TENANT)})
    propose.assert_not_called()
    assert req.await_args.kwargs == {"only_if_stale": True}
    assert out["refresh"] == {"status": "running", "refreshing": True, "finished_at": None}
    assert out["unmatched_markets"] == ["XX"]


@pytest.mark.asyncio
async def test_t7_job_runs_propose():
    with patch.object(slate, "propose_slate", new=AsyncMock(return_value={"segments_seen": 3})) as propose:
        out = await t7_slate_job.run(_ctx(MagicMock(), "t7_slate", {"tenant_id": str(TENANT)}))
    assert propose.await_args.args[0] == TENANT
    assert out == {"segments_seen": 3}


# ── T9: fact-select runs in the job ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestResolveFacts:
    async def test_pending_facts_are_selected_once(self):
        ctx = {"facts_pending": True, "tenant_id": str(TENANT), "atom_text": "seed", "chosen": {},
               "trip_name": "Trip"}
        with patch.object(cw, "fetch_facts_for_writing", new=AsyncMock(return_value=[])) as fetch, \
             patch.object(cw, "select_relevant_facts", new=AsyncMock(return_value=([], 0))):
            await cw.resolve_facts(REQUEST, ctx, MagicMock())
            await cw.resolve_facts(REQUEST, ctx, MagicMock())
        fetch.assert_awaited_once()
        assert ctx["facts_pending"] is False and ctx["facts_text"] == ""

    async def test_pre_aa764_context_untouched(self):
        ctx = {"facts_text": "[Fact id=f]"}
        with patch.object(cw, "fetch_facts_for_writing", new=AsyncMock()) as fetch:
            await cw.resolve_facts(REQUEST, ctx, MagicMock())
        fetch.assert_not_called()
        assert ctx == {"facts_text": "[Fact id=f]"}
