"""AA-1126 (S283) review fixes — the spend controls end where they say they end (no paid call, no database).

- G1 (the gateway tests are in test_aa1126_gateway_caps.py, with its fake providers): a budget skip is noted per
  request only.
- G4: an S1 rewrite job that reaches its run cap ends ``stopped_budget`` (not ``failed``): ``_run_tour_safe`` stops at
  the first spend stop and raises it, also when the graph swallowed the refusal (the gateway notes it for the job), and
  the worker reads a spend stop in a wrapped failure.
- G2: only CIS refuses on its breaker (its gateway reads it); TripPlanner and the CRM show ``enforced`` false and are
  alerted by the 15 minute ``llm_route_watch`` run, not only on a CIS call.
- One source per cap: the per-run cap of job:s1_rewrite / eval / adhoc is the Platform › Settings value only (the
  central budget's per_run_usd is ignored there and refused on write).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from shared import cost_guard
from shared.jobs import queue
from shared.jobs.registry import JobContext
from shared.llm_client import anomaly_breaker, caps_settings, central_budget, connection_budget, run_cap
from tests.unit.test_aa1126_gateway_caps import JOB, NOW, POLICY, _Conn, _settings


def _cap_stop(spent=2.0) -> run_cap.RunCapReached:
    run = run_cap.Run("s1_rewrite", "job", JOB)
    return run_cap.refusal(run, 2.0, spent)


# ── G1 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_budget_skip_is_noted_only_inside_a_request():
    connection_budget.note_skip("acc3")                                   # outside run_with_cap_mode: nothing kept
    seen = []

    def call():
        connection_budget.note_skip("acc1")
        seen.append(list(connection_budget._SKIPPED.get()))
        return "ok"
    assert connection_budget.run_with_cap_mode(call, "s1_generate") == "ok"
    assert seen == [["acc1"]] and connection_budget._SKIPPED.get() is None



# ── G4: the S1 rewrite job ends stopped_budget ──────────────────────────────────────────────────────────────────

def test_spend_stop_in_reads_the_explicit_chain_only():
    stop = _cap_stop()
    wrapped = RuntimeError("node failed")
    wrapped.__cause__ = stop
    trip = anomaly_breaker.AnomalyBreakerTripped("tripped", "llm", "anomaly")
    assert run_cap.spend_stop_in(stop) is stop and run_cap.spend_stop_in(wrapped) is stop
    assert run_cap.spend_stop_in(trip) is trip
    context_only = RuntimeError("later")
    context_only.__context__ = stop
    assert run_cap.spend_stop_in(context_only) is None
    assert run_cap.spend_stop_in(connection_budget.exceeded("over", ("acc3",))) is None
    assert run_cap.spend_stop_in(None) is None


@pytest.mark.asyncio
async def test_run_tour_safe_stops_at_a_run_cap_and_raises_it():
    from api.routers import admin_pipeline as ap

    stop = _cap_stop()
    wrapped = RuntimeError("graph failed")
    wrapped.__cause__ = stop
    req = ap.TourRunRequest(tour_id="t1", batch_id="not-a-uuid", tenant_id="x")
    with patch.object(ap, "_execute_run_tour", AsyncMock(side_effect=wrapped)) as run, \
            patch.object(ap.asyncio, "sleep", AsyncMock()) as sleep:
        with pytest.raises(run_cap.RunCapReached) as ei:
            await ap._run_tour_safe(req, job_id=JOB)
    assert ei.value is stop and run.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_tour_safe_stops_on_a_stop_the_graph_swallowed():
    from api.routers import admin_pipeline as ap

    stop = _cap_stop()
    run_cap._job_stops[JOB] = stop                                        # the gateway's note (graph: a failed draft)
    req = ap.TourRunRequest(tour_id="t1", batch_id="not-a-uuid", tenant_id="x")
    try:
        with patch.object(ap, "_execute_run_tour", AsyncMock(side_effect=RuntimeError("no draft"))) as run, \
                patch.object(ap.asyncio, "sleep", AsyncMock()):
            with pytest.raises(run_cap.RunCapReached):
                await ap._run_tour_safe(req, job_id=JOB)
        assert run.await_count == 1
    finally:
        run_cap.invalidate()


def _s1_ctx():
    job = queue.Job(id=JOB, kind="s1_rewrite", payload={"tour_id": "t1", "batch_id": "b", "tenant_id": "x"},
                    status="running", attempt=1, max_attempts=1, progress={}, cost_usd=0.0)
    ctx = JobContext(None, job, {"redis": None})
    ctx.progress = AsyncMock()
    return job, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("version_id, phase_done", [(None, False), ("v-1", True)])
async def test_the_s1_job_raises_a_stop_the_graph_swallowed(version_id, phase_done):
    from services.jobs import s1_rewrite_job as sj

    _, ctx = _s1_ctx()
    stop = _cap_stop()

    async def run_tour(*a, **k):
        run_cap._job_stops[JOB] = stop
        return {"version_id": version_id, "status": "held"}
    with patch("api.routers.admin_pipeline._build_s1_progress_sink", return_value=None), \
            patch("api.routers.admin_pipeline._run_tour_safe", run_tour):
        with pytest.raises(run_cap.RunCapReached):
            await sj.run(ctx)
    calls = [c.kwargs for c in ctx.progress.await_args_list]
    assert any(c.get("phase") == "done" for c in calls) is phase_done   # a stored version stays resumable
    cleared = any("s1_writer_state" in c and "phase" not in c for c in calls)
    assert cleared is not phase_done                                     # nothing stored: resume state cleared
    assert run_cap.job_stop(JOB) is None                                # the note never outlives its job


@pytest.mark.asyncio
async def test_the_worker_ends_an_s1_job_at_its_run_cap_stopped_budget(monkeypatch):
    from services.jobs import s1_rewrite_job  # noqa: F401 — registers the kind
    from shared.jobs import worker as worker_mod

    job, _ = _s1_ctx()
    stop = _cap_stop()
    stopped, failed, hooks = AsyncMock(return_value=True), AsyncMock(return_value="failed"), AsyncMock()
    monkeypatch.setattr(worker_mod.queue, "stop_budget", stopped)
    monkeypatch.setattr(worker_mod.queue, "fail", failed)
    monkeypatch.setattr(worker_mod, "run_terminal_hook", hooks)
    monkeypatch.setattr(JobContext, "progress", AsyncMock())
    w = worker_mod.Worker(MagicMock(), worker_id="w-test")
    with patch("api.routers.admin_pipeline._build_s1_progress_sink", return_value=None), \
            patch("api.routers.admin_pipeline._run_tour_safe", AsyncMock(side_effect=stop)):
        await w._execute_job(job)
    assert stopped.await_args.args[3] == str(stop) and "run cap reached" in str(stop)
    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_worker_reads_a_wrapped_spend_stop(monkeypatch):
    from shared.jobs import registry, worker as worker_mod

    stop = anomaly_breaker.AnomalyBreakerTripped("tripped", "llm", "anomaly")

    async def handler(ctx):
        raise RuntimeError("pipeline failed") from stop
    monkeypatch.setitem(registry._KINDS, "aa1126_wrapped", registry.JobKind("aa1126_wrapped", handler, max_attempts=2))
    stopped, failed = AsyncMock(return_value=True), AsyncMock(return_value="queued")
    monkeypatch.setattr(worker_mod.queue, "stop_budget", stopped)
    monkeypatch.setattr(worker_mod.queue, "fail", failed)
    monkeypatch.setattr(worker_mod, "run_terminal_hook", AsyncMock())
    w = worker_mod.Worker(MagicMock(), worker_id="w-test")
    job = queue.Job(id=JOB, kind="aa1126_wrapped", payload={}, status="running", attempt=1, max_attempts=2,
                    progress={}, cost_usd=0.0)
    await w._execute_job(job)
    assert stopped.await_args.args[3] == "tripped"
    failed.assert_not_awaited()


# ── G2: enforced for CIS only, every app alerted by the watch run ─────────────────────────────────────────────

def _verdict(app, mode="enforce", spent=90.0):
    return anomaly_breaker.verdict(app, mode, POLICY, since=NOW, spent_usd=spent, median_usd=1.0)


def test_only_an_app_whose_gateway_reads_the_breaker_refuses():
    assert anomaly_breaker.ENFORCED_APPS == ("cis",)
    cis, tp = _verdict("cis"), _verdict("tripplanner")
    assert cis.refuses and cis.as_dict()["enforced"] is True
    assert tp.tripped and not tp.refuses and tp.as_dict()["enforced"] is False
    assert "does not read the breaker yet" in anomaly_breaker.message(tp)
    assert "refused" in anomaly_breaker.message(cis)


def test_the_watch_run_alerts_every_tripped_app_once(monkeypatch):
    _settings(monkeypatch, anomaly_mode="enforce", multiple=4, floor=25, days=14)

    async def no_profiles(conn):
        return {}
    from shared.llm_client import app_profile
    monkeypatch.setattr(app_profile, "read_all", no_profiles)
    rung = []

    async def bell(conn, summary, **kw):
        rung.append((summary["scope"], kw["stopped"]))
    monkeypatch.setattr(cost_guard, "maybe_alert_central", bell)
    anomaly_breaker.invalidate()
    rows = [30.0, 1.0, 60.0, 1.0, 0.0, 0.0]                             # (today, median): cis, tripplanner, crm
    assert asyncio.run(anomaly_breaker.alert_tripped(_Conn(list(rows)), now=NOW)) == ["cis", "tripplanner"]
    assert rung == [("anomaly:cis", True), ("anomaly:tripplanner", False)]
    assert asyncio.run(anomaly_breaker.alert_tripped(_Conn(list(rows)), now=NOW)) == ["cis", "tripplanner"]
    assert len(rung) == 2                                                # once per trip window in this process
    anomaly_breaker.invalidate()


class _Pool:
    def __init__(self):
        self.conn = object()

    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False
        return _Acq()


def test_the_route_watch_run_evaluates_the_breaker(monkeypatch):
    from services.jobs import llm_route_watch_job as job
    from shared.llm_client import route_watch

    monkeypatch.setattr(route_watch, "evaluate", AsyncMock(side_effect=lambda pool, mode: {"mode": mode,
                                                                                          "findings": []}))
    tripped = AsyncMock(return_value=["crm"])
    monkeypatch.setattr(anomaly_breaker, "alert_tripped", tripped)
    pool = _Pool()
    ctx = MagicMock()
    ctx.pool, ctx.payload, ctx.job_id = pool, {"trigger": "schedule", "mode": "watch"}, JOB
    out = asyncio.run(job.run(ctx))
    assert out["anomaly_tripped"] == ["crm"] and tripped.await_args.args[0] is pool.conn
    ctx.payload = {"mode": "digest"}
    assert "anomaly_tripped" not in asyncio.run(job.run(ctx))           # the digest and a dry run do not alert
    ctx.payload = {"mode": "watch", "dry_run": True}
    assert "anomaly_tripped" not in asyncio.run(job.run(ctx))
    assert tripped.await_count == 1


def test_a_failed_breaker_read_never_fails_the_watch_run(monkeypatch):
    from services.jobs import llm_route_watch_job as job

    async def boom(conn):
        raise RuntimeError("no database")
    monkeypatch.setattr(anomaly_breaker, "alert_tripped", boom)
    assert asyncio.run(job.anomaly_watch(_Pool())) is None


# ── one source per cap ─────────────────────────────────────────────────────────────────────────────────────────

def test_a_central_s1_rewrite_row_has_no_per_run_cap():
    row = central_budget.row_of({"scope": "job:s1_rewrite", "per_run_usd": 3, "per_day_usd": 40, "hard_stop": True})
    assert row.per_run_usd is None and row.per_day_usd == 40.0
    other = central_budget.row_of({"scope": "job:a3_atomize", "per_run_usd": 3, "hard_stop": True})
    assert other.per_run_usd == 3.0
    assert caps_settings.run_class_of_scope("job:s1_rewrite") == "s1_rewrite"
    assert caps_settings.run_class_of_scope("stage:s1_generate") is None
    assert set(caps_settings.RUN_CAP_SCOPES.values()) == set(caps_settings.RUN_CAP_KEYS)


def test_the_budgets_api_refuses_a_per_run_cap_the_settings_own():
    from api.routers import admin_budgets as ab

    with pytest.raises(HTTPException) as e:
        ab._valid("llm", "job:s1_rewrite", ab.BudgetUpdate(per_run_usd=3))
    assert e.value.status_code == 422 and caps_settings.RUN_CAP_S1_REWRITE_USD in e.value.detail
    ab._valid("llm", "job:s1_rewrite", ab.BudgetUpdate(per_day_usd=40))       # its day cap stays here
    ab._valid("llm", "job:a3_atomize", ab.BudgetUpdate(per_run_usd=3))


def test_the_spend_controls_read_names_the_scopes_whose_run_cap_is_a_setting():
    from api.services import ai_gateway_spend_controls as sc

    assert sc.run_cap_scopes() == {"job:s1_rewrite": caps_settings.RUN_CAP_S1_REWRITE_USD,
                                   "eval": caps_settings.RUN_CAP_EVAL_USD, "adhoc": caps_settings.RUN_CAP_ADHOC_USD}
