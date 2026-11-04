"""AA-1042 (S268, lane trace) — the route alerts, the llm_route_watch job and its schedule, the Trace tab reads, the
route policy and the route test (pure parts and mocked I/O).

No AWS, no vendor, no DB, no paid call. The SQL runs in tests/integration/test_aa1042_route_trace_db.py.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.services import llm_route_trace as svc
from services.jobs import llm_route_watch_job as job
from shared import ops_checks
from shared.llm_client import route_watch as rw

NOW = datetime(2026, 11, 4, 9, 7, tzinfo=timezone.utc)
ROUTES = {"stage_a": {"stage": "stage_a", "role": "writer", "model_id": "model-one",
                      "fallback_model_ids": ["model-two", "model-three"], "is_active": True},
          "stage_b": {"stage": "stage_b", "role": "judge", "model_id": "model-one", "fallback_model_ids": [],
                      "is_active": True}}


def _st(answered=10, fb=0, lost=0, max_position=0, primary_at=None, fallback_at=None, cost=0.0, fb_cost=0.0):
    return {"answered": answered, "primary_answered": answered - fb, "fallback_answered": fb,
            "unknown_answered": 0, "fallback_cost_usd": fb_cost, "cost_usd": cost, "failed_attempts": fb,
            "max_position": max_position, "last_primary_at": primary_at, "last_fallback_at": fallback_at,
            "lost": lost}


# ── policy ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_effective_layers_stage_then_star_then_defaults():
    rows = {"*": {"fallback_alert_pct": 30, "min_requests": None},
            "stage_a": {"fallback_alert_pct": None, "min_requests": 2, "hold_fallback_writes": True}}
    a = rw.effective(rows, "stage_a")
    assert (a.fallback_alert_pct, a.min_requests, a.hold_fallback_writes, a.cost_shift_usd) == (30, 2, True, 1.0)
    assert rw.effective({}, "stage_b") == rw.Policy(stage="stage_b")


@pytest.mark.parametrize("fields, msg", [
    ({"fallback_alert_pct": 0}, "fallback_alert_pct"), ({"min_requests": True}, "min_requests"),
    ({"cost_shift_usd": 0}, "cost_shift_usd"), ({"alert_last_rung": "yes"}, "alert_last_rung"),
    ({"notes": "x" * 501}, "notes")])
def test_validate_policy_refuses_bad_values(fields, msg):
    with pytest.raises(ValueError, match=msg):
        rw.validate_policy(fields)


def test_validate_policy_keeps_nulls_and_rounds_the_cost():
    assert rw.validate_policy({"cost_shift_usd": 1.234, "notes": ""}) == {
        "alert_last_rung": None, "hold_fallback_writes": None, "fallback_alert_pct": None, "min_requests": None,
        "cost_shift_usd": 1.23, "notes": None}


# ── alerts ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def _checks(findings):
    return sorted(f.check for f in findings)


def test_a_fallback_rate_above_the_threshold_alerts():
    f = rw.watch_findings({"stage_a": _st(10, fb=3)}, {}, {}, ROUTES, {}, {})
    assert _checks(f) == ["route_fb.stage_a"] and f[0].details["fallback_pct"] == 30.0


def test_below_the_threshold_or_too_few_requests_is_quiet():
    assert rw.watch_findings({"stage_a": _st(10, fb=1)}, {}, {}, ROUTES, {}, {}) == []
    assert rw.watch_findings({"stage_a": _st(4, fb=4)}, {}, {}, ROUTES, {}, {}) == []
    rows = {"stage_a": {"min_requests": 2}}
    assert _checks(rw.watch_findings({"stage_a": _st(4, fb=4)}, {}, {}, ROUTES, rows, {})) == ["route_fb.stage_a"]


def test_the_last_model_of_a_route_alerts_and_a_one_model_route_never_does():
    f = rw.watch_findings({"stage_a": _st(10, fb=1, max_position=2)}, {}, {}, ROUTES, {}, {})
    assert _checks(f) == ["route_last.stage_a"] and f[0].details["last_model"] == "model-three"
    assert rw.watch_findings({"stage_a": _st(10, fb=1, max_position=1)}, {}, {}, ROUTES, {}, {}) == []
    assert rw.watch_findings({"stage_b": _st(10, max_position=0)}, {}, {}, ROUTES, {}, {}) == []
    off = {"stage_a": {"alert_last_rung": False}}
    assert rw.watch_findings({"stage_a": _st(10, fb=1, max_position=2)}, {}, {}, ROUTES, off, {}) == []


def test_primary_back_after_a_fallback_bell():
    bell = NOW - timedelta(minutes=30)
    state = {"route_fb.stage_a": bell}
    back = rw.watch_findings({"stage_a": _st(5, primary_at=NOW - timedelta(minutes=5),
                                             fallback_at=NOW - timedelta(minutes=20))}, {}, {}, ROUTES, {}, state)
    assert _checks(back) == ["route_back.stage_a"]
    # the primary answered before the bell, or a fallback answered after it: not back yet
    assert rw.watch_findings({"stage_a": _st(5, primary_at=bell - timedelta(minutes=1))}, {}, {}, ROUTES, {},
                             state) == []
    assert rw.watch_findings({"stage_a": _st(5, primary_at=NOW - timedelta(minutes=5),
                                             fallback_at=NOW - timedelta(minutes=1))}, {}, {}, ROUTES, {},
                             state) == []
    # a primary-back bell newer than the fallback bell: the stage is healthy, nothing to say
    healthy = {**state, "route_back.stage_a": NOW - timedelta(minutes=10)}
    assert rw.degraded_since(healthy, "stage_a") is None
    assert rw.degraded_since({"route_last.stage_a": bell}, "stage_a") == bell


def test_a_cost_shift_over_24h_alerts_with_a_baseline_only():
    cost = {"stage_a": _st(10, fb=4, fb_cost=2.0)}
    base = {"stage_a": {"n": 50, "avg_cost_usd": 0.1}}
    (f,) = rw.watch_findings({}, cost, base, ROUTES, {}, {})
    assert f.check == "route_cost.stage_a" and f.details["extra_usd"] == pytest.approx(1.6)
    assert rw.watch_findings({}, cost, {"stage_a": {"n": 2, "avg_cost_usd": 0.1}}, ROUTES, {}, {}) == []
    assert rw.watch_findings({}, cost, base, ROUTES, {"*": {"cost_shift_usd": 5}}, {}) == []


def test_the_digest_lists_stages_with_fallbacks_or_lost_requests_only():
    d = rw.digest({"stage_a": _st(10, fb=2), "stage_b": _st(8), "stage_c": _st(3, lost=1)}, {}, ROUTES)
    assert [s["stage"] for s in d["attention"]] == ["stage_a", "stage_c"] and d["answered"] == 21
    f = rw.digest_finding(d)
    assert f.check == "route_digest" and "2 stage(s)" in f.summary
    assert rw.digest_finding(rw.digest({"stage_b": _st(8)}, {}, ROUTES)) is None


def test_every_route_check_resolves_and_fits_the_notification_column():
    stage = "s" * 64
    for prefix in (ops_checks.ROUTE_FALLBACK_PREFIX, ops_checks.ROUTE_LAST_PREFIX, ops_checks.ROUTE_BACK_PREFIX,
                   ops_checks.ROUTE_COST_PREFIX):
        chk = ops_checks.check_for(prefix + stage)
        assert chk.page == ops_checks.TRACE_PAGE
        assert len(ops_checks.EVENT_TYPE_PREFIX + prefix + stage) <= 100        # notifications.event_type
    assert ops_checks.check_for("route_digest").bell_every == timedelta(hours=20)
    with pytest.raises(KeyError):
        ops_checks.check_for("route_fb.")
    subject, text = ops_checks.render(ops_checks.Finding("route_fb.stage_a", "x", {"stage": "stage_a"}))
    assert "fallback" in subject and "tab=trace" in text


def test_evaluate_reads_and_decides(monkeypatch):
    calls = []

    class Db:
        async def fetch(self, sql, *args):
            calls.append(sql)
            if "llm_route_policy" in sql:
                return []
            if "llm_role_config" in sql:
                return [dict(ROUTES["stage_a"])]
            if "shared.notifications" in sql:
                return []
            if "avg(cost_usd)" in sql:
                return []
            return [{"stage": "stage_a", **_st(10, fb=5)}]

    pool = MagicMock()
    pool.acquire.return_value = MagicMock(__aenter__=AsyncMock(return_value=Db()), __aexit__=AsyncMock())
    out = asyncio.run(rw.evaluate(pool, now=NOW))
    assert [f.check for f in out["findings"]] == ["route_fb.stage_a"] and out["policy_ready"] is True
    out = asyncio.run(rw.evaluate(pool, mode="digest", now=NOW))
    assert out["digest"]["attention_total"] == 1 and out["findings"][0].check == "route_digest"


# ── the job and its schedule ────────────────────────────────────────────────────────────────────────────────────────

def test_buckets_and_keys():
    assert job.watch_key(NOW) == "llm_route_watch:2026-11-04T09:00"
    assert job.watch_key(NOW.replace(minute=59)) == "llm_route_watch:2026-11-04T09:45"
    assert job.digest_key(NOW.date()) == "llm_route_digest:2026-11-04"


def test_schedule_switch(monkeypatch):
    assert job.schedule_enabled()
    monkeypatch.setenv(job.SWITCH_ENV, "off")
    assert not job.schedule_enabled()


def test_enqueue_watch_once_per_bucket(monkeypatch):
    enq = AsyncMock(return_value=("j1", True))
    monkeypatch.setattr(job, "enqueue", enq)
    pool = MagicMock(fetchval=AsyncMock(return_value=False))
    assert asyncio.run(job.enqueue_watch(pool, now=NOW)) == ("j1", True, "llm_route_watch:2026-11-04T09:00")
    assert enq.call_args.kwargs["idempotency_key"] == "llm_route_watch:2026-11-04T09:00"
    pool.fetchval = AsyncMock(return_value=True)
    assert asyncio.run(job.enqueue_watch(pool, now=NOW))[:2] == (None, False)


def test_the_digest_waits_for_its_hour(monkeypatch):
    enq = AsyncMock(return_value=("j2", True))
    monkeypatch.setattr(job, "enqueue", enq)
    pool = MagicMock(fetchval=AsyncMock(return_value=False))
    assert asyncio.run(job.enqueue_digest(pool, now=NOW.replace(hour=0))) == (None, False, None)
    assert asyncio.run(job.enqueue_digest(pool, now=NOW))[0] == "j2"
    assert enq.call_args.args[2] == {"trigger": "schedule", "mode": "digest"}


def _ctx(payload):
    ctx = MagicMock()
    ctx.payload = payload
    ctx.job_id = "job-1"
    ctx.pool = MagicMock()
    return ctx


def test_run_raises_the_alerts(monkeypatch):
    finding = ops_checks.Finding("route_fb.stage_a", "s", {"stage": "stage_a"})
    monkeypatch.setattr(rw, "evaluate", AsyncMock(return_value={"mode": "watch", "findings": [finding]}))
    alert = AsyncMock(return_value={"route_fb.stage_a": {"bell": True, "email": "sent"}})
    monkeypatch.setattr(ops_checks, "alert_all", alert)
    out = asyncio.run(job.run(_ctx({"trigger": "schedule", "mode": "watch"})))
    assert out["alerts"] == {"route_fb.stage_a": {"bell": True, "email": "sent"}}
    assert out["findings"][0]["check"] == "route_fb.stage_a"


def test_a_dry_run_raises_nothing(monkeypatch):
    finding = ops_checks.Finding("route_fb.stage_a", "s", {})
    monkeypatch.setattr(rw, "evaluate", AsyncMock(return_value={"mode": "watch", "findings": [finding]}))
    alert = AsyncMock()
    monkeypatch.setattr(ops_checks, "alert_all", alert)
    out = asyncio.run(job.run(_ctx({"mode": "bogus", "dry_run": True})))
    alert.assert_not_called()
    assert out["mode"] == "watch" and out["alerts"] == {}


def test_enqueue_manual_refuses_an_unknown_mode():
    with pytest.raises(ValueError):
        asyncio.run(job.enqueue_manual(MagicMock(), mode="nope", dry_run=False, actor="admin"))


def test_the_sampler_queues_the_watch_and_the_digest(monkeypatch):
    from api.core import ops_metrics_sampler as sampler
    monkeypatch.setattr(job, "enqueue_watch", AsyncMock(return_value=("w", True, "kw")))
    monkeypatch.setattr(job, "enqueue_digest", AsyncMock(return_value=("d", True, "kd")))
    out: dict = {}
    asyncio.run(sampler._llm_route_watch_part(MagicMock(), {job.KIND: object()}, NOW, out))
    assert out == {"llm_route_watch": {"job_id": "w", "key": "kw"}, "llm_route_digest": {"job_id": "d", "key": "kd"}}
    out = {}
    asyncio.run(sampler._llm_route_watch_part(MagicMock(), {}, NOW, out))
    assert out == {}


# ── Trace tab reads ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_window_stage_and_trace_id_parsing():
    assert svc.window_of(None) == ("24h", timedelta(hours=24))
    with pytest.raises(ValueError):
        svc.window_of("2d")
    assert svc.parse_stage("") is None and svc.parse_stage("stage_a") == "stage_a"
    with pytest.raises(ValueError):
        svc.parse_stage("Stage A")
    tid = "6F1C5F8E-1B9E-4C55-9F5E-1A2B3C4D5E6F"
    assert svc.parse_trace_id(tid) == tid.lower()
    with pytest.raises(ValueError):
        svc.parse_trace_id("nope")


def test_requests_sql_has_its_filters():
    sql = svc.requests_sql(stage="stage_a", fallback_only=True)
    assert "AND stage = $2" in sql and "LIMIT $3" in sql and "NOT answered" in sql
    sql = svc.requests_sql(stage=None, fallback_only=False)
    assert "stage = $2" not in sql and "LIMIT $2" in sql and "NOT answered" not in sql


def test_request_view_outcomes():
    base = {"trace": "t", "answered": True, "served_attempt": 1, "rows": 1}
    assert svc.request_view(base)["outcome"] == "primary"
    assert svc.request_view({**base, "served_attempt": 2})["outcome"] == "fallback"
    assert svc.request_view({**base, "answered": False})["outcome"] == "no_answer"


def test_trace_view_orders_and_sums():
    rows = [{"created_at": NOW, "stage": "stage_a", "role": "writer", "model": "model-one", "status": "refused",
             "error_class": "access_denied", "attempt_no": 1, "latency_ms": 3, "cost_usd": 0.0, "position": 0,
             "error": "refused", "route_test": True},
            {"created_at": NOW, "stage": "stage_a", "role": "writer", "model": "model-two", "status": "ok",
             "attempt_no": 2, "latency_ms": 900, "cost_usd": 0.0002, "position": 1, "job_id": "j"}]
    v = svc.trace_view("t", rows, ROUTES["stage_a"])
    assert v["outcome"] == "fallback" and v["served"]["model"] == "model-two" and v["latency_ms"] == 903
    assert v["route"] == ["model-one", "model-two", "model-three"] and v["route_test"] and v["job_id"] == "j"
    assert svc.trace_view("t", [], None) is None
    v = svc.trace_view("t", rows[:1], None)
    assert v["outcome"] == "no_answer" and v["served"] is None


def test_rungs_view_shares():
    rung_rows = [{"stage": "stage_a", "model": "model-one", "connection": "acc3", "rung": "primary", "position": 0,
                  "answered": 6, "cost_usd": 0.6, "avg_latency_ms": 100.4},
                 {"stage": "stage_a", "model": "model-two", "connection": "api", "rung": "fallback", "position": 1,
                  "answered": 2, "cost_usd": 0.4, "avg_latency_ms": None}]
    (s,) = svc.rungs_view(rung_rows, {"stage_a": _st(8, fb=2, lost=1)}, ROUTES)
    assert [r["share_pct"] for r in s["rungs"]] == [75.0, 25.0] and s["fallback_pct"] == 25.0
    assert s["lost_requests"] == 1 and s["rungs"][0]["avg_latency_ms"] == 100
    assert svc.rungs_view(rung_rows, {}, ROUTES, stage="stage_b") == []


def test_held_reads_the_degraded_list():
    assert svc._held('[{"name": "fallback_writer", "severity": "hard"}]')
    assert not svc._held([{"name": "fallback_writer", "severity": "count"}]) and not svc._held("{bad")


# ── route test ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _pool_with_routes(routes):
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[dict(r) for r in routes.values()])
    pool = MagicMock()
    pool.acquire.return_value = MagicMock(__aenter__=AsyncMock(return_value=db), __aexit__=AsyncMock())
    return pool


@pytest.mark.parametrize("stage, refuse, msg", [
    ("missing", False, "no route"), ("stage_b", True, "no fallback model"), ("stage_e", False, "only chat")])
def test_route_test_refuses(stage, refuse, msg):
    routes = {**ROUTES, "stage_e": {"stage": "stage_e", "role": "embed", "model_id": "e", "fallback_model_ids": [],
                                    "is_active": True}}
    with pytest.raises(svc.RouteTestRefused, match=msg):
        asyncio.run(svc.run_route_test(_pool_with_routes(routes), stage=stage, refuse_primary=refuse, count=1,
                                       actor="admin"))


def test_route_test_logs_each_answer_with_its_billed_fields(monkeypatch):
    from shared.llm_client import call_log
    from shared.llm_client.models import LLMResponse
    resp = LLMResponse(content="ok", model_used="model-two", provider="openai", cost_usd=0.0001, attempt_no=2,
                       route_trace="t-1", route_position=1, fallback_used=True, connection_key="api")
    seen = []
    monkeypatch.setattr(svc, "_test_one", lambda stage, refuse: (seen.append((stage, refuse)), resp)[1])
    rec = AsyncMock()
    monkeypatch.setattr(call_log, "record_call_with_pool", rec)
    out = asyncio.run(svc.run_route_test(_pool_with_routes(ROUTES), stage="stage_a", refuse_primary=True, count=99,
                                         actor="admin:x"))
    assert len(out["requests"]) == svc.TEST_MAX_REQUESTS and seen[0] == ("stage_a", True)
    kw = rec.call_args.kwargs
    assert kw["route_trace"] == "t-1" and kw["route_position"] == 1 and kw["quality_signal"]["route_test"] is True
    assert out["requests"][0]["trace_id"] == "t-1" and out["cost_usd"] == pytest.approx(0.0006)


def test_route_test_reports_a_failed_request(monkeypatch):
    def boom(stage, refuse):
        raise RuntimeError("every model refused")
    monkeypatch.setattr(svc, "_test_one", boom)
    out = asyncio.run(svc.run_route_test(_pool_with_routes(ROUTES), stage="stage_a", refuse_primary=False, count=1,
                                         actor="admin"))
    assert out["requests"] == [{"n": 1, "ok": False, "error": "RuntimeError: every model refused"}]


# ── router ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def _request():
    r = MagicMock()
    r.app.state.pool = MagicMock()
    return r


def test_policy_write_errors_map_to_http(monkeypatch):
    from api.routers import admin_llm_route as route
    monkeypatch.setattr(svc, "write_policy", AsyncMock(side_effect=svc.PolicyUnavailable("needs 373")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(route.put_route_policy(route.RoutePolicyBody(), _request(), stage="stage_a"))
    assert e.value.status_code == 503
    monkeypatch.setattr(svc, "write_policy", AsyncMock(side_effect=ValueError("bad")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(route.put_route_policy(route.RoutePolicyBody(), _request(), stage="stage_a"))
    assert e.value.status_code == 422
    monkeypatch.setattr(svc, "clear_policy", AsyncMock(side_effect=LookupError("stage_a")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(route.delete_route_policy(_request(), stage="stage_a"))
    assert e.value.status_code == 404


def test_trace_lookup_answers_404_and_422(monkeypatch):
    from api.routers import admin_llm_route as route
    monkeypatch.setattr(svc, "read_trace", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as e:
        asyncio.run(route.get_route_trace(_request(), trace_id="6f1c5f8e-1b9e-4c55-9f5e-1a2b3c4d5e6f", at=None))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        asyncio.run(route.get_route_trace(_request(), trace_id="zz", at=None))
    assert e.value.status_code == 422


def test_route_test_is_audited_and_refusals_are_422(monkeypatch):
    from api.routers import admin_llm_route as route
    monkeypatch.setattr(svc, "run_route_test", AsyncMock(return_value={"requests": [{"ok": True}], "cost_usd": 0.1}))
    audit = AsyncMock()
    monkeypatch.setattr(route, "write_audit_log", audit)
    out = asyncio.run(route.post_route_test(route.RouteTestBody(stage="stage_a", refuse_primary=True, count=2),
                                            _request(), x_admin_user_id="u1"))
    assert out["cost_usd"] == 0.1 and audit.call_args.kwargs["action"] == "llm_route_test_run"
    assert audit.call_args.kwargs["details"]["refuse_primary"] is True
    monkeypatch.setattr(svc, "run_route_test", AsyncMock(side_effect=svc.RouteTestRefused("no route")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(route.post_route_test(route.RouteTestBody(stage="stage_a"), _request()))
    assert e.value.status_code == 422


def test_the_page_access_lists_the_trace_writes():
    from api.routers.admin_ai_gateway import PAGE_WRITES
    assert PAGE_WRITES["route_policy"] == ("PUT", "/admin/ai-gateway/route-policy/{stage}")
    assert PAGE_WRITES["route_test"] == ("POST", "/admin/ai-gateway/route-trace/test")
