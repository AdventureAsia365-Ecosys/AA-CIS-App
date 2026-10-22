"""AA-859 wave 4 — the six route groups on the tenant pool, each behind its own fail-closed flag.

One fail-closed matrix over every moved handler (the same fakes for all: a connection whose set_config echoes the
tenant, pools that record acquires and releases, the route's tenant-data step stubbed so the test is about which pool
runs it):
  * flag on: the tenant pool, one `<event>` line with used_tenant_pool=True, the tenant-data step on that connection;
  * explicit OFF ("false"): the main pool, used_tenant_pool=False, the tenant pool never touched;
  * a Redis error: still the tenant pool (S241 D-12, fail closed);
  * flag on without a tenant pool: 503 and the tenant-data step never runs;
then the group specifics: the main-pool exceptions run only after the tenant block was released (angle-gate job status
only for 'pending_goal' without a goal; Slate config before, last proposal after; marketplace posts_per_week before),
pick / confirm audits after commit and best-effort (still 200), the pre-wave-4 audit order with the flag off, and a
concurrent confirm's unique violation as VersionConflictError (409 in the router).

One file instead of the plan's six (test_aa859_<group>_pool.py): the matrix is the same for every group, so the fakes
live once; each group's specifics are below its own heading.
"""
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.routers import v1_angle_gate, v1_content_writing, v1_marketplace, v1_planning
from services.acp_angle_gate import service as ag_service
from services.acp_content_writing import service as cw_service
from services.acp_planning import trip_reallocation as tr
from services.acp_planning.models import QuarterPlan
from services.acp_planning.tenant_config import TenantPlanningConfig

TENANT_ID = str(uuid.uuid4())
REQUEST_ID = uuid.uuid4()
SUBJECT_ID = uuid.uuid4()


# ── fakes ─────────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, name, events):
        self.name, self.events = name, events
        self.tx = MagicMock()
        self.tx.__aenter__ = AsyncMock(return_value=None)
        self.tx.__aexit__ = AsyncMock(return_value=False)
        self.transaction = MagicMock(return_value=self.tx)
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value={"posts_per_week": 3})
        self.execute = AsyncMock(return_value="OK")

    async def fetchval(self, sql, *args):
        if "set_config('app.tenant_id'" in sql:
            return args[0]
        return None


class _Pool:
    def __init__(self, name, events, error=None):
        self.name, self.events, self.error = name, events, error
        self.conn = _Conn(name, events)
        self.acquires = 0

    def acquire(self, timeout=None):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                if pool.error is not None:
                    raise pool.error
                pool.acquires += 1
                pool.events.append(f"{pool.name}_acquired")
                return pool.conn

            async def __aexit__(self, *exc):
                pool.events.append(f"{pool.name}_released")
                return False
        return _Ctx()

    async def execute(self, sql, *args):   # write_audit_log(pool, ...)
        self.events.append(f"{self.name}_execute")
        return "INSERT 0 1"


def _request(flag_key, redis="true", tenant_pool=True):
    events = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())
    request.app.state.pool = _Pool("main", events)
    request.app.state.tenant_pool = _Pool("tenant", events) if tenant_pool else None
    redis_get = AsyncMock(side_effect=ConnectionError("redis down")) if redis == "error" else \
        AsyncMock(side_effect=lambda key: redis if key == flag_key else "false")
    request.app.state.redis = SimpleNamespace(get=redis_get)
    return request, events


def _step(events, name, result):
    """A stubbed tenant-data step that records which connection ran it."""
    async def _run(conn, *args, **kwargs):
        events.append(f"{name}@{conn.name}")
        return result
    return AsyncMock(side_effect=_run)


# ── one entry per handler: (flag, event, call(request, events) -> awaitable, step name) ──────────────

async def _angle_gate_get(request, events):
    with patch.object(ag_service, "fetch_request_on",
                      new=_step(events, "read", {"request_id": str(REQUEST_ID), "status": "approved", "goal": "g"})):
        return await v1_angle_gate.get_request(REQUEST_ID, request, tenant={"sub": TENANT_ID})


async def _slate_get(request, events):
    with patch.object(v1_planning, "request_slate_proposal", new=AsyncMock(return_value=None)), \
            patch.object(v1_planning, "_resolve_config", new=AsyncMock(
                return_value=TenantPlanningConfig(markets=["US"], channels=["blog"], capacity_posts_per_week=3))), \
            patch.object(v1_planning, "fetch_slate_on", new=_step(events, "read", {})), \
            patch.object(v1_planning, "last_proposal", new=AsyncMock(return_value=None)):
        return await v1_planning.get_slate(request, tenant={"sub": TENANT_ID})


_PICKED = {"subject_id": str(SUBJECT_ID), "channel": "blog", "request_id": str(REQUEST_ID), "atom_id": "a1",
           "trip_id": None, "status": "pending_goal", "route_segment_ids": None}


async def _pick(request, events):
    with patch.object(v1_planning, "pick_subject_on", new=_step(events, "read", dict(_PICKED))), \
            patch.object(v1_planning, "write_pick_audit", new=_step(events, "audit", None)):
        return await v1_planning.post_pick_subject(SUBJECT_ID, request, tenant={"sub": TENANT_ID})


async def _cut(request, events):
    with patch.object(v1_planning, "cut_subject_on",
                      new=_step(events, "read", {"subject_id": str(SUBJECT_ID), "channel": "blog", "state": "cut"})):
        return await v1_planning.post_cut_subject(SUBJECT_ID, request, tenant={"sub": TENANT_ID})


_CONFIG = TenantPlanningConfig(markets=["US"], channels=["blog"], capacity_posts_per_week=2)


def _suggestion():
    plan = QuarterPlan(tenant_id=uuid.UUID(TENANT_ID), year=2031, quarter=1, trip_ids=[], trip_scores=[])
    return {"plan": plan.model_dump(mode="json"), "has_existing_plan": False, "added": [], "removed": [],
            "unchanged": []}


async def _suggest(request, events):
    with patch.object(tr, "fetch_tenant_planning_config", new=AsyncMock(return_value=_CONFIG)), \
            patch.object(tr, "_read_inputs_on", new=_step(events, "read", ([], {}, {}, None))), \
            patch.object(tr, "_suggestion", return_value=_suggestion()):
        return await v1_planning.get_trip_reallocation_suggestion(request, tenant={"sub": TENANT_ID}, year=2031,
                                                                  quarter=1)


async def _confirm_reject(request, events):
    with patch.object(tr, "fetch_tenant_planning_config", new=AsyncMock(return_value=_CONFIG)), \
            patch.object(tr, "_read_inputs_on", new=_step(events, "read", ([], {}, {}, None))), \
            patch.object(tr, "_suggestion", return_value=_suggestion()):
        body = v1_planning.TripReallocationConfirmRequest(year=2031, quarter=1, accept=False)
        return await v1_planning.post_trip_reallocation_confirm(body, request, tenant={"sub": TENANT_ID})


async def _marketplace(request, events):
    out = await v1_marketplace.get_marketplace(request, tenant={"sub": TENANT_ID})
    events.append("read@" + ("tenant" if request.app.state.tenant_pool and
                             request.app.state.tenant_pool.conn.fetch.await_count else "main"))
    return out


async def _reviews(request, events):
    out = await v1_content_writing.list_reviews(request, tenant={"sub": TENANT_ID})
    events.append("read@" + ("tenant" if request.app.state.tenant_pool and
                             request.app.state.tenant_pool.conn.fetch.await_count else "main"))
    return out


_REVIEW_REQ = {"request_id": str(REQUEST_ID), "atom_id": "a1", "trip_id": None, "channel": "blog", "goal": None,
               "angles": [], "dfs_paa_snapshot": None, "cta": None}


async def _review(request, events):
    pools = [p for p in (request.app.state.pool, request.app.state.tenant_pool) if p is not None]
    for p in pools:
        p.conn.fetchrow = AsyncMock(side_effect=[
            {"piece_id": uuid.uuid4(), "status": "approved", "content_text": "x", "channel": "blog",
             "angle_gate_option_id": None, "created_at": datetime(2026, 10, 22),
             "route_hub_name": None, "route_segment_count": None, "flags": None},
            {"text": "t", "activity_type": None, "emotional_hook": None, "season_note": None}])
    with patch.object(cw_service.angle_gate_service, "fetch_request_on", new=_step(events, "read", dict(_REVIEW_REQ))):
        return await v1_content_writing.get_review(REQUEST_ID, request, tenant={"sub": TENANT_ID})


HANDLERS = [
    pytest.param(tp.ANGLE_GATE_READ_FLAG, "aa859_angle_gate_read", _angle_gate_get, id="GET angle-gate request"),
    pytest.param(tp.SLATE_READS_FLAG, "aa859_slate_read", _slate_get, id="GET /v1/slate"),
    pytest.param(tp.SUBJECT_DECIDE_FLAG, "aa859_subject_pick", _pick, id="POST subjects/{id}/pick"),
    pytest.param(tp.SUBJECT_DECIDE_FLAG, "aa859_subject_cut", _cut, id="POST subjects/{id}/cut"),
    pytest.param(tp.TRIP_REALLOCATION_FLAG, "aa859_trip_reallocation_suggest", _suggest, id="GET realloc suggest"),
    pytest.param(tp.TRIP_REALLOCATION_FLAG, "aa859_trip_reallocation_confirm", _confirm_reject,
                 id="POST realloc confirm"),
    pytest.param(tp.MARKETPLACE_FLAG, "aa859_marketplace_read", _marketplace, id="GET /v1/marketplace"),
    pytest.param(tp.REVIEWS_FLAG, "aa859_reviews_list", _reviews, id="GET content-writing reviews"),
    pytest.param(tp.REVIEWS_FLAG, "aa859_review_detail", _review, id="GET requests/{id}/review"),
]


def _lines(logs, event):
    return [entry for entry in logs if entry["event"] == event]


@pytest.mark.parametrize("flag,event,call", HANDLERS)
@pytest.mark.asyncio
async def test_flag_on_runs_on_the_tenant_pool(flag, event, call):
    request, events = _request(flag)
    with capture_logs() as logs:
        await call(request, events)
    assert "read@tenant" in events and "read@main" not in events
    assert request.app.state.tenant_pool.acquires == 1
    assert [e["used_tenant_pool"] for e in _lines(logs, event)] == [True]
    assert [t["flag_key"] for t in _lines(logs, "aa859_tenant_pool_timing")] == [flag]


@pytest.mark.parametrize("flag,event,call", HANDLERS)
@pytest.mark.asyncio
async def test_explicit_off_runs_on_the_main_pool(flag, event, call):
    request, events = _request(flag, redis="false")
    with capture_logs() as logs:
        await call(request, events)
    assert "read@main" in events and "read@tenant" not in events
    assert request.app.state.tenant_pool.acquires == 0
    assert [e["used_tenant_pool"] for e in _lines(logs, event)] == [False]


@pytest.mark.parametrize("flag,event,call", HANDLERS)
@pytest.mark.asyncio
async def test_redis_error_keeps_the_tenant_pool(flag, event, call):
    request, events = _request(flag, redis="error")
    with capture_logs() as logs:
        await call(request, events)
    assert "read@tenant" in events
    assert _lines(logs, "aa817_tenant_flag_read_failed")


@pytest.mark.parametrize("flag,event,call", HANDLERS)
@pytest.mark.asyncio
async def test_flag_on_without_a_tenant_pool_is_503(flag, event, call):
    request, events = _request(flag, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await call(request, events)
    assert exc.value.status_code == 503
    assert "read@main" not in events and "read@tenant" not in events


@pytest.mark.parametrize("flag,event,call", HANDLERS)
@pytest.mark.asyncio
async def test_no_tenant_connection_is_503(flag, event, call):
    request, events = _request(flag)
    request.app.state.tenant_pool.error = asyncpg.TooManyConnectionsError("too many connections")
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await call(request, events)
    assert exc.value.status_code == 503
    assert [u["reason"] for u in _lines(logs, "aa817_tenant_pool_unavailable")] == ["too_many_connections"]


def test_each_group_has_its_own_flag():
    assert {tp.ANGLE_GATE_READ_FLAG, tp.SLATE_READS_FLAG, tp.SUBJECT_DECIDE_FLAG, tp.TRIP_REALLOCATION_FLAG,
            tp.MARKETPLACE_FLAG, tp.REVIEWS_FLAG} == {
        "aa859:angle_gate_read_pool", "aa859:slate_reads_pool", "aa859:subject_decide_pool",
        "aa859:trip_reallocation_pool", "aa859:marketplace_pool", "aa859:reviews_pool"}


# ── angle-gate read: the job status on MAIN, after the block, only when it matters ─────────────────

@pytest.mark.parametrize("status,goal,reads_job", [
    ("pending_goal", None, True), ("pending_goal", "promotion", False), ("pending_choice", "promotion", False),
    ("approved", "promotion", False),
])
@pytest.mark.asyncio
async def test_angle_gate_job_status_read_only_for_pending_goal_without_goal(status, goal, reads_job):
    request, events = _request(tp.ANGLE_GATE_READ_FLAG)
    result = {"request_id": str(REQUEST_ID), "status": status, "goal": goal, "generation_failed": False}

    async def _job(pool, request_id):
        events.append(f"job@{pool.name}")
        return "failed"
    with patch.object(ag_service, "fetch_request_on", new=_step(events, "read", dict(result))), \
            patch.object(ag_service, "generation_status", new=AsyncMock(side_effect=_job)):
        out = await v1_angle_gate.get_request(REQUEST_ID, request, tenant={"sub": TENANT_ID})
    if reads_job:
        assert events.index("job@main") > events.index("tenant_released"), "job status read after release"
        assert out["generation_failed"] is True
    else:
        assert "job@main" not in events and out["generation_failed"] is False


@pytest.mark.asyncio
async def test_angle_gate_not_found_is_404_and_reads_no_job():
    request, events = _request(tp.ANGLE_GATE_READ_FLAG)
    not_found = AsyncMock(side_effect=ag_service.RequestNotFoundError("x"))
    with patch.object(ag_service, "fetch_request_on", new=not_found), \
            patch.object(ag_service, "generation_status", new=AsyncMock()) as job:
        with pytest.raises(HTTPException) as exc:
            await v1_angle_gate.get_request(REQUEST_ID, request, tenant={"sub": TENANT_ID})
    assert exc.value.status_code == 404
    job.assert_not_called()


# ── Slate: config before, last proposal after ──────────────────────────────────

@pytest.mark.asyncio
async def test_slate_main_reads_around_the_tenant_block():
    request, events = _request(tp.SLATE_READS_FLAG)

    async def _config(tenant_id, pool):
        events.append("config")
        return TenantPlanningConfig(markets=["US"], channels=["linkedin", "blog", "unknown"], capacity_posts_per_week=3)

    async def _last(tenant_id, conn):
        events.append(f"last@{conn.name}")
        return {"result": {"unmatched_markets": ["XX"]}, "finished_at": None}
    slate_on = _step(events, "read", {})
    with patch.object(v1_planning, "request_slate_proposal", new=AsyncMock(return_value=None)), \
            patch.object(v1_planning, "fetch_tenant_planning_config", new=AsyncMock(side_effect=_config)), \
            patch.object(v1_planning, "fetch_slate_on", new=slate_on), \
            patch.object(v1_planning, "last_proposal", new=AsyncMock(side_effect=_last)):
        out = await v1_planning.get_slate(request, tenant={"sub": TENANT_ID})
    assert events.index("config") < events.index("tenant_acquired")
    assert events.index("last@main") > events.index("tenant_released")
    assert slate_on.await_args.args[2] == ["blog", "linkedin"], "CHANNEL_BARS order, unknown channels dropped"
    assert out["unmatched_markets"] == ["XX"] and out["posts_per_week"] == 3


# ── pick: audit after commit, best-effort; flag off keeps it in the transaction ────────────────────

@pytest.mark.asyncio
async def test_pick_audit_runs_on_main_after_the_tenant_block():
    request, events = _request(tp.SUBJECT_DECIDE_FLAG)
    await _pick(request, events)
    assert events.index("audit@main") > events.index("tenant_released")
    request.app.state.tenant_pool.conn.transaction.assert_called_once()   # acquire_scoped_conn's only


@pytest.mark.asyncio
async def test_pick_audit_failure_still_answers_200():
    request, events = _request(tp.SUBJECT_DECIDE_FLAG)
    with patch.object(v1_planning, "pick_subject_on", new=_step(events, "read", dict(_PICKED))), \
            patch.object(v1_planning, "write_pick_audit", new=AsyncMock(side_effect=RuntimeError("db down"))), \
            capture_logs() as logs:
        out = await v1_planning.post_pick_subject(SUBJECT_ID, request, tenant={"sub": TENANT_ID})
    assert out["request_id"] == str(REQUEST_ID)
    assert _lines(logs, "aa859_subject_pick_audit_failed")


@pytest.mark.asyncio
async def test_pick_flag_off_writes_the_audit_inside_its_own_transaction():
    request, events = _request(tp.SUBJECT_DECIDE_FLAG, redis="false")
    await _pick(request, events)
    main = request.app.state.pool.conn
    main.transaction.assert_called_once()
    assert events.index("audit@main") < events.index("main_released"), "same connection, same transaction"


@pytest.mark.parametrize("error,status", [(v1_planning.SubjectNotFoundError("x"), 404),
                                          (v1_planning.SubjectNotEligibleError("x"), 409)])
@pytest.mark.asyncio
async def test_pick_domain_errors_roll_back_and_write_no_audit(error, status):
    request, events = _request(tp.SUBJECT_DECIDE_FLAG)
    with patch.object(v1_planning, "pick_subject_on", new=AsyncMock(side_effect=error)), \
            patch.object(v1_planning, "write_pick_audit", new=AsyncMock()) as audit:
        with pytest.raises(HTTPException) as exc:
            await v1_planning.post_pick_subject(SUBJECT_ID, request, tenant={"sub": TENANT_ID})
    assert exc.value.status_code == status
    audit.assert_not_called()


# ── trip reallocation confirm ─────────────────────────────────────────────────

async def _confirm(request, events, accept=True, save_error=None, audit_error=None):
    async def _save(conn, plan, source="standard"):
        events.append(f"save@{conn.name}")
        if save_error:
            raise save_error
        return uuid.uuid4()

    async def _approve(conn, version_id, approved_by):
        events.append(f"approve@{conn.name}")

    async def _audit(conn_or_pool, *args, **kwargs):
        events.append(f"audit@{conn_or_pool.name}:{kwargs.get('applied')}")
        if audit_error:
            raise audit_error
    with patch.object(tr, "fetch_tenant_planning_config", new=AsyncMock(return_value=_CONFIG)), \
            patch.object(tr, "_read_inputs_on", new=_step(events, "read", ([], {}, {}, None))), \
            patch.object(tr, "_suggestion", return_value=_suggestion()), \
            patch.object(tr, "save_quarter_plan_version_on", new=AsyncMock(side_effect=_save)), \
            patch.object(tr, "approve_quarter_plan_version_on", new=AsyncMock(side_effect=_approve)), \
            patch.object(tr, "_write_reallocation_audit", new=AsyncMock(side_effect=_audit)):
        return await tr.confirm_trip_reallocation_scoped(request, uuid.UUID(TENANT_ID), 2031, 1, accept,
                                                         actor=f"tenant:{TENANT_ID}")


@pytest.mark.asyncio
async def test_confirm_accept_saves_and_approves_in_one_tenant_block_then_audits_on_main():
    request, events = _request(tp.TRIP_REALLOCATION_FLAG)
    out = await _confirm(request, events)
    assert out["accepted"] is True
    assert request.app.state.tenant_pool.acquires == 2            # reads, then save + approve (sequential)
    i_save, i_approve = events.index("save@tenant"), events.index("approve@tenant")
    assert i_save < i_approve and "tenant_released" not in events[i_save:i_approve]
    assert events[-1] == "audit@main:True"
    lock = request.app.state.tenant_pool.conn.execute.await_args_list[0].args
    assert "pg_advisory_xact_lock" in lock[0] and lock[1] == f"{TENANT_ID}:quarter_plan:2031:1"


@pytest.mark.asyncio
async def test_confirm_reject_writes_only_the_audit():
    request, events = _request(tp.TRIP_REALLOCATION_FLAG)
    out = await _confirm(request, events, accept=False)
    assert out == {"accepted": False, "suggestion": _suggestion()}
    assert request.app.state.tenant_pool.acquires == 1
    assert events[-1] == "audit@main:False" and not [e for e in events if e.startswith("save@")]


@pytest.mark.asyncio
async def test_confirm_unique_violation_is_a_version_conflict_and_still_audits():
    request, events = _request(tp.TRIP_REALLOCATION_FLAG)
    with pytest.raises(tr.VersionConflictError):
        await _confirm(request, events, save_error=asyncpg.UniqueViolationError("duplicate key"))
    assert events[-1] == "audit@main:False"


@pytest.mark.asyncio
async def test_confirm_audit_failure_still_answers():
    request, events = _request(tp.TRIP_REALLOCATION_FLAG)
    with capture_logs() as logs:
        out = await _confirm(request, events, audit_error=RuntimeError("db down"))
    assert out["accepted"] is True
    assert _lines(logs, "aa859_trip_reallocation_audit_failed")


@pytest.mark.asyncio
async def test_confirm_flag_off_keeps_the_pre_wave_4_order():
    """Flag off: the reads on MAIN, then audit FIRST (a lost audit row fails the confirm), then the pool functions."""
    request, events = _request(tp.TRIP_REALLOCATION_FLAG, redis="false")
    with patch.object(tr, "fetch_tenant_planning_config", new=AsyncMock(return_value=_CONFIG)), \
            patch.object(tr, "_read_inputs_on", new=_step(events, "read", ([], {}, {}, None))), \
            patch.object(tr, "_suggestion", return_value=_suggestion()), \
            patch.object(tr, "save_quarter_plan_version",
                         new=AsyncMock(side_effect=lambda *a, **k: events.append("save@pool") or uuid.uuid4())), \
            patch.object(tr, "approve_quarter_plan_version",
                         new=AsyncMock(side_effect=lambda *a, **k: events.append("approve@pool"))):
        out = await tr.confirm_trip_reallocation_scoped(request, uuid.UUID(TENANT_ID), 2031, 1, True,
                                                        actor=f"tenant:{TENANT_ID}")
    assert out["accepted"] is True and request.app.state.tenant_pool.acquires == 0
    assert events.index("main_execute") < events.index("save@pool") < events.index("approve@pool")


# ── marketplace: posts_per_week on MAIN before the tenant block ─────────────────────────────────

@pytest.mark.asyncio
async def test_marketplace_reads_posts_per_week_on_main_first():
    request, events = _request(tp.MARKETPLACE_FLAG)
    out = await v1_marketplace.get_marketplace(request, tenant={"sub": TENANT_ID})
    assert events[:4] == ["main_acquired", "main_released", "tenant_acquired", "tenant_released"]
    assert out["posts_per_week"] == 3
    request.app.state.pool.conn.fetch.assert_not_called()
