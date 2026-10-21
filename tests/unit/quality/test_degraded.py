"""AA-925 (Q-2) — degraded[]: the registry, the per-output list, the A1 / T9 holds and the per-job Jev counter."""
import asyncio
from types import SimpleNamespace

import pytest

from services.quality import degraded as dg
from shared.llm_client.decide import Verdict


def test_add_sums_the_same_entry_and_keeps_others():
    lst = dg.add([], dg.JEV_UNAVAILABLE, detector="grounding", count=3)
    lst = dg.add(lst, dg.JEV_UNAVAILABLE, detector="grounding", count=2)
    lst = dg.add(lst, dg.JUDGE_UNAVAILABLE, detector="judge")
    assert dg.counts(lst) == {"jev_unavailable": 5, "judge_unavailable": 1}
    assert dg.is_hard(lst) and dg.hard_names(lst) == ["jev_unavailable"]


def test_unknown_name_is_refused():
    with pytest.raises(KeyError):
        dg.add([], "made_up", detector="x")


def test_replace_detector_reports_only_the_latest_check():
    lst = dg.add([], dg.JEV_UNAVAILABLE, detector="grounding", count=9)
    lst = dg.add(lst, dg.REPAIR_CONTRACT, detector="flag_fix")
    lst = dg.replace_detector(lst, "grounding", [])
    assert dg.counts(lst) == {"repair_contract": 1}
    assert not dg.is_hard(lst)


@pytest.mark.parametrize("mode,severity", [("enforce", "hard"), ("off", "hard"), (None, "hard"),
                                           ("shadow", "count")])
def test_jev_severity(mode, severity):
    assert dg.jev_severity(mode) == severity


def test_jev_entries_split_by_severity():
    entries = dg.jev_entries([{"mode": "enforce"}, {"mode": None}, {"mode": "shadow"}])
    assert {(e["severity"], e["count"]) for e in entries} == {("hard", 2), ("count", 1)}


def test_a1_hold_adds_the_one_hard_code_and_manual_check():
    state = {"failure_codes": ["SEO_TITLE_TOO_LONG"], "brand_audit_status": "fixed", "revalidate_ran": True,
             "degraded": dg.add([], dg.JEV_UNAVAILABLE, detector="grounding", count=4)}
    out = dg.apply_a1_hold(state)
    assert out["failure_codes"] == ["SEO_TITLE_TOO_LONG", dg.HOLD_CODE]
    assert out["brand_audit_status"] == "manual_check" and out["revalidate_passed"] is False
    assert "jev_unavailable" in out["feedback"]


def test_a1_hold_ignores_soft_and_block_entries():
    state = {"failure_codes": [], "brand_audit_status": "pass",
             "degraded": dg.add(dg.add([], dg.REPAIR_CONTRACT, detector="flag_fix"), dg.DAY_CHECK_SKIPPED,
                                detector="day_oracle")}
    assert dg.apply_a1_hold(state) is state


def test_hold_code_is_hard_but_not_rewrite_class():
    from api.routers.admin_pipeline import _REWRITE_CLASS_HARD_CODES
    from services.content_generation.graph import HARD_CODE_REPAIR, _HARD_BLOCK_CODES

    assert dg.HOLD_CODE in _HARD_BLOCK_CODES
    assert dg.HOLD_CODE not in _REWRITE_CLASS_HARD_CODES   # a stronger model cannot fix an outage
    assert HARD_CODE_REPAIR[dg.HOLD_CODE].startswith("review:")


def test_t9_entries_read_the_final_ledger_marker_and_contract_reverts():
    ledger = [{"gate": "F9_cta_fact", "passed": False, "blocking": False, "degraded": "f9_judge_unavailable"},
              {"gate": "F1_grounding", "passed": True}]
    lst = dg.t9_entries(ledger, repair_items_contract=2)
    assert dg.counts(lst) == {"f9_judge_unavailable": 1, "repair_contract": 2}
    assert dg.apply_t9_hold("approved", None, lst) == ("held", "degraded: f9_judge_unavailable")
    assert dg.apply_t9_hold("held", "F1_grounding: x", lst) == ("held", "F1_grounding: x")
    flag = dg.t9_flag(lst)
    assert flag["gate"] == "degraded" and flag["blocking"] is False and flag["hard"] == ["f9_judge_unavailable"]


def test_t9_no_marker_no_hold():
    lst = dg.t9_entries([{"gate": "F9_cta_fact", "passed": True}])
    assert lst == [] and dg.t9_flag(lst) is None
    assert dg.apply_t9_hold("approved", None, lst) == ("approved", None)


def test_job_collector_counts_error_verdicts_only():
    with dg.collect() as box:
        dg.note_verdicts("a3_atomize", [Verdict("q", "enforce", "error"), Verdict("q", "off", "skipped"),
                                        Verdict("q", "enforce", "accept", probability=0.9),
                                        Verdict("q", "shadow", "error")])
    assert box == {"jev_unavailable:a3_atomize:hard": 1, "jev_unavailable:a3_atomize:count": 1}
    assert dg.merge_job_result({"n": 1}, box)["degraded"]["totals"] == {"jev_unavailable": 2}
    assert dg.merge_job_result({"n": 1}, {}) == {"n": 1}
    dg.note_verdicts("x", [Verdict("q", "enforce", "error")])   # no collector bound: a no-op


def test_decide_counts_an_outage_into_the_bound_job(monkeypatch):
    """The central counter: decide() itself notes every zone=error verdict, so A3 / ingestion / prefetch
    outages reach shared.job.result.degraded without their code knowing (here: decide_failed)."""
    from shared.llm_client import decide

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(decide, "_decide", boom)
    with dg.collect() as box:
        out = asyncio.run(decide.decide("a3_atomize", "atom:1", "text", ["q1", "q2"], pool=SimpleNamespace()))
    assert {v.zone for v in out.verdicts.values()} == {"error"}
    assert box == {"jev_unavailable:a3_atomize:hard": 2}


def test_validate_records_day_check_skipped(monkeypatch):
    from services.content_generation import graph

    state = {"generated": {"name": "Paro walk", "subtitle": "s", "summary": "A walk.", "highlights": ["a", "b", "c"],
                           "itineraries": "Walk through Paro town and visit the dzong.",
                           "seo_title": "Paro walk", "seo_meta": "A walk."},
             "tour": {"name": "Paro walk", "duration": "", "itineraries": "Walk through Paro town."},
             "retry_count": 0}
    out = graph.validate_node(state)
    assert dg.counts(out["degraded"]).get(dg.DAY_CHECK_SKIPPED) == 1
    again = graph.validate_node({**state, "degraded": out["degraded"]})
    assert dg.counts(again["degraded"]).get(dg.DAY_CHECK_SKIPPED) == 1   # replaced per validate, not summed


def test_regression_eval_never_flags_an_invalid_run():
    from services.eval import regression

    results = [{"src_name": "a", "degraded": dg.add([], dg.JEV_UNAVAILABLE, detector="grounding", count=3)},
               {"src_name": "b", "degraded": dg.add([], dg.REPAIR_CONTRACT, detector="flag_fix")}]
    summary = regression.degraded_summary(results)
    assert summary == {"counts": {"jev_unavailable": 3, "repair_contract": 1}, "invalid": True,
                       "invalid_tours": ["a"]}
    current = {"avg_quality_score": 1.0, "details": {"invalid": True}}
    assert regression._detect_regression("s1_old", current, {"avg_quality_score": 9.0}) is False
    assert regression._detect_regression("s1_old", {**current, "details": {}}, {"avg_quality_score": 9.0}) is True


def test_worker_writes_degraded_into_the_job_result():
    from shared.jobs import worker

    with worker._degraded_collect() as box:
        dg.note_verdicts("s1_prefetch", [Verdict("q", "enforce", "error")])
    assert worker._degraded_result({"done": 3}, box)["degraded"]["totals"] == {"jev_unavailable": 1}
    assert worker._degraded_result(None, {}) is None
