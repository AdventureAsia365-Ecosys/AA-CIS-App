"""AA-1003 R4 — catalog, AI-quality, reliability and data-quality reports (no DB, no AWS, no paid call).

Reports: CIS-01, CIS-06, CIS-07 (services/reports/quality.py); OPS-13, OPS-14, DATA-02, DATA-03, DATA-04
(services/reports/reliability.py). Each renderer on fixture facts lists every KPI its registry entry names, a part
that could not be read is a line in the report (never an exception), and the computations follow the kpis.yaml
definitions.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from services.reports import generate, periods, quality, registry, reliability

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
MON, SUN = date(2026, 10, 26), date(2026, 11, 1)
R4 = ("CIS-01", "CIS-06", "CIS-07", "OPS-13", "OPS-14", "DATA-02", "DATA-03", "DATA-04")
EXPECTED = {"s1_rewrite": 900, "kpi_snapshot_daily": 300, "report_generate": 120, "retention_job": 600}


def _reg():
    return registry.load()


def _kpis(out) -> dict:
    return {k["id"]: k for k in out.output["kpis"]}


def _ts(day: date, hour: int = 3) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


# ── registry, periods, dispatch ─────────────────────────────────────────────────────────────────────────────────

def test_r4_reports_are_generated_here_with_their_schedules():
    reg = _reg()
    for rid in R4:
        r = reg.report(rid)
        assert r.runnable and rid in generate.RENDERERS, rid
    assert {reg.report(r).schedule for r in ("CIS-01", "OPS-13", "OPS-14", "DATA-02")} == {"weekly"}
    assert {reg.report(r).schedule for r in ("CIS-06", "CIS-07", "DATA-03")} == {"event"}
    assert reg.report("DATA-04").schedule == "monthly"


def test_event_reports_default_to_the_last_seven_closed_days():
    now = datetime(2026, 10, 30, 3, tzinfo=UTC)                     # ICT 30/10 10:00: closed day 29/10
    assert periods.default_period("event", now) == (date(2026, 10, 23), date(2026, 10, 29))
    assert periods.default_period("daily", now) == (periods.snapshot_day(now),) * 2
    reg = _reg()
    assert generate.due_state(reg.report("CIS-07"), None, now)["state"] == "manual"


@pytest.mark.parametrize("rid,module", [("CIS-01", "quality"), ("CIS-06", "quality"), ("CIS-07", "quality"),
                                        ("OPS-13", "reliability"), ("OPS-14", "reliability"),
                                        ("DATA-02", "reliability"), ("DATA-03", "reliability"),
                                        ("DATA-04", "reliability")])
def test_generate_dispatches_to_the_r4_modules(monkeypatch, rid, module):
    mod = quality if module == "quality" else reliability
    seen = []

    async def fake(conn, reg, report, first, last, now):
        seen.append(report.id)
        return generate.Rendered("s", {}, "m")
    monkeypatch.setattr(mod, "render", fake)
    reg = _reg()
    asyncio.run(generate.render(None, reg, reg.report(rid), MON, SUN, NOW))
    assert seen == [rid]


def test_r4_sql_names_only_reference_tables():
    from pathlib import Path

    ref = Path(__file__).resolve().parents[2].parent.parent / "docs" / "architecture" / "db-schema-reference.md"
    if not ref.exists():
        pytest.skip("root repo not checked out next to this one")
    text = ref.read_text(encoding="utf-8")
    for name in ("gold_aa_internal.tour_lifecycle_event", "gold_aa_internal.v_tour_liveness", "shared.v_tour_photos",
                 "silver_aa_internal.generated_content", "shared.prompt_eval_runs", "shared.writing_memory_release",
                 "shared.eval_item_result", "shared.rerun_wave", "shared.job", "shared.schema_versions",
                 "booking.v_ops_inquiry_inbox", "tripplanner.v_ops_inquiry_keys", "shared.report_run",
                 "shared.llm_call_log", "shared.decision_log", "shared.cost_explorer_snapshot",
                 "shared.tenant_api_usage"):
        assert f"### `{name}`" in text, name


# ── CIS-01 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _catalog():
    return [{"country": "Vietnam", "live": 100, "current_writer": 95, "kept_old_writer": 5, "with_photo": 98,
             "photo_depth": 90, "writer_version": "v2"},
            {"country": "Japan", "live": 50, "current_writer": 30, "kept_old_writer": 0, "with_photo": 40,
             "photo_depth": 20, "writer_version": "v2"}]


def _cis01_facts():
    return {
        "catalog": _catalog(),
        "liveness_now": {"served": 150, "paused": 2, "retired": 9, "as_of": NOW.isoformat()},
        "tours_live": [{"kpi_id": "K-CAT-LIVE", "day": SUN, "scope": "all", "value": 150},
                       {"kpi_id": "K-CAT-LIVE", "day": MON - timedelta(days=1), "scope": "all", "value": 152}],
        "lifecycle": {"counts": [{"event": "retired", "reason": "source_deleted", "n": 2},
                                 {"event": "superseded", "reason": "replaced", "n": 1}],
                      "review": [{"event": "retired", "reason": "source_deleted", "source_id": "S-1", "tour_id": "t1",
                                  "replaced_by": None, "occurred_at": "2026-10-28T02:00:00+00:00", "name": "Tour A",
                                  "country": "Japan"},
                                 {"event": "superseded", "reason": "replaced", "source_id": "S-2", "tour_id": "t2",
                                  "replaced_by": "t3", "occurred_at": "2026-10-29T02:00:00+00:00", "name": "Tour B",
                                  "country": "Vietnam"}]},
        "data_kpis": [{"kpi_id": "K-DATA-STALE", "day": SUN, "scope": "all", "value": 4},
                      {"kpi_id": "K-DATA-LIVENESS", "day": SUN, "scope": "all", "value": 0}],
        "published": {"total": 12, "first_published": 1, "republished": 11},
    }


def test_cis01_kpis_and_actions():
    reg = _reg()
    out = quality.render_cis01(reg, reg.report("CIS-01"), MON, SUN, _cis01_facts(), NOW)
    kp = _kpis(out)
    assert set(kp) == set(reg.report("CIS-01").kpis)
    assert kp["K-CAT-LIVE"]["value"] == 150 and kp["K-CAT-LIVE"]["status"] == "ok"      # -1.3 %: not a > 2 % drop
    assert kp["K-CAT-COVER"]["value"] == pytest.approx(138 / 150, abs=1e-4)
    assert kp["K-CAT-COVER"]["status"] == "amber"                                         # 92 %: < 95, >= 90
    assert kp["K-CAT-PHOTO"]["value"] == pytest.approx(110 / 150, abs=1e-4) and kp["K-CAT-PHOTO"]["status"] == "amber"
    assert kp["K-CAT-S1"]["value"] == pytest.approx(125 / 150, abs=1e-4) and "Japan" in kp["K-CAT-S1"]["note"]
    assert kp["K-CAT-LIFECYCLE"]["status"] == "amber" and kp["K-CAT-LIFECYCLE"]["value"] == 3
    actions = out.output["actions"]
    assert actions[0].startswith("Downstream freshness: 4")
    assert any(a.startswith("Review 1 retirement") for a in actions)
    assert any(a.startswith("Japan: rewrite 20 tour(s)") for a in actions)
    assert not any(a.startswith("Vietnam: rewrite") for a in actions)                    # 5 kept on purpose
    assert len(actions) <= quality.ACTIONS_MAX
    md = out.markdown
    assert "## Lifecycle events this week" in md and "Tour A" in md and "## Top actions" in md
    assert "Tours published or republished this week: 12" in md


def test_cis01_isolates_unreadable_parts():
    reg = _reg()
    facts = {k: {"error": "UndefinedTableError: x"} for k in _cis01_facts()}
    out = quality.render_cis01(reg, reg.report("CIS-01"), MON, SUN, facts, NOW)
    kp = _kpis(out)
    assert all(k["status"] == "no_data" for k in kp.values())
    assert set(out.output["sources_unreadable"]) == set(facts)
    assert "Not readable" in out.markdown


# ── CIS-06 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _run(**kw):
    base = {"id": "11111111-aaaa", "pipeline": "writing_memory", "run_kind": "r1", "writer": "a1",
            "prompt_version": None, "release_id": "22222222-bbbb", "baseline_prompt_version": None,
            "avg_quality_score": 8.1, "baseline_avg_quality_score": 8.0, "gate_pass_count": 18, "gate_fail_count": 2,
            "cost_usd": 1.2, "cost_cap_usd": 2.0, "verdict": "pass", "stop_reason": "completed", "judge_kappa": 0.7,
            "regression_detected": False, "tour_count": 20, "triggered_by": "x",
            "created_at": "2026-10-27T01:00:00+00:00", "finished_at": "2026-10-27T02:00:00+00:00"}
    return {**base, **kw}


def test_cis06_delta_gate_and_cost_cap():
    reg = _reg()
    facts = {"eval": {"runs": [_run(), _run(id="33333333-cccc", avg_quality_score=7.5, cost_usd=2.5,
                                    verdict="fail")],
                      "releases": [{"release_id": "22222222-bbbb", "writer": "a1", "scope": "platform",
                                    "release_no": 3, "train_kind": "regular", "content_class": "rules",
                                    "status": "approved", "eval_run_id": "11111111-aaaa", "r0_run_id": None,
                                    "shadow_run_id": None, "approved_by": "lead", "approved_at": "2026-10-28T00:00",
                                    "approval_ref": None, "activated_at": None, "retired_at": None,
                                    "rejected_by": None, "created_by": "x", "created_at": "x", "updated_at": "x"}],
                      "items": [{"run_id": "33333333-cccc", "item_id": "44444444-dddd", "writer": "a1",
                                 "arm": "candidate", "phase": "r1", "verdict": "fail", "score": 3.0,
                                 "failure_codes": ["fact_mismatch"], "hard_stop": True}]}}
    out = quality.render_cis06(reg, reg.report("CIS-06"), MON, SUN, facts, NOW)
    kp = _kpis(out)
    assert set(kp) == set(reg.report("CIS-06").kpis)
    assert kp["K-EVAL-DELTA"]["value"] == pytest.approx(-0.5) and kp["K-EVAL-DELTA"]["status"] == "amber"
    assert kp["K-EVAL-GATEPASS"]["value"] == pytest.approx(0.9)
    assert kp["K-EVAL-COST"]["status"] == "red" and kp["K-EVAL-COST"]["value"] == pytest.approx(3.7)
    assert "fact_mismatch" in out.markdown and "## Releases and approval record" in out.markdown
    assert "2 run(s)" in out.summary


def test_cis06_with_no_runs_says_so():
    reg = _reg()
    out = quality.render_cis06(reg, reg.report("CIS-06"), MON, SUN, {"eval": {"runs": [], "releases": [],
                                                                              "items": []}}, NOW)
    assert all(k["status"] == "no_data" for k in out.output["kpis"])
    assert "No eval run finished in the period." in out.markdown


# ── CIS-07 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _wave(name="japan-w2", status="complete", closed=True, running=0, not_terminal=None, failed_cost=0.5):
    states = {"master": 9, "review_pending": 1, "trashed": 0, "failed": 0, "interrupted": 0, "running": running,
              "unclassified": 0, "not_started": 0}
    return {"report": {"wave": {"id": "w1", "name": name, "country": "Japan", "status": status,
                                "created_at": "2026-10-27T00:00:00+00:00",
                                "closed_at": "2026-10-28T00:00:00+00:00" if closed else None},
                       "expected": 10, "columns": {"s1_ok": 10, "to_master": 9, "to_review": 1, "atomized": 9},
                       "states": states, "terminal": 10 - running, "jobs_finished": not running,
                       "complete": not running, "jev_coverage": {"tours": 9, "expected": 10, "pct": 90.0},
                       "cost": {"llm_usd": 4.0, "llm_by_provider": [], "dfs_usd": 0.5, "dfs_calls": 3,
                                "total_usd": 4.5}, "jobs": []},
            "not_terminal": not_terminal or [],
            "stages": [{"stage": "s1_generate", "calls": 30, "cost_usd": 3.0, "failed_cost_usd": failed_cost},
                       {"stage": "judge", "calls": 10, "cost_usd": 1.0, "failed_cost_usd": 0.0}]}


def test_cis07_cost_unit_retry_and_projection():
    reg = _reg()
    facts = {"waves": {"waves": [_wave()], "capped": False}, "catalog": _catalog()}
    out = quality.render_cis07(reg, reg.report("CIS-07"), MON, SUN, facts, NOW)
    kp = _kpis(out)
    assert set(kp) == set(reg.report("CIS-07").kpis)
    assert kp["K-WAVE-DONE"]["value"] == 1.0 and kp["K-WAVE-DONE"]["status"] == "ok"
    assert kp["K-WAVE-UNIT"]["value"] == pytest.approx(0.5)                      # 4.5 USD / 9 to Master
    assert kp["K-WAVE-RETRY"]["value"] == pytest.approx(0.125) and kp["K-WAVE-RETRY"]["status"] == "amber"
    assert kp["K-WAVE-STUCK"]["value"] == 0 and kp["K-WAVE-RUNNING"]["status"] == quality.NOT_WIRED
    w = out.output["waves"][0]
    assert w["projection_usd"] == pytest.approx(25.0) and w["country_live"] == 50  # 0.5 x 50 live Japan tours
    assert "fix before the next wave: retry waste" in out.markdown


def test_cis07_stuck_and_running_waves():
    reg = _reg()
    stuck = _wave(status="incomplete", not_terminal=[{"tour_id": "t9", "name": "Tour Z", "country": "Japan",
                                                      "state": "unclassified"}], failed_cost=0.0)
    running = _wave(name="w3", status="open", closed=False, running=2, failed_cost=0.0)
    out = quality.render_cis07(reg, reg.report("CIS-07"), MON, SUN,
                               {"waves": {"waves": [stuck, running], "capped": False}, "catalog": {"error": "x"}}, NOW)
    kp = _kpis(out)
    assert kp["K-WAVE-STUCK"]["value"] == 1 and kp["K-WAVE-STUCK"]["status"] == "red"   # running tours are not stuck
    assert "still running" in out.markdown and "Tour Z (unclassified)" in out.markdown
    none = quality.render_cis07(reg, reg.report("CIS-07"), MON, SUN, {"waves": {"waves": [], "capped": False},
                                                                      "catalog": []}, NOW)
    assert "No rerun wave was open during the period." in none.markdown


# ── OPS-14 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _job_facts():
    return {
        "job_kinds": [
            {"kind": "s1_rewrite", "created": 40, "finished": 40, "failed": 2, "stopped": 0, "retried": 3,
             "p95_s": 1200.0, "queue_p95_s": 30.0, "cost_usd": 8.0},
            {"kind": "kpi_snapshot_daily", "created": 7, "finished": 7, "failed": 0, "stopped": 0, "retried": 0,
             "p95_s": 20.0, "queue_p95_s": 2.0, "cost_usd": 0.0}],
        "job_last": [{"kind": "kpi_snapshot_daily", "last_created": NOW.isoformat(), "scheduled": True,
                      "last_scheduled": NOW.isoformat()},
                     {"kind": "retention_job", "last_created": (NOW - timedelta(days=40)).isoformat(),
                      "scheduled": True, "last_scheduled": (NOW - timedelta(days=40)).isoformat()},
                     {"kind": "s1_rewrite", "last_created": NOW.isoformat(), "scheduled": False,
                      "last_scheduled": None}],
        "job_active": [{"kind": "s1_rewrite", "status": "running", "started_at": NOW - timedelta(hours=2),
                        "attempt_started_at": (NOW - timedelta(minutes=40)).isoformat(),
                        "run_after": NOW - timedelta(hours=2), "created_at": NOW - timedelta(hours=2)},
                       {"kind": "report_generate", "status": "queued", "started_at": None, "attempt_started_at": None,
                        "run_after": NOW - timedelta(minutes=5), "created_at": NOW - timedelta(minutes=6)}],
        "queue_kpi": [{"kpi_id": "K-JOB-QUEUEAGE", "day": SUN, "scope": "app:cis", "value": 12},
                      {"kpi_id": "K-JOB-QUEUEAGE", "day": SUN - timedelta(days=1), "scope": "app:cis", "value": 400}],
    }


def test_ops14_rates_overdue_and_dead_kinds():
    reg = _reg()
    out = reliability.render_ops14(reg, reg.report("OPS-14"), MON, SUN, _job_facts(), NOW, expected=EXPECTED)
    kp = _kpis(out)
    assert set(kp) == set(reg.report("OPS-14").kpis)
    assert kp["K-JOB-FAILRATE"]["value"] == pytest.approx(2 / 47, abs=1e-4) and "s1_rewrite" in kp["K-JOB-FAILRATE"][
        "note"]
    assert kp["K-JOB-P95"]["value"] == pytest.approx(1200 / 900, abs=1e-4) and kp["K-JOB-P95"]["status"] == "amber"
    assert kp["K-JOB-QUEUEAGE"]["value"] == 400 and kp["K-JOB-QUEUEAGE"]["status"] == "amber"
    assert kp["K-JOB-DEAD"]["value"] == 1 and "retention_job" in kp["K-JOB-DEAD"]["note"]
    jobs = out.output["jobs"]
    assert [o["kind"] for o in jobs["overdue"]] == ["s1_rewrite"]           # attempt 40 min > 2 x 15 min
    assert jobs["queued"]["report_generate"]["queued"] == 1
    assert "report_generate" in jobs["never_90d"]                           # registered, no row in 90 days
    assert jobs["paid"] == [{"kind": "s1_rewrite", "cost_usd": 8.0, "runs": 40, "per_run": 0.2}]
    assert "booking.job" in out.markdown


def test_ops14_unreadable_job_table():
    reg = _reg()
    facts = {k: {"error": "InsufficientPrivilegeError: x"} for k in _job_facts()}
    out = reliability.render_ops14(reg, reg.report("OPS-14"), MON, SUN, facts, NOW, expected=EXPECTED)
    assert all(k["status"] == "no_data" for k in out.output["kpis"]) and "not readable" in out.summary


# ── OPS-13 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _ops13_facts(prev=None):
    hours = [(NOW - timedelta(hours=i), float(v)) for i, v in enumerate([40] * 160 + [120] * 8)]   # a short peak: p95 stays 40
    return {
        **_job_facts(),
        "statements": {"top_total": [{"role": "cis_app", "calls": 900, "total_ms": 90000.0, "mean_ms": 100.0,
                                      "rows": 9, "query": "SELECT 1 | x"},
                                     {"role": "tripplanner", "calls": 50, "total_ms": 30000.0, "mean_ms": 600.0,
                                      "rows": 5, "query": None}],
                       "top_mean": [], "by_role": [{"role": "cis_app", "statements": 300, "slow": 3, "over_500ms": 1},
                                                   {"role": "tripplanner", "statements": 40, "slow": 2,
                                                    "over_500ms": 1}],
                       "claim": {"mean_ms": 4.2, "calls": 10000}, "stats_reset": "2026-10-16T00:00:00+00:00"},
        "roles": {"roles": [{"role": "cis_app", "conn_limit": 90, "connections": 30},
                            {"role": "booking", "conn_limit": 15, "connections": 13}], "max_connections": 181},
        "rds": {"connections": hours, "calls": 1},
        "previous": prev,
    }


def test_ops13_objectives_slow_statements_and_two_week_misses():
    reg = _reg()
    out = reliability.render_ops13(reg, reg.report("OPS-13"), MON, SUN, _ops13_facts(), NOW, expected=EXPECTED)
    kp = _kpis(out)
    assert set(kp) == set(reg.report("OPS-13").kpis)
    objs = {o["id"]: o for o in out.output["objectives"]}
    assert objs["conn_saturation"]["met"] is True and objs["conn_saturation"]["value"] < 0.6
    assert objs["role_limits"]["met"] is False                              # booking 13 / 15 = 87 %
    assert objs["job_claim"]["met"] is True
    assert objs["tenant_reads"]["met"] is None
    assert kp["K-SLI-MET"]["value"] == 2 and kp["K-SLI-MET"]["status"] == "amber"
    assert kp["K-DB-SLOWSTMT"]["value"] == 5 and kp["K-SLI-2WEEK"]["value"] == 0
    assert "booking 13/15" in kp["K-DB-CONN"]["note"]
    assert out.output["draft"] is True and "text hidden" in out.markdown and "SELECT 1 / x" in out.markdown
    again = reliability.render_ops13(reg, reg.report("OPS-13"), MON, SUN,
                                     _ops13_facts(prev={"objectives": [{"id": "role_limits", "met": False}]}), NOW,
                                     expected=EXPECTED)
    k2 = _kpis(again)
    assert k2["K-SLI-2WEEK"]["value"] == 1 and k2["K-SLI-2WEEK"]["status"] == "red"
    assert "role_limits" in again.markdown


def test_ops13_without_cloudwatch_names_the_reason():
    reg = _reg()
    facts = _ops13_facts()
    facts["rds"] = {"unavailable": "not_permitted: AccessDenied"}
    out = reliability.render_ops13(reg, reg.report("OPS-13"), MON, SUN, facts, NOW, expected=EXPECTED)
    assert _kpis(out)["K-DB-CONN"]["value"] is None and "not_permitted" in _kpis(out)["K-DB-CONN"]["note"]


# ── DATA-02 ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_data02_inbox_unmatched_and_llm_recon():
    reg = _reg()
    facts = {
        "handoff": {"totals": {"sent": 5, "received": 4, "gap": 1, "parked": 0, "failed": 0, "cases": 4},
                    "days": []},
        "inbox": {"status": [{"status": "processed", "n": 4, "unprocessed": 0, "no_case_after_24h": 0}],
                  "unprocessed_age_p95_s": None, "lag_p95_s": 42.0},
        "unmatched": {"unmatched": 1, "keys": 5, "oldest": "2026-10-27T01:00:00+00:00"},
        "aws": [{"day": MON, "account": "acc3", "amount_usd": 10.0}, {"day": MON, "account": "acc2", "amount_usd": 9.0},
                {"day": SUN, "account": "acc3", "amount_usd": 10.0}],
        "llm": [{"day": MON, "account": "acc3", "cost_usd": 9.0}, {"day": SUN, "account": "acc3", "cost_usd": 8.0},
                {"day": MON + timedelta(days=1), "account": "acc3", "cost_usd": 50.0},     # no stored bill that day
                {"day": MON, "account": "acc2", "cost_usd": 1.0}],
    }
    out = reliability.render_data02(reg, reg.report("DATA-02"), MON, SUN, facts, NOW)
    kp = _kpis(out)
    assert set(kp) == set(reg.report("DATA-02").kpis)
    assert kp["K-REC-INBOX"]["value"] == 42.0 and kp["K-REC-INBOX"]["status"] == "ok"
    assert kp["K-REC-UNMATCHED"]["value"] == 1 and kp["K-REC-UNMATCHED"]["status"] == "red"
    assert kp["K-REC-FEEDLAG"]["status"] == reliability.NOT_WIRED
    assert kp["K-LLM-RECON"]["value"] == pytest.approx(0.15) and kp["K-LLM-RECON"]["status"] == "amber"
    assert "flagged" in out.summary and "Sent 5, received 4, gap 1" in out.markdown


def test_data02_contract_views_missing():
    reg = _reg()
    facts = {k: {"error": "UndefinedTableError: x"} for k in ("handoff", "inbox", "unmatched", "aws", "llm")}
    out = reliability.render_data02(reg, reg.report("DATA-02"), MON, SUN, facts, NOW)
    assert "Contract views not readable" in out.markdown
    assert _kpis(out)["K-REC-INBOX"]["status"] == "no_data"


# ── DATA-03 ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version,owner", [("306", "cis"), ("tp-022", "tripplanner"), ("bk-035", "booking"),
                                           ("crm-023", "crm"), ("x", "other")])
def test_owner_of_a_version(version, owner):
    assert reliability.owner_of(version) == owner


def test_data03_counts_migrations_per_owner():
    reg = _reg()
    rows = [{"version": "305", "description": "kpi tables", "applied_at": _ts(MON)},
            {"version": "306", "description": "x", "applied_at": _ts(SUN)},
            {"version": "99", "description": "old", "applied_at": _ts(date(2026, 1, 1))},
            {"version": "bk-035", "description": "inbox view", "applied_at": _ts(MON + timedelta(days=2))},
            {"version": "bk-034", "description": "old", "applied_at": _ts(date(2026, 9, 1))}]
    out = reliability.render_data03(reg, reg.report("DATA-03"), MON, SUN, {"versions": rows}, NOW)
    kp = _kpis(out)
    assert set(kp) == set(reg.report("DATA-03").kpis)
    assert kp["K-SCH-APPLIED"]["value"] == 3 and "booking 1" in kp["K-SCH-APPLIED"]["note"]
    owners = out.output["changes"]["owners"]
    assert owners["cis"]["latest"] == "306" and owners["booking"]["latest"] == "bk-035"
    assert kp["K-SCH-BREAKING"]["status"] == reliability.NOT_WIRED


# ── DATA-04 ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_data04_overdue_runs_growth_and_size_change():
    reg = _reg()
    facts = {
        "overdue": [{"kind": "retention_job", "table": "shared.job", "part": "succeeded", "window_days": 30,
                     "overdue": 120, "oldest": "2026-09-01"},
                    {"kind": "retention_llm_call_log", "table": "shared.llm_call_log", "part": "raw",
                     "window_days": 180, "overdue": 0, "oldest": None}],
        "growth": {"shared.llm_call_log": 900.0, "shared.job": 250.0},
        "sizes": {"ledgers": [{"name": "shared.job", "rows_estimate": 8000, "bytes": 24_000_000},
                              {"name": "booking.job", "rows_estimate": 300, "bytes": 200_000}],
                  "top": [{"name": "shared.llm_call_log", "bytes": 200_000_000, "rows_estimate": 500_000}]},
        "runs": [{"kind": "retention_job", "status": "succeeded", "mode": "live", "created_by": "s",
                  "finished_at": None},
                 {"kind": "retention_job", "status": "failed", "mode": "", "created_by": "s", "finished_at": None}],
        "previous": {"sizes": {"top": [{"name": "shared.llm_call_log", "bytes": 160_000_000}]}},
    }
    out = reliability.render_data04(reg, reg.report("DATA-04"), date(2026, 10, 1), date(2026, 10, 31), facts, NOW)
    kp = _kpis(out)
    assert set(kp) == set(reg.report("DATA-04").kpis)
    assert kp["K-RET-OVERDUE"]["value"] == 120 and kp["K-RET-OVERDUE"]["status"] == "amber"
    assert kp["K-RET-RUNS"]["value"] == 0.5 and kp["K-RET-RUNS"]["status"] == "amber"
    assert kp["K-RET-GROWTH"]["value"] == 1150.0
    assert out.output["top_growth"][0]["growth"] == pytest.approx(0.25)
    assert "not readable from CIS" in out.markdown                          # booking.job row count growth


def test_r4_readers_survive_a_failing_source():
    """Every R4 collector part goes through money_sources.run_parts: one failing reader is an error entry."""
    from unittest.mock import AsyncMock, MagicMock

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("boom"))
    conn.fetchval = AsyncMock(side_effect=RuntimeError("boom"))
    facts = asyncio.run(reliability.collect_jobs(conn, MON, SUN))
    assert set(facts) == {"job_kinds", "job_last", "job_active", "queue_kpi"}
    assert all("error" in v for v in facts.values())
    reg = _reg()
    out = reliability.render_ops14(reg, reg.report("OPS-14"), MON, SUN, facts, NOW, expected=EXPECTED)
    assert out.markdown.startswith("# ")


# ── KPI table units ─────────────────────────────────────────────────────────────────────────────────────────────

def _actual(out, kid: str) -> str:
    """The Actual cell of a KPI row in the Markdown KPIs table (KPI_HEADERS: ID@version, name, target, actual, ...)."""
    row = next(line for line in out.markdown.splitlines() if line.startswith(f"| {kid}@"))
    return row[2:-2].split(" | ")[3]


def test_kpi_tables_show_values_in_the_unit_they_are_computed_in():
    """kpis.yaml units were inferred at import; where a value here is a different kind of number the table must show
    that kind (e.g. 3 migrations, not '300.0 %')."""
    reg = _reg()
    cis01 = quality.render_cis01(reg, reg.report("CIS-01"), MON, SUN, _cis01_facts(), NOW)
    assert _actual(cis01, "K-CAT-LIFECYCLE") == "3"
    cis06 = quality.render_cis06(reg, reg.report("CIS-06"), MON, SUN, {"eval": {"runs": [
        _run(), _run(id="33333333-cccc", avg_quality_score=7.5)], "releases": [], "items": []}}, NOW)
    assert _actual(cis06, "K-EVAL-DELTA") == "-0.50"
    cis07 = quality.render_cis07(reg, reg.report("CIS-07"), MON, SUN,
                                 {"waves": {"waves": [_wave()], "capped": False}, "catalog": _catalog()}, NOW)
    assert _actual(cis07, "K-WAVE-DONE") == "100.0 %"
    assert _actual(cis07, "K-WAVE-RETRY") == "12.5 %"
    assert _actual(cis07, "K-WAVE-RUNNING") == "4.50 USD"
    ops14 = reliability.render_ops14(reg, reg.report("OPS-14"), MON, SUN, _job_facts(), NOW, expected=EXPECTED)
    assert _actual(ops14, "K-JOB-P95") == "1.33×"                       # 1200 s p95 / 900 s expected
    rows = [{"version": v, "description": "x", "applied_at": _ts(MON)} for v in ("305", "306", "bk-035")]
    data03 = reliability.render_data03(reg, reg.report("DATA-03"), MON, SUN, {"versions": rows}, NOW)
    assert _actual(data03, "K-SCH-APPLIED") == "3"
    facts = {"runs": [{"kind": "retention_job", "status": s, "mode": "live", "created_by": "s", "finished_at": None}
                      for s in ("succeeded", "failed")]}
    data04 = reliability.render_data04(reg, reg.report("DATA-04"), date(2026, 10, 1), date(2026, 10, 31), facts, NOW)
    assert _actual(data04, "K-RET-RUNS") == "50.0 %"
    data02 = reliability.render_data02(reg, reg.report("DATA-02"), MON, SUN, {
        "unmatched": {"unmatched": 2, "keys": 5, "oldest": None}}, NOW)
    assert _actual(data02, "K-REC-UNMATCHED") == "2"
