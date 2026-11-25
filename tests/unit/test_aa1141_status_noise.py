"""AA-1141 (S292, status page noise) — two Needs-attention items of the Ecosystem status page the tech lead confirmed as
noise, fixed in their checks (not their thresholds):

1. ``rds_role_connections`` fired on one sampler tick while an app's old and new task overlapped in a rolling deploy
   (a role at 5 of its 6 connections, the limit sized for that overlap). With the sampler loop's run counts it
   alerts only when the role stays above the line on ``cis.ops.role_connections_samples`` ticks in a row (CIS 488).
2. The route digest listed the connection probe stage (it calls every model, the ones an account refuses included) as
   a stage with requests that had no answer. Only routed stages are judged now.

No DB, no AWS, no model call; settings patched on the reader; invented role and stage names only."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core import ops_metrics_sampler as sampler
from shared import ops_checks, ops_thresholds, settings
from shared.llm_client import route_watch as rw

LINE = 80.0


@pytest.fixture
def setting(monkeypatch):
    values: dict = {}
    real = settings.get

    def get(key, tenant_id=None):
        return values[key] if key in values else real(key, tenant_id)

    monkeypatch.setattr(settings, "get", get)
    return values


def _tick(conns: int, limit: int = 6, role: str = "zz_app") -> dict:
    return {"roles": {role: {"connections": conns, "limit": limit, "pct": round(100.0 * conns / limit, 2)}}}


def _role_findings(tick: dict, streaks) -> list:
    return [f for f in ops_checks.state_findings(tick, streaks) if f.check == "rds_role_connections"]


# ── 1. the role connections check ─────────────────────────────────────────────────────────────────────────────────

def test_the_default_needs_three_samples_in_a_row():
    assert ops_thresholds.role_connections_samples() == 3


def test_a_rolling_deploy_overlap_of_one_or_two_ticks_stays_silent():
    streaks: dict = {}
    assert _role_findings(_tick(5), streaks) == []          # the old and the new task overlap: 5 of 6
    assert _role_findings(_tick(6), streaks) == []          # still overlapping on the next tick
    assert _role_findings(_tick(3), streaks) == []          # the old task stopped: back to one pool
    assert streaks == {}
    assert _role_findings(_tick(5), streaks) == []          # the next deploy starts a new run from 1
    assert streaks == {"zz_app": 1}


def test_a_sustained_peak_outside_a_deploy_still_alerts():
    streaks: dict = {}
    assert _role_findings(_tick(5), streaks) == [] and _role_findings(_tick(5), streaks) == []
    (f,) = _role_findings(_tick(5), streaks)
    assert f.summary == "zz_app 5/6"
    assert f.details["samples_needed"] == 3 and f.details["roles"]["zz_app"]["samples"] == 3
    assert f.details["threshold_pct"] == LINE
    (again,) = _role_findings(_tick(6), streaks)            # keeps firing while it lasts (the bell window dedupes)
    assert again.details["roles"]["zz_app"]["samples"] == 4


def test_each_role_has_its_own_run():
    streaks: dict = {}
    both = {"roles": {**_tick(5)["roles"], **_tick(19, 20, "zz_other")["roles"]}}
    only_one = {"roles": {**_tick(5)["roles"], **_tick(4, 20, "zz_other")["roles"]}}
    assert _role_findings(both, streaks) == [] and _role_findings(only_one, streaks) == []
    (f,) = _role_findings(both, streaks)
    assert set(f.details["roles"]) == {"zz_app"} and streaks == {"zz_app": 3, "zz_other": 1}


def test_a_tick_whose_role_read_failed_neither_adds_nor_resets():
    streaks: dict = {}
    _role_findings(_tick(5), streaks)
    _role_findings(_tick(5), streaks)
    assert _role_findings({"rds_error": "TimeoutError"}, streaks) == []
    assert streaks == {"zz_app": 2}
    assert len(_role_findings(_tick(5), streaks)) == 1


def test_the_samples_setting_and_the_line_are_read_on_every_tick(setting):
    streaks: dict = {}
    setting[ops_thresholds.ROLE_CONNECTIONS_SAMPLES] = 1          # the behaviour before S292
    assert len(_role_findings(_tick(5), streaks)) == 1
    setting[ops_thresholds.ROLE_CONNECTIONS_SAMPLES] = 5
    assert _role_findings(_tick(5), streaks) == []                # run 2 of 5
    setting[ops_thresholds.ROLE_CONNECTIONS_PCT] = 90             # 83 % is under the new line: the run resets
    assert _role_findings(_tick(5), streaks) == [] and streaks == {}


def test_without_runs_one_sample_alerts_as_before():
    tick = _tick(6)
    (f,) = _role_findings(tick, None)
    assert f.summary == "zz_app 6/6" and "samples_needed" not in f.details and "roles_above_samples" not in tick


def test_the_check_text_names_the_samples_setting():
    assert "cis.ops.role_connections_samples" in ops_checks.CHECKS["rds_role_connections"].action
    assert ops_thresholds.ROLE_CONNECTIONS_SAMPLES in ops_thresholds.BY_CHECK["rds_role_connections"]


def test_the_sampler_tick_passes_the_loops_runs_and_logs_them(monkeypatch):
    seen = []
    real = ops_checks.state_findings

    def spy(tick, role_runs=None):
        seen.append(role_runs)
        return real(tick, role_runs)

    monkeypatch.setattr(ops_checks, "state_findings", spy)
    monkeypatch.setattr(ops_checks, "app_spend_findings", AsyncMock(return_value=[]))
    monkeypatch.setattr(ops_checks, "stage_coverage_findings", AsyncMock(return_value=[]))
    monkeypatch.setattr(ops_checks, "alert_all", AsyncMock(return_value={}))
    monkeypatch.setattr(ops_checks, "flush_events", AsyncMock(return_value={}))
    streaks: dict = {}
    out = _tick(5)
    asyncio.run(sampler._ops_checks_part(MagicMock(), None, out, streaks))
    assert seen[0] is streaks and out["roles_above_samples"] == {"zz_app": 1}


def test_the_loop_holds_one_run_count_across_ticks(monkeypatch):
    calls = []

    async def fake_sample_once(pool, client, **kw):
        calls.append(kw.get("role_runs"))
        if len(calls) == 2:
            raise asyncio.CancelledError
        return {}

    app = MagicMock()
    app.state.pool = object()
    monkeypatch.setattr(sampler, "sample_once", fake_sample_once)
    monkeypatch.setattr(sampler, "jev_credit_tick", AsyncMock())
    monkeypatch.setattr(sampler, "provider_credit_tick", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sampler.run_ops_metrics_sampler(app, interval_s=0, first_delay_s=0, client_factory=lambda: None,
                                                    jev_check_every_s=3600, recompute_sweep_every_s=3600,
                                                    research_stale_every_s=3600))
    assert len(calls) == 2 and calls[0] == {} and calls[0] is calls[1]


# ── 2. the route digest ───────────────────────────────────────────────────────────────────────────────────────────

ROUTES = {"zz_writer": {"stage": "zz_writer", "role": "writer", "model_id": "model-one",
                        "fallback_model_ids": ["model-two"], "is_active": True}}


def _st(answered=10, fb=0, lost=0):
    return {"answered": answered, "primary_answered": answered - fb, "fallback_answered": fb, "unknown_answered": 0,
            "fallback_cost_usd": 0.0, "cost_usd": 0.0, "failed_attempts": 0, "expected_refusals": 0,
            "max_position": 0, "last_primary_at": None, "last_fallback_at": None, "lost": lost}


def test_the_digest_ignores_the_connection_probe_and_unrouted_stages():
    stats = {"adhoc_conn_test": _st(29, lost=12), "zz_no_route": _st(4, fb=2, lost=1), "zz_writer": _st(10)}
    d = rw.digest(stats, {}, ROUTES)
    assert d["attention_total"] == 0 and rw.digest_finding(d) is None
    assert d["unrouted"] == ["adhoc_conn_test", "zz_no_route"] and d["stages"] == 1 and d["answered"] == 10


def test_the_digest_still_reports_a_routed_stage_with_fallbacks():
    stats = {"adhoc_conn_test": _st(29, lost=12), "zz_writer": _st(10, fb=3)}
    d = rw.digest(stats, {}, ROUTES)
    f = rw.digest_finding(d)
    assert [s["stage"] for s in d["attention"]] == ["zz_writer"] and f.check == "route_digest"
    assert "zz_writer: 3/10 by fallback" in f.summary and "adhoc_conn_test" not in f.summary
    assert "(3 of 10 requests by fallback)" in f.summary


def test_an_adhoc_stage_is_never_routed_even_with_a_route_row():
    routes = {**ROUTES, "adhoc_zz_probe": {**ROUTES["zz_writer"], "stage": "adhoc_zz_probe"}}
    assert not rw.routed_stage("adhoc_zz_probe", routes) and not rw.routed_stage("adhoc_conn_test", routes)
    assert rw.routed_stage("zz_writer", routes)


def test_a_code_route_counts_as_routed_before_its_seed_row():
    from shared.llm_client.role_config import SAFE_DEFAULTS

    stage = sorted(SAFE_DEFAULTS)[0]
    assert rw.routed_stage(stage, {}) and not rw.routed_stage("zz_unknown_stage", {})
