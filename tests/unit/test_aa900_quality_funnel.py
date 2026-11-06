"""AA-900 (S271 W0) — the content quality funnel (services/quality/funnel.py), its CIS-08 report
(services/reports/funnel_report.py), the nightly watchdog job (quality_funnel_watch) and GET /admin/quality/funnel.

No database: the SQL itself runs in tests/integration/test_aa900_quality_funnel_sql.py.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.jobs import quality_funnel_watch_job as watch_job
from services.jobs import report_jobs, report_ops_jobs
from services.quality import funnel
from services.reports import funnel_report, generate, periods
from services.reports.registry import load
from shared import ops_checks

UTC = timezone.utc
DAY = date(2026, 11, 5)          # a Thursday
NOW = datetime(2026, 11, 6, 0, 40, tzinfo=UTC)


# ── names the SQL reads must match the code that writes them ──────────────────────────────────────────────────────


def test_ledger_names_match_the_gate_code():
    from services.acp_content_writing import sentence_repair
    from services.quality import degraded, registry as qr

    assert funnel.F9_GATE == qr.F9_CTA_FACT
    assert funnel.F9_JUDGE_UNAVAILABLE == degraded.F9_JUDGE_UNAVAILABLE
    assert funnel.TEXT_INTEGRITY_PREFIX == sentence_repair.TEXT_INTEGRITY_PREFIX


def test_the_f9_outage_violation_text_is_what_the_judge_writes():
    import inspect

    from services.acp_content_writing import quality_gates

    assert f'f"{funnel.JUDGE_UNAVAILABLE_TEXT}: ' in inspect.getsource(quality_gates)


@pytest.mark.parametrize("stage,wf", [("t8_angle_gen", "t9"), ("t9_write", "t9"), ("t10_judge", "t9"),
                                      ("s1_generate", "s1"), ("t2_generate", "t2"), ("a3_atomize", None),
                                      ("t1_x", None)])
def test_stage_workflow(stage, wf):
    assert funnel.workflow_of(stage) == wf


def test_every_sql_text_binds_only_what_it_is_given():
    import re

    for sql, n in ((funnel.T9_OBJECTS_SQL, 2), (funnel.T9_GATES_SQL, 2), (funnel.T9_FIRST_GATE_SQL, 2),
                   (funnel.T9_COST_SQL, 2), (funnel.S1_OBJECTS_SQL, 2), (funnel.S1_GATES_SQL, 2),
                   (funnel.T2_OBJECTS_SQL, 3), (funnel.STAGES_SQL, 3), (funnel.WATCH_SQL, 4)):
        assert max(int(m) for m in re.findall(r"\$(\d+)", sql)) == n


def test_every_cte_of_a_workflow_counts_from_the_same_base_rows():
    """Lessons (Dữ liệu): the columns of a funnel count one set. Every T9 query starts from the same piece CTE."""
    for sql in (funnel.T9_OBJECTS_SQL, funnel.T9_GATES_SQL, funnel.T9_FIRST_GATE_SQL, funnel.T9_COST_SQL):
        assert funnel._PIECES in sql


# ── window and derived rates ──────────────────────────────────────────────────────────────────────────────────────


def test_window_is_eight_iso_weeks_ending_with_the_day():
    first, last, start, end = funnel.window(DAY)
    assert first == date(2026, 9, 14) and first.weekday() == 0 and last == DAY
    assert (start, end) == periods.range_bounds(first, DAY)


def test_object_row_rates():
    r = funnel._object_row({"week": "2026-11-02", "created": 10, "approved": 4, "held": 4, "failed": 2,
                            "first_pass": 1, "attempts_delivered": 20, "attempts_approved": 8,
                            "attempts_held": 12}, 2.0)
    assert r["delivered"] == 8
    assert r["first_pass_yield"] == 0.1          # clean on attempt 1 / finished (8 + 2)
    assert r["hold_rate"] == 0.5 and r["attempts_per_delivered"] == 2.5
    assert r["attempts_per_approved"] == 2.0 and r["attempts_per_held"] == 3.0
    assert r["usd_per_delivered"] == 0.25


def test_object_row_with_no_delivery_has_no_rates():
    r = funnel._object_row({"week": "w", "created": 1, "delivered": 0, "held": 0, "failed": 0, "first_pass": 0,
                            "attempts_delivered": 0}, 0.0)
    assert r["first_pass_yield"] is None and r["usd_per_delivered"] is None and r["hold_rate"] is None


def test_s1_held_drafts_are_not_delivered_but_count_as_finished():
    r = funnel._object_row({"week": "w", "created": 4, "delivered": 2, "held": 1, "failed": 1, "first_pass": 1,
                            "attempts_delivered": 3}, 1.0, funnel.HELD_IN_DELIVERED["s1"])
    assert r["first_pass_yield"] == 0.25 and r["hold_rate"] == round(1 / 3, 4) and r["usd_per_delivered"] == 0.5


def test_totals_split_window_and_this_week():
    rows = [funnel._object_row({"week": "2026-10-26", "created": 4, "approved": 1, "held": 1, "failed": 0,
                                "first_pass": 1, "attempts_delivered": 3}, 1.0),
            funnel._object_row({"week": "2026-11-02", "created": 2, "approved": 2, "held": 0, "failed": 0,
                                "first_pass": 0, "attempts_delivered": 4}, 1.0)]
    t = funnel.totals(rows, [], [], last_week="2026-11-02")
    assert t["t9"]["window"]["delivered"] == 4 and t["t9"]["window"]["usd_per_delivered"] == 0.5
    assert t["t9"]["this_week"]["delivered"] == 2 and t["t9"]["this_week"]["first_pass_yield"] == 0.0
    assert t["s1"]["window"]["delivered"] == 0


def test_t9_totals_use_the_whole_stage_cost_and_keep_the_attributed_part():
    rows = [funnel._object_row({"week": "2026-10-26", "created": 2, "approved": 2, "held": 0, "failed": 0,
                                "first_pass": 2, "attempts_delivered": 2}, 0.2),
            funnel._object_row({"week": "2026-11-02", "created": 2, "approved": 2, "held": 0, "failed": 0,
                                "first_pass": 2, "attempts_delivered": 2}, 0.2)]
    t = funnel.totals(rows, [], [], last_week="2026-11-02",
                      t9_unattributed={"2026-10-26": 0.6, "2026-11-02": 0.4})
    week, win = t["t9"]["this_week"], t["t9"]["window"]
    assert (week["cost_usd"], week["attributed_cost_usd"], week["unattributed_cost_usd"]) == (0.6, 0.2, 0.4)
    assert week["usd_per_delivered"] == 0.3 and week["attributed_usd_per_delivered"] == 0.1
    assert (win["cost_usd"], win["unattributed_cost_usd"], win["usd_per_delivered"]) == (1.4, 1.0, 0.35)
    # S1 and T2 already carry the whole stage cost: no attributed split
    assert "attributed_cost_usd" not in t["s1"]["window"]


def test_s1_first_pass_excludes_the_fix_pass_and_human_review_and_counts_ever_published():
    sql = funnel.S1_OBJECTS_SQL
    assert "retry_count = 0 AND NOT fix_pass AND NOT human" in sql
    assert "retry_count + 1 + fix_pass::int" in sql
    assert "silver_aa_internal.review_queue" in sql and "human_edited" in sql and "reviewed_by" in sql
    assert "gc.status::text IN ('approved', 'published')" in sql


# ── watchdog ──────────────────────────────────────────────────────────────────────────────────────────────────────

REC, END = datetime(2026, 10, 30, 17, tzinfo=UTC), datetime(2026, 11, 5, 17, tzinfo=UTC)


def _w(stage, model, win, answered, fb=0, replies=None, cut=0):
    return {"stage": stage, "model": model, "win": win, "answered": answered, "fallback_answered": fb,
            "replies": answered if replies is None else replies, "cut": cut}


def _checks(rows, routes=None):
    return sorted(f.check for f in funnel.watch_findings(funnel.summarize_watch(
        rows, routes or {}, recent=REC, end=END)))


def test_a_quiet_week_raises_nothing():
    assert _checks([_w("t9_write", "model-a", "recent", 50), _w("t9_write", "model-a", "prior", 50)],
                   {"t9_write": "model-a"}) == []


def test_cut_rate_above_the_threshold_alerts():
    assert _checks([_w("t9_write", "model-a", "recent", 40, cut=3)]) == ["qf_cut.t9_write"]       # 7.5 %
    assert _checks([_w("t9_write", "model-a", "recent", 40, cut=2)]) == []                         # 5.0 %, not above
    assert _checks([_w("t9_write", "model-a", "recent", 10, cut=5)]) == []                         # too few


def test_fallback_share_at_the_threshold_alerts():
    assert _checks([_w("t10_judge", "model-b", "recent", 40, fb=8)]) == ["qf_fb.t10_judge"]       # 20 %
    assert _checks([_w("t10_judge", "model-b", "recent", 40, fb=7)]) == []


def test_served_model_drift_needs_a_model_new_against_the_week_before():
    rows = [_w("t2_judge", "model-old", "prior", 100), _w("t2_judge", "model-old", "recent", 30),
            _w("t2_judge", "model-new", "recent", 10)]
    s = funnel.summarize_watch(rows, {"t2_judge": "model-new"}, recent=REC, end=END)["stages"][0]
    assert s["drift_models"] == ["model-new"] and s["drift_pct"] == 25.0 and s["off_primary_pct"] == 75.0
    assert _checks(rows) == ["qf_drift.t2_judge"]
    # the same model served 5 % of the week before: no drift
    assert _checks([_w("t2_judge", "model-old", "prior", 95), _w("t2_judge", "model-new", "prior", 5),
                    _w("t2_judge", "model-old", "recent", 30), _w("t2_judge", "model-new", "recent", 10)]) == []
    # no prior traffic: drift is unknown, never an alert
    assert _checks([_w("t2_judge", "model-new", "recent", 40)]) == []


def test_every_finding_has_a_registered_ops_check():
    rows = [_w("t9_write", "m1", "prior", 100), _w("t9_write", "m2", "recent", 40, fb=20, cut=10)]
    found = funnel.watch_findings(funnel.summarize_watch(rows, {}, recent=REC, end=END))
    assert {f.check for f in found} == {"qf_cut.t9_write", "qf_fb.t9_write", "qf_drift.t9_write"}
    for f in found:
        chk = ops_checks.check_for(f.check)
        assert chk.bell_every == timedelta(hours=20)
        assert len(ops_checks.EVENT_TYPE_PREFIX + f.check) <= 100
        subject, text = ops_checks.render(f, environment="test")
        assert subject.isascii() and f.summary in text


def test_the_daily_checks_bell_every_20_hours_like_the_route_digest():
    """Review S271: a daily job whose start drifts; a 24 h window drops the bell of a run a little earlier."""
    for name in ("qf_cut.t9_write", "qf_fb.t9_write", "qf_drift.t9_write", ops_checks.QF_DIGEST_CHECK):
        assert ops_checks.check_for(name).bell_every == timedelta(hours=20)
    assert ops_checks.check_for(ops_checks.QF_DIGEST_CHECK).bell_every == \
        ops_checks.check_for(ops_checks.ROUTE_DIGEST_CHECK).bell_every


def _found(rows, policies=None):
    return sorted(f.check for f in funnel.watch_findings(funnel.summarize_watch(
        rows, {}, recent=REC, end=END, policies=policies)))


def test_the_stage_route_policy_sets_the_fallback_threshold_and_can_raise_the_minimum():
    rows = [_w("t10_judge", "model-b", "recent", 40, fb=8)]                                       # 20 %
    assert _found(rows) == ["qf_fb.t10_judge"]
    # the stage's own row: alert from 50 % only
    assert _found(rows, {"t10_judge": {"stage": "t10_judge", "fallback_alert_pct": 50}}) == []
    # the * row applies when the stage has none; a NULL field of the stage row falls through to it
    assert _found(rows, {"*": {"stage": "*", "fallback_alert_pct": 10},
                         "t10_judge": {"stage": "t10_judge", "fallback_alert_pct": None}}) == ["qf_fb.t10_judge"]
    assert _found([_w("t10_judge", "model-b", "recent", 40, fb=5)],
                  {"*": {"stage": "*", "fallback_alert_pct": 10}}) == ["qf_fb.t10_judge"]           # 12.5 %
    # min_requests above the weekly floor wins (for cut and drift too); below it never lowers the floor
    assert _found(rows, {"t10_judge": {"stage": "t10_judge", "min_requests": 100}}) == []
    assert _found([_w("t9_write", "m", "recent", 40, cut=4)], {"*": {"stage": "*", "min_requests": 100}}) == []
    assert _found([_w("t10_judge", "model-b", "recent", 10, fb=5)],
                  {"t10_judge": {"stage": "t10_judge", "min_requests": 5}}) == []
    s = funnel.summarize_watch(rows, {}, recent=REC, end=END,
                               policies={"t10_judge": {"stage": "t10_judge", "min_requests": 5}})["stages"][0]
    assert s["policy"] == {"fallback_alert_pct": 20.0, "min_requests": funnel.MIN_REQUESTS, "policy_min_requests": 5}


def test_the_watch_reads_the_route_policy_table():
    from shared.llm_client import route_watch

    seen = []

    async def fake_part(conn, un, name, sql, *args):
        seen.append((name, sql))
        if name == "route_policy":
            return [{"stage": "t10_judge", "fallback_alert_pct": 50, "min_requests": None}]
        if name == "watch":
            return [_w("t10_judge", "model-b", "recent", 40, fb=8)]
        return []
    orig, funnel._part = funnel._part, fake_part
    try:
        w = asyncio.run(funnel.watch_stats(None, DAY, {}))
    finally:
        funnel._part = orig
    assert ("route_policy", route_watch.POLICY_SQL) in seen
    assert w["stages"][0]["policy"]["fallback_alert_pct"] == 50.0 and funnel.watch_findings(w) == []


def test_route_test_rows_stay_out_of_every_ledger_read():
    """Review S271: AI gateway > Trace route tests are 16-token real requests: every one 'cut', no content."""
    assert funnel.NOT_ROUTE_TEST == "coalesce(quality_signal ->> 'route_test', '') <> 'true'"
    for sql in (funnel.STAGES_SQL, funnel.WATCH_SQL, funnel.T9_COST_SQL):
        assert funnel.NOT_ROUTE_TEST in sql


def test_stage_refused_rows_leave_out_the_expected_refusals():
    from shared.llm_client.expected_refusal import EXPECTED_SQL

    assert EXPECTED_SQL in funnel.STAGES_SQL
    assert "status = 'refused' AND NOT expected) AS refused" in funnel.STAGES_SQL
    assert "FILTER (WHERE expected) AS expected_refusals" in funnel.STAGES_SQL


def _f(check, **details):
    return {"check": check, "summary": check + " summary", "details": details}


def test_a_finding_already_present_in_the_previous_run_is_a_repeat():
    now = [_f("qf_cut.t9_write"), _f("qf_fb.t10_judge"), _f("qf_drift.t2_judge", models=["m-new"])]
    prev = [_f("qf_cut.t9_write"), _f("qf_drift.t2_judge", models=["m-new", "m-x"])]
    marked = {f["check"]: f["repeat"] for f in funnel.mark_repeats(now, prev)}
    assert marked == {"qf_cut.t9_write": True, "qf_fb.t10_judge": False, "qf_drift.t2_judge": True}
    # a drift to a model that was not drifting the night before is a new transition
    assert funnel.mark_repeats([_f("qf_drift.t2_judge", models=["m-other"])], prev)[0]["repeat"] is False
    # no earlier scheduled run in reach: everything is new
    assert [f["repeat"] for f in funnel.mark_repeats(now, None)] == [False, False, False]


def test_one_digest_for_the_night_with_the_new_findings_only():
    """An outage over several stages is one alert, and what stays in the trailing window is not raised again."""
    found = funnel.mark_repeats([_f("qf_fb.t9_write"), _f("qf_fb.t10_judge"), _f("qf_fb.s1_generate"),
                                 _f("qf_cut.t9_write")], [_f("qf_cut.t9_write")])
    d = funnel.digest_finding(found, day="2026-11-05")
    assert d.check == ops_checks.QF_DIGEST_CHECK
    assert d.details["new"] == ["qf_fb.t9_write", "qf_fb.t10_judge", "qf_fb.s1_generate"]
    assert d.details["still_open"] == ["qf_cut.t9_write"]
    assert "3 new quality funnel alert(s) for 2026-11-05" in d.summary and "1 still open" in d.summary
    assert funnel.digest_finding(funnel.mark_repeats([_f("qf_cut.t9_write")], [_f("qf_cut.t9_write")])) is None
    subject, text = ops_checks.render(d, environment="test")
    assert subject.isascii() and len(ops_checks.EVENT_TYPE_PREFIX + d.check) <= 100


# ── CIS-08 report ─────────────────────────────────────────────────────────────────────────────────────────────────


def _snap(watch_rows=()):
    t9 = [funnel._object_row({"week": "2026-11-02", "channel": "blog", "created": 3, "approved": 1, "held": 1,
                              "failed": 1, "processing": 0, "first_pass": 1, "attempts_delivered": 4,
                              "attempts_approved": 1, "attempts_held": 3, "degraded": 0}, 0.4)]
    return {"version": 1, "day": DAY.isoformat(),
            "window": {"first_day": "2026-09-14", "last_day": DAY.isoformat(), "weeks": 8, "week_start": "x"},
            "workflows": {"t9": {"by_week_channel": t9, "unattributed_cost_usd": {"2026-11-02": 0.2},
                                 "gate_fails": [{"week": "2026-11-02", "channel": "blog", "attempt": 1,
                                                 "gate": funnel.F9_JUDGE_CLASS, "blocking": True, "fires": 2,
                                                 "pieces": 2}],
                                 "first_gate_holds": []},
                          "s1": {"by_week": [], "gate_fails": []}, "t2": {"by_week": [], "gate_fails": None}},
            "stages": [], "thresholds": funnel.THRESHOLDS, "unavailable": {"t2_objects": "UndefinedTableError: x"},
            "watch": funnel.summarize_watch(list(watch_rows), {}, recent=REC, end=END),
            "totals": funnel.totals(t9, [], [], last_week="2026-11-02", t9_unattributed={"2026-11-02": 0.2})}


def test_cis08_is_registered_daily_and_rendered_here():
    report = load().report(funnel_report.REPORT_ID)
    assert report is not None and report.runnable and report.schedule == "daily"
    assert generate.RENDERERS[funnel_report.REPORT_ID] is generate._funnel
    assert funnel_report.REPORT_ID in watch_job.WATCHED_REPORTS


def test_build_stores_the_snapshot_with_its_findings():
    report = load().report("CIS-08")
    out = funnel_report.build(report, _snap([_w("t9_write", "m", "recent", 40, cut=4)]), NOW)
    assert out.output["report_id"] == "CIS-08" and out.output["day"] == DAY.isoformat()
    assert [(f["check"], f["repeat"]) for f in out.output["watch"]["findings"]] == [("qf_cut.t9_write", False)]
    assert out.summary.startswith("T9 2 delivered of 3 created")
    assert "1 new watchdog alert(s), 0 still open" in out.summary
    assert "0.3000 USD per delivered" in out.summary          # (0.4 attributed + 0.2 unattributed) / 2
    assert "Attributed USD / delivered" in out.markdown and "| 2026-11-02 | 0.2000 USD |" in out.markdown
    assert "## T9 failing gates, every attempt (blocking)" in out.markdown
    assert funnel.F9_JUDGE_CLASS in out.markdown and "Sources not readable here" in out.markdown
    json.dumps(out.output)       # JSON-safe as stored


def test_build_flags_what_the_previous_run_already_had():
    report = load().report("CIS-08")
    snap = _snap([_w("t9_write", "m", "recent", 40, cut=4, fb=10)])
    out = funnel_report.build(report, snap, NOW, previous=[{"check": "qf_cut.t9_write", "summary": "s"}])
    assert {f["check"]: f["repeat"] for f in out.output["watch"]["findings"]} == {"qf_cut.t9_write": True,
                                                                                 "qf_fb.t9_write": False}
    assert "1 new watchdog alert(s), 1 still open" in out.summary
    assert "(still open from an earlier night, not raised again)" in out.markdown


def test_render_computes_the_period_end_day(monkeypatch):
    seen = {}

    async def fake(conn, day, now=None):
        seen["day"] = day
        return _snap()
    monkeypatch.setattr(funnel, "compute", fake)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    out = asyncio.run(funnel_report.render(conn, load(), load().report("CIS-08"), DAY, DAY, NOW))
    assert seen["day"] == DAY and out.output["watch"]["findings"] == []
    assert out.output["watch"]["compared_with"] is None


def test_render_compares_with_the_previous_scheduled_run(monkeypatch):
    async def fake(conn, day, now=None):
        return _snap([_w("t9_write", "m", "recent", 40, cut=4)])
    monkeypatch.setattr(funnel, "compute", fake)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": "p1", "period_end": DAY - timedelta(days=1),
                                            "findings": json.dumps([{"check": "qf_cut.t9_write"}])})
    out = asyncio.run(funnel_report.render(conn, load(), load().report("CIS-08"), DAY, DAY, NOW))
    sql, *args = conn.fetchrow.await_args.args
    assert sql is funnel_report.PREVIOUS_SQL and args == ["CIS-08", DAY, DAY - timedelta(days=funnel.WATCH_DAYS)]
    assert "trigger = 'schedule'" in sql and "period_end < $2" in sql
    assert out.output["watch"]["findings"][0]["repeat"] is True
    assert out.output["watch"]["compared_with"] == {"run_id": "p1", "day": (DAY - timedelta(days=1)).isoformat()}


# ── the watchdog job ──────────────────────────────────────────────────────────────────────────────────────────────


def _ctx(payload, row):
    ctx = MagicMock()
    ctx.payload, ctx.job_id = payload, "j"
    ctx.pool = MagicMock()
    ctx.pool.fetchrow = AsyncMock(return_value=row)
    return ctx


def _row(output, status="succeeded", report_id="CIS-08"):
    return {"report_id": report_id, "status": status, "period_end": DAY, "output": json.dumps(output)}


def test_watch_job_raises_the_new_stored_findings_as_one_digest(monkeypatch):
    alert = AsyncMock(return_value={"qf_digest": {"bell": True, "email": "sent"}})
    monkeypatch.setattr(watch_job.ops_checks, "alert_all", alert)
    out_ = {"watch": {"findings": [{"check": "qf_fb.t9_write", "summary": "s", "details": {"a": 1}},
                                   {"check": "qf_fb.t10_judge", "summary": "s2", "details": {}},
                                   {"check": "qf_cut.t9_write", "summary": "c", "repeat": True},
                                   {"check": "renamed.check", "summary": "x"}]}}
    out = asyncio.run(watch_job.run(_ctx({"run_id": "r1"}, _row(out_))))
    (pool, findings), _ = alert.await_args
    assert [f.check for f in findings] == ["qf_digest"]
    assert findings[0].details["new"] == ["qf_fb.t9_write", "qf_fb.t10_judge"]
    assert findings[0].details["findings"][0]["details"] == {"a": 1}
    assert out["raised"] == ["qf_fb.t9_write", "qf_fb.t10_judge"] and out["day"] == DAY.isoformat()
    assert [f["repeat"] for f in out["findings"]] == [False, False, True]
    assert out["alerts"]["qf_digest"]["bell"] is True


def test_watch_job_raises_nothing_when_every_finding_is_a_repeat(monkeypatch):
    alert = AsyncMock()
    monkeypatch.setattr(watch_job.ops_checks, "alert_all", alert)
    out_ = {"watch": {"findings": [{"check": "qf_cut.t9_write", "summary": "c", "repeat": True}]}}
    out = asyncio.run(watch_job.run(_ctx({"run_id": "r1"}, _row(out_))))
    assert alert.await_count == 0 and out["raised"] == [] and out["findings"][0]["repeat"] is True


@pytest.mark.parametrize("result", [{"bell": False, "email": "failed"}, {"error": "PostgresError: x"}])
def test_watch_job_retries_when_the_alert_was_not_delivered(monkeypatch, result):
    """Review S271: a failed SNS publish lost the night (the bell was removed, the job succeeded). Now the job raises
    a retryable error; the bell is deduplicated, so the retry is safe."""
    monkeypatch.setattr(watch_job.ops_checks, "alert_all", AsyncMock(return_value={"qf_digest": result}))
    out_ = {"watch": {"findings": [{"check": "qf_fb.t9_write", "summary": "s"}]}}
    with pytest.raises(watch_job.AlertNotDelivered) as e:
        asyncio.run(watch_job.run(_ctx({"run_id": "r1"}, _row(out_))))
    assert not isinstance(e.value, watch_job.NonRetryable)


def test_watch_job_dry_run_and_no_findings_raise_nothing(monkeypatch):
    alert = AsyncMock()
    monkeypatch.setattr(watch_job.ops_checks, "alert_all", alert)
    out_ = {"watch": {"findings": [{"check": "qf_cut.t9_write", "summary": "s"}]}}
    out = asyncio.run(watch_job.run(_ctx({"run_id": "r1", "dry_run": True}, _row(out_))))
    assert out["findings"][0]["check"] == "qf_cut.t9_write" and out["alerts"] == {}
    asyncio.run(watch_job.run(_ctx({"run_id": "r1"}, _row({"watch": {"findings": []}}))))
    assert alert.await_count == 0


@pytest.mark.parametrize("payload,row", [({}, None), ({"run_id": "r"}, None),
                                         ({"run_id": "r"}, _row({}, status="failed")),
                                         ({"run_id": "r"}, _row({}, report_id="EX-04"))])
def test_watch_job_refuses(payload, row):
    with pytest.raises(watch_job.NonRetryable):
        asyncio.run(watch_job.run(_ctx(payload, row)))


def test_watch_kind_is_background_cheap_and_not_deploy_blocking():
    from shared.jobs.registry import kinds

    k = kinds()[watch_job.KIND]
    assert k.background and k.concurrency == 1 and k.deploy_gate_exempt


@pytest.mark.parametrize("report_id,trigger,watched", [("CIS-08", "schedule", True), ("CIS-08", "manual", False),
                                                       ("EX-04", "schedule", False)])
def test_report_generate_queues_the_watch_for_scheduled_cis08_only(monkeypatch, report_id, trigger, watched):
    run = {"id": "r1", "report_id": report_id, "status": "queued", "trigger": trigger,
           "period_start": DAY, "period_end": DAY}
    monkeypatch.setattr(report_jobs.store, "get_run", AsyncMock(return_value=run))
    monkeypatch.setattr(report_jobs.store, "mark_running", AsyncMock())
    monkeypatch.setattr(report_jobs.store, "finish_ok", AsyncMock())
    monkeypatch.setattr(report_jobs.generate, "render", AsyncMock(return_value=generate.Rendered("s", {}, "# md")))
    monkeypatch.setattr(report_ops_jobs, "enqueue_report_email", AsyncMock())
    enq = AsyncMock(return_value={"job_id": "w1", "created": True})
    monkeypatch.setattr(watch_job, "enqueue_for_run", enq)
    pool = MagicMock()

    @asynccontextmanager
    async def acq():
        yield MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: acq())
    ctx = MagicMock()
    ctx.payload, ctx.pool, ctx.job_id = {"run_id": "r1"}, pool, "j"
    out = asyncio.run(report_jobs.run_report(ctx))
    assert enq.await_count == (1 if watched else 0) and ("watch" in out) is watched
    if watched:
        assert enq.await_args.args[1] == "r1" and enq.await_args.kwargs == {"parent_job_id": "j"}


def test_a_failed_watch_enqueue_does_not_fail_the_report(monkeypatch):
    run = {"id": "r1", "report_id": "CIS-08", "status": "queued", "trigger": "schedule",
           "period_start": DAY, "period_end": DAY}
    monkeypatch.setattr(report_jobs.store, "get_run", AsyncMock(return_value=run))
    monkeypatch.setattr(report_jobs.store, "mark_running", AsyncMock())
    monkeypatch.setattr(report_jobs.store, "finish_ok", AsyncMock())
    monkeypatch.setattr(report_jobs.generate, "render", AsyncMock(return_value=generate.Rendered("s", {}, "# md")))
    monkeypatch.setattr(watch_job, "enqueue_for_run", AsyncMock(side_effect=RuntimeError("db")))
    pool = MagicMock()

    @asynccontextmanager
    async def acq():
        yield MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: acq())
    ctx = MagicMock()
    ctx.payload, ctx.pool, ctx.job_id = {"run_id": "r1"}, pool, "j"
    assert "RuntimeError" in asyncio.run(report_jobs.run_report(ctx))["watch"]["error"]


def test_enqueue_for_run_is_idempotent_per_run(monkeypatch):
    enq = AsyncMock(return_value=("w1", True))
    monkeypatch.setattr(watch_job, "enqueue", enq)
    assert asyncio.run(watch_job.enqueue_for_run("pool", "r9", parent_job_id="p")) == {"job_id": "w1",
                                                                                       "created": True}
    assert enq.await_args.kwargs["idempotency_key"] == "quality_funnel_watch:r9"


# ── GET /admin/quality/funnel ─────────────────────────────────────────────────────────────────────────────────────


def _app(latest, history, manual=None):
    from fastapi import FastAPI

    from api.routers import admin_quality_funnel as r

    conn = MagicMock()

    async def fetchrow(sql, *args):
        from services.reports import funnel_report as fr
        return manual if sql is fr.LATEST_MANUAL_SQL else latest
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(return_value=history)
    pool = MagicMock()

    @asynccontextmanager
    async def acq():
        yield conn
    pool.acquire = MagicMock(side_effect=lambda: acq())
    app = FastAPI()
    app.include_router(r.router)
    app.state.pool = pool
    from api.core.staff_authz import staff_policy
    app.dependency_overrides[staff_policy] = lambda: None
    return app, conn


def test_funnel_endpoint_serves_the_stored_snapshot():
    from fastapi.testclient import TestClient

    latest = {"id": "11111111-1111-1111-1111-111111111111", "period_start": DAY, "period_end": DAY,
              "trigger": "schedule", "finished_at": NOW, "summary": "T9 ...", "output": json.dumps({"day": "d"})}
    hist = [{"id": latest["id"], "period_end": DAY, "finished_at": NOW, "summary": "T9 ...",
             "totals": json.dumps({"t9": {}}), "findings": 2, "new_findings": 1}]
    manual = {**latest, "id": "22222222-2222-2222-2222-222222222222", "trigger": "manual"}
    app, conn = _app(latest, hist, manual)
    body = TestClient(app).get("/admin/quality/funnel?history=5").json()
    assert body["report_id"] == "CIS-08" and body["snapshot"] == {"day": "d"}
    assert body["run"]["day"] == DAY.isoformat() and body["run"]["trigger"] == "schedule"
    assert body["history"][0]["findings"] == 2 and body["history"][0]["new_findings"] == 1
    assert body["manual_run"]["id"] == manual["id"] and "output" not in body["manual_run"]
    assert conn.fetch.await_args.args[1:] == ("CIS-08", 5)


def test_the_funnel_page_reads_the_scheduled_runs_the_watchdog_alerted_on():
    """Review S271: a manual "Generate now" run is never alerted on, so it must not replace the snapshot."""
    for sql in (funnel_report.LATEST_SQL, funnel_report.HISTORY_SQL, funnel_report.PREVIOUS_SQL):
        assert "trigger = 'schedule'" in sql
    assert "trigger <> 'schedule'" in funnel_report.LATEST_MANUAL_SQL


def test_funnel_endpoint_without_a_snapshot_yet():
    from fastapi.testclient import TestClient

    app, _ = _app(None, [])
    body = TestClient(app).get("/admin/quality/funnel").json()
    assert body == {"report_id": "CIS-08", "run": None, "snapshot": None, "history": [], "manual_run": None}
    assert TestClient(app).get("/admin/quality/funnel?history=0").status_code == 422


def test_funnel_route_has_a_staff_policy():
    from api.core import staff_authz

    assert ("GET", "/admin/quality/funnel") in staff_authz.POLICY
