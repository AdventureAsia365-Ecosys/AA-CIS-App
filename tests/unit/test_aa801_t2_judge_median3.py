"""AA-801 — T2 judge median-of-3 (option a) behind T2_JUDGE_REPEAT (default "single" = unchanged).

S235: the same T2 draft scored brand_fit 4..8 across calls and mission_present flipped, so one noisy
call decided save-at-6 vs save-at-8. median3 runs three parallel calls on every draft, keeps the
median brand_fit/distinct and the majority mission, and logs the raw calls in quality_signal.
All mock-only: no AWS, no DB, no LLM.
"""
import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from services.content_generation import brand_fit as bf
from services.content_generation import judge_node as jn
from services.content_generation.brand_fit import BrandFitResult, aggregate_runs, runs_signal
from shared.llm_client.models import LLMResponse

BRAND = {"brand_core_idea": "slow, private journeys", "brand_customer_mindset": "unhurried"}
GEN = {"name": "Kyoto Days", "subtitle": "Quiet temples", "summary": "Calm.", "highlights": ["Nanzen-ji"],
       "itineraries": "Day 1 — Arrive Kyoto.", "seo_title": "Kyoto", "seo_meta": "A calm Kyoto trip."}


def _res(bfit, distinct=9.0, mission=True, feedback=None, cost=0.001):
    return BrandFitResult(brand_fit_score=bfit, cross_brand_distinct=distinct, mission_present=mission,
                          feedback=feedback or f"fb{bfit}", judge_score=min(bfit, distinct),
                          model_used="satellite-gpt-5.6-luna", cost_usd=cost, input_tokens=100,
                          output_tokens=50, stop_reason="end_turn", account="acc3", fallback_used=False)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.delenv("T2_JUDGE_REPEAT", raising=False)
    monkeypatch.delenv("T2_JUDGE_GATE", raising=False)


# ── aggregation ───────────────────────────────────────────────────────────────────────────────

def test_median_of_three_takes_the_middle_value():
    out = aggregate_runs([_res(4.0, 5.0), _res(8.0, 9.0), _res(7.0, 6.0)], mission_absent_cap=6.0)
    assert out.brand_fit_score == 7.0 and out.cross_brand_distinct == 6.0
    assert out.judge_score == 6.0                       # min(7, 6), mission present: no cap


def test_median_ignores_one_outlier_in_either_direction():
    assert aggregate_runs([_res(8.0), _res(8.0), _res(2.0)], mission_absent_cap=6.0).brand_fit_score == 8.0
    assert aggregate_runs([_res(4.0), _res(4.0), _res(10.0)], mission_absent_cap=6.0).brand_fit_score == 4.0


def test_median_of_two_is_the_mean_of_both():
    # only reached when one of the three calls failed
    out = aggregate_runs([_res(6.0, 8.0), _res(8.0, 9.0)], mission_absent_cap=6.0, failed=1)
    assert out.brand_fit_score == 7.0 and out.cross_brand_distinct == 8.5
    assert {"failed": 1} in out.runs


def test_mission_is_majority_vote():
    out = aggregate_runs([_res(8.0, mission=True), _res(8.0, mission=False), _res(8.0, mission=True)],
                         mission_absent_cap=6.0)
    assert out.mission_present is True and out.judge_score == 8.0
    out = aggregate_runs([_res(8.0, mission=False), _res(8.0, mission=False), _res(8.0, mission=True)],
                         mission_absent_cap=6.0)
    assert out.mission_present is False and out.judge_score == 6.0     # capped


def test_mission_tie_counts_as_absent():
    out = aggregate_runs([_res(8.0, mission=True), _res(8.0, mission=False)], mission_absent_cap=6.0)
    assert out.mission_present is False and out.judge_score == 6.0


def test_no_cap_when_cap_is_none():
    out = aggregate_runs([_res(8.0, mission=False)] * 3, mission_absent_cap=None)
    assert out.mission_present is False and out.judge_score == 8.0


def test_feedback_from_the_run_closest_to_the_median_first_on_a_tie():
    out = aggregate_runs([_res(5.0, feedback="low"), _res(7.0, feedback="mid-a"), _res(7.0, feedback="mid-b")],
                         mission_absent_cap=6.0)
    assert out.feedback == "mid-a"


def test_cost_and_tokens_are_summed_and_runs_recorded():
    out = aggregate_runs([_res(6.0, cost=0.01), _res(7.0, cost=0.02), _res(8.0, cost=0.03)],
                         mission_absent_cap=6.0)
    assert out.cost_usd == pytest.approx(0.06)
    assert out.input_tokens == 300 and out.output_tokens == 150
    assert [r["brand_fit"] for r in out.runs] == [6.0, 7.0, 8.0]


def test_empty_results_raise():
    with pytest.raises(ValueError):
        aggregate_runs([], mission_absent_cap=6.0)


def test_runs_signal_shows_the_spread():
    out = aggregate_runs([_res(4.0, mission=False), _res(8.0), _res(7.0)], mission_absent_cap=6.0)
    sig = runs_signal(out, "median3")
    assert sig == {"mode": "median3", "n": 3, "failed": 0, "brand_fits": [4.0, 8.0, 7.0],
                   "distincts": [9.0, 9.0, 9.0], "missions": [False, True, True],
                   "chosen_brand_fit": 7.0, "chosen_distinct": 9.0, "chosen_mission": True,
                   "brand_fit_spread": 4.0}
    json.dumps(sig)   # must be JSON-serialisable for llm_call_log


# ── score_brand_fit(repeat=3) ─────────────────────────────────────────────────────────────────

def test_repeat3_calls_three_times_in_parallel_even_on_a_clear_score():
    scores = iter([9.0, 4.0, 8.0])
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5)   # breaks (raises) unless all three calls run at once

    def fake(*_a, **_k):
        barrier.wait()
        with lock:
            return _res(next(scores))
    with patch.object(bf, "_judge", side_effect=fake) as j, patch.object(bf, "_seeded_model") as seeded:
        out = bf.score_brand_fit(BRAND, GEN, repeat=3, stage="t2_judge", mission="slow journeys")
    assert j.call_count == 3
    assert seeded.call_count == 0                       # the AA-714 near-band layer is skipped
    assert out.brand_fit_score == 8.0                   # median(9, 4, 8)
    assert len([r for r in out.runs if "brand_fit" in r]) == 3
    assert all(c.kwargs["stage"] == "t2_judge" for c in j.call_args_list)


def test_repeat3_survives_one_failed_call():
    calls = iter([_res(6.0), RuntimeError("boom"), _res(8.0)])

    def fake(*_a, **_k):
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v
    with patch.object(bf, "_judge", side_effect=fake):
        out = bf.score_brand_fit(BRAND, GEN, repeat=3)
    assert out.brand_fit_score == 7.0 and {"failed": 1} in out.runs


def test_repeat3_all_failed_raises():
    with patch.object(bf, "_judge", side_effect=RuntimeError("down")):
        with pytest.raises(RuntimeError):
            bf.score_brand_fit(BRAND, GEN, repeat=3)


def test_repeat_default_keeps_single_call_and_empty_runs():
    with patch.object(bf, "_judge", side_effect=[_res(9.0)]) as j, \
            patch.object(bf, "_seeded_model", return_value=False):
        out = bf.score_brand_fit(BRAND, GEN)
    assert j.call_count == 1 and out.runs == ()


def test_aa714_near_band_path_result_unchanged():
    with patch.object(bf, "_judge", side_effect=[_res(8.0, mission=False), _res(7.0, mission=False),
                                                  _res(6.0, mission=True)]) as j, \
            patch.object(bf, "_seeded_model", return_value=False):
        out = bf.score_brand_fit(BRAND, GEN)
    assert j.call_count == 3
    assert out.brand_fit_score == 7.0 and out.mission_present is False and out.judge_score == 6.0
    assert out.feedback == "fb7.0" and out.cost_usd == pytest.approx(0.003)
    assert out.runs == ()                               # AA-714 shape and log unchanged


# ── judge_node: flag routing ──────────────────────────────────────────────────────────────────

_T2 = {"is_tenant_rewrite": True, "tenant_id": "t-1", "tour": {"tour_id": "x"},
       "brand_core_idea": "Movement and freedom.", "brand_customer_mindset": "Wants to feel alive.",
       "brand_voice_examples": ["Energetic"]}


def _judge_resp(fit, mission=True):
    return LLMResponse(content=json.dumps({"brand_fit_score": fit, "cross_brand_distinct": 9,
                                           "mission_present": mission, "feedback": "x"}),
                       model_used="gpt-4.1", cost_usd=0.001, provider="openai")


def _run(state, responses):
    with patch.object(bf, "LLMClient") as client, patch.object(jn, "record_call_sync") as rec:
        client.return_value.generate.side_effect = responses
        out = jn.judge_node(dict(state, generated=GEN, quality_score=9.75, feedback=""))
    return out, client.return_value.generate.call_count, rec.call_args.kwargs


def test_default_repeat_mode_is_single():
    assert jn.t2_judge_repeat() == "single"


def test_unknown_repeat_value_falls_back_to_single(monkeypatch):
    monkeypatch.setenv("T2_JUDGE_REPEAT", "median5")
    assert jn.t2_judge_repeat() == "single"


def test_flag_off_t2_path_unchanged():
    # gpt-4.1 is seeded → exactly one call, as before AA-801; no judge_runs key, no repeat kwarg
    with patch.object(jn, "score_brand_fit", wraps=jn.score_brand_fit) as score:
        out, n_calls, rec = _run(_T2, [_judge_resp(8)])
    assert n_calls == 1 and out["judge_score"] == 8.0
    assert "repeat" not in score.call_args.kwargs
    assert set(rec["quality_signal"]) == {
        "judge_score", "brand_fit_score", "cross_brand_distinct", "mission_present", "passed", "gate",
        "mission_source", "mission_gated"}


def test_median3_t2_judges_three_times_and_logs_runs(monkeypatch):
    monkeypatch.setenv("T2_JUDGE_REPEAT", "median3")
    out, n_calls, rec = _run(_T2, [_judge_resp(4, mission=False), _judge_resp(8), _judge_resp(7)])
    assert n_calls == 3
    assert out["judge_brand_fit"] == 7.0 and out["judge_mission_present"] is True
    assert out["judge_score"] == 7.0 and out["quality_score"] == 7.0
    assert rec["cost_usd"] == pytest.approx(0.003)
    runs = rec["quality_signal"]["judge_runs"]
    assert runs["mode"] == "median3" and runs["n"] == 3
    assert sorted(runs["brand_fits"]) == [4.0, 7.0, 8.0]
    assert runs["chosen_brand_fit"] == 7.0 and runs["brand_fit_spread"] == 4.0


def test_median3_does_not_touch_a1(monkeypatch):
    monkeypatch.setenv("T2_JUDGE_REPEAT", "median3")
    state = dict(_T2, is_tenant_rewrite=False)
    with patch.object(jn, "score_brand_fit") as score, patch.object(jn, "record_call_sync") as rec:
        score.return_value = MagicMock(brand_fit_score=8.0, cross_brand_distinct=3.0, mission_present=False,
                                       judge_score=3.0, feedback="", model_used="m", input_tokens=1,
                                       output_tokens=1, cost_usd=0.0)
        jn.judge_node(dict(state, generated=GEN, quality_score=9.75, feedback=""))
    assert "repeat" not in score.call_args.kwargs
    assert "judge_runs" not in rec.call_args.kwargs["quality_signal"]
