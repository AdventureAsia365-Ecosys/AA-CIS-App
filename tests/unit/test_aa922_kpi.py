"""AA-922 WM-30 — the weekly KPI math, the soft stops, keeps, the project stop rule and rule re-confirmation (pure; no
DB, no model call)."""
from datetime import date, datetime, timezone

import pytest

from services.writing_memory import kpi


def _c(**kw):
    return {**kpi.empty_counts(), **kw}


def test_wilson_interval():
    lo, hi = kpi.wilson(5, 20)
    assert 0.11 < lo < 0.12 and 0.46 < hi < 0.47
    assert kpi.wilson(0, 0) is None
    assert kpi.wilson(0, 50)[0] == 0.0


def test_derive_measures_t9_only_where_it_can():
    t9 = kpi.derive(_c(writes=40, flagged=4, repeat_flagged=2, labels=10, light=7, minutes_sum=50, minutes_n=10,
                       tenant_edits=4, admin_signals=10, admin_with_reason=9, cost_usd=2.0), "t9_blog")
    assert (t9["reflag_rate"], t9["repeat_error_rate"], t9["light_edit_share"], t9["minutes_to_fix"]) == (
        0.1, 0.05, 0.7, 5.0)
    assert t9["tenant_edit_rate"] == 0.1 and t9["capture_complete"] == 0.9 and t9["capture_ok"] is True
    assert t9["cost_per_write"] == 0.05 and len(t9["reflag_ci95"]) == 2
    a1 = kpi.derive(_c(writes=10, cost_usd=1.0), "a1")
    assert a1["reflag_rate"] is None and a1["tenant_edit_rate"] is None and a1["cost_per_write"] == 0.1
    none = kpi.derive(_c(), "t2")
    assert none["cost_per_write"] is None and none["tenant_edit_rate"] is None and none["capture_ok"] is None


def _d(writer="t9_blog", **kw):
    return kpi.derive(_c(**kw), writer)


@pytest.mark.parametrize("code, before, after", [
    ("reflag_over_8pct", _d(writes=30, flagged=1), _d(writes=40, flagged=12)),
    ("light_edit_drop", _d(labels=10, light=8), _d(labels=10, light=6)),
    ("minutes_to_fix_up", _d(labels=6, minutes_sum=60, minutes_n=6), _d(labels=6, minutes_sum=80, minutes_n=6)),
    ("tenant_edit_up", _d(writes=40, tenant_edits=4), _d(writes=40, tenant_edits=8)),
    ("cost_up", _d(writes=40, cost_usd=4.0), _d(writes=40, cost_usd=5.0)),
])
def test_each_soft_stop_fires(code, before, after):
    assert code in [s["code"] for s in kpi.soft_stops("t9_blog", "tenant", before, after)]


def test_soft_stops_hold_below_the_minimum_samples_and_without_a_change():
    assert kpi.soft_stops("t9_blog", "tenant", _d(writes=5), _d(writes=19, flagged=19)) == []      # < 20 writes
    assert kpi.soft_stops("t9_blog", "tenant", _d(labels=4, light=4), _d(labels=4, light=0)) == []  # < 5 labels
    same = _d(writes=40, flagged=1, labels=10, light=8, minutes_sum=50, minutes_n=10, tenant_edits=4, cost_usd=4.0)
    assert kpi.soft_stops("t9_blog", "tenant", same, same) == []
    # a re-flag of 10% on 40 writes: its Wilson lower bound is under 8%, no stop on noise
    assert kpi.soft_stops("t9_blog", "tenant", _d(writes=40), _d(writes=40, flagged=4)) == []
    # tenant edits up 20% but by under 5 points: no stop
    assert kpi.soft_stops("t9_blog", "tenant", _d(writes=100, tenant_edits=10), _d(writes=100, tenant_edits=13)) == []


def test_a_keep_covers_its_codes_until_it_expires():
    stops = [{"code": "cost_up"}, {"code": "reflag_over_8pct"}]
    today = date(2026, 10, 30)
    rest, kept = kpi.covered(stops, [{"codes": ["cost_up"], "kept_until": date(2026, 11, 2)}], today)
    assert [s["code"] for s in rest] == ["reflag_over_8pct"] and [s["code"] for s in kept] == ["cost_up"]
    rest, kept = kpi.covered(stops, [{"codes": [], "kept_until": today}], today)
    assert rest == [] and len(kept) == 2
    rest, _ = kpi.covered(stops, [{"codes": [], "kept_until": date(2026, 10, 29)}], today)     # expired
    assert len(rest) == 2


def test_improved_and_the_project_stop_rule():
    assert kpi.improved(_d(writes=30, flagged=6), _d(writes=30, flagged=3)) is True
    assert kpi.improved(_d(writes=30, flagged=3), _d(writes=30, flagged=3)) is False
    assert kpi.improved(_d("a1", writes=30), _d("a1", writes=30)) is None
    ps = kpi.project_stop({"t9_blog": [False, False, True], "t2": [False, None], "a1": [False]})
    assert ps["flagged"] == ["t9_blog"]


def test_weeks():
    assert kpi.week_of(date(2026, 10, 30)) == date(2026, 10, 26)
    assert kpi.last_full_week(datetime(2026, 10, 30, 12, tzinfo=timezone.utc)) == date(2026, 10, 19)
    assert kpi.last_full_week(datetime(2026, 10, 26, 0, 5, tzinfo=timezone.utc)) == date(2026, 10, 19)


def _sig(**kw):
    base = {"signal_id": "s1", "writer": "t9_blog", "channel": "blog", "scope": "tenant", "tenant_id": "A",
            "before_text": "A truly breathtaking view.", "after_text": "A view.", "gate": None, "problem": None,
            "reason_code": "ai_phrasing", "action": "tenant_edit", "actor_type": "tenant", "actor_id": "t",
            "fact_status": "pass", "source_id": "p1"}
    return {**base, **kw}


def test_reconfirmation_needs_a_fresh_signal_of_the_rule_cluster():
    rule = {"kind": "writer_instruction", "cluster_key": "t9_blog|reason:ai_phrasing"}
    assert kpi.reconfirmed(rule, [_sig()]) is True
    assert kpi.reconfirmed(rule, [_sig(reason_code="voice_off_brand")]) is False
    assert kpi.reconfirmed(rule, []) is False
    summary = {"kind": "preference_summary", "cluster_key": "t9_blog|pref:voice"}
    assert kpi.reconfirmed(summary, [_sig(reason_code=None)]) is True


def test_the_trend_script_renders_weeks_oldest_first():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "writing_memory" / "wm30_kpi_trend.py"
    spec = importlib.util.spec_from_file_location("wm30_kpi_trend", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    reports = [{"week_start": "2026-10-26", "kpis": {"writers": {"t9_blog": {"overall": {"writes": 3,
                                                                                         "reflag_rate": 0.25}}}},
                "actions": [{"writer": "t9_blog", "release_no": 2, "codes": ["cost_up"], "action": "rolled_back"}]},
               {"week_start": "2026-10-19", "kpis": {"writers": {}}, "actions": []}]
    md = mod.render(reports)
    assert md.index("2026-10-19") < md.index("2026-10-26")
    assert "| 2026-10-26 | 3 | 25.0% |" in md and "t9_blog #2 cost_up -> rolled_back" in md


def test_the_sampler_queues_one_job_per_week_for_the_week_before(monkeypatch):
    import asyncio

    from api.core import ops_metrics_sampler as sampler
    from services.jobs.writing_memory_job import KPI_WEEKLY
    import shared.jobs.registry as registry
    calls = []

    async def enqueue(pool, kind, payload, *, idempotency_key, created_by):
        calls.append((kind, payload, idempotency_key))
        return "job-1", len(calls) == 1

    monkeypatch.setattr(registry, "enqueue", enqueue)
    out: dict = {}
    ts = datetime(2026, 10, 30, 9, tzinfo=timezone.utc)
    asyncio.run(sampler._writing_memory_kpi_part(None, {KPI_WEEKLY: object()}, ts, out))
    asyncio.run(sampler._writing_memory_kpi_part(None, {KPI_WEEKLY: object()}, ts, out))
    assert calls[0] == (KPI_WEEKLY, {"week_start": "2026-10-19", "apply": True, "trigger": "schedule"},
                        f"{KPI_WEEKLY}:2026-10-19")
    assert calls[0][2] == calls[1][2] and out == {"writing_memory_kpi_weekly": "job-1"}
    out2: dict = {}
    asyncio.run(sampler._writing_memory_kpi_part(None, {}, ts, out2))          # kind not registered: nothing
    assert out2 == {} and len(calls) == 2
