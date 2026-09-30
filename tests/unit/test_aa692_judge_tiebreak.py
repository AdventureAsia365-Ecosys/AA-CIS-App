"""AA-692 A1-4 — Jev tie-break when the A1 judge score sits on the retry line."""
from unittest.mock import patch

from services.content_generation import judge_node as jn
from shared.llm_client.decide import Decision, Verdict


def _dec(zone, mode="enforce"):
    return Decision(verdicts={jn.TIE_Q: Verdict(jn.TIE_Q, mode, zone, probability=0.5)})


def test_band_only_when_judge_decides():
    assert jn.in_tie_band(7.4, 9.5)
    assert jn.in_tie_band(6.5, 9.5)
    assert not jn.in_tie_band(6.4, 9.5)            # clearly below
    assert not jn.in_tie_band(7.6, 9.5)            # clearly above
    assert not jn.in_tie_band(7.0, 6.8)            # validate already failed: the judge does not decide


def test_tie_break_score():
    assert jn.tie_break_score(6.6, _dec("accept")) == 7.0
    assert jn.tie_break_score(7.4, _dec("accept")) == 7.4
    assert jn.tie_break_score(7.3, _dec("reject")) == 6.9
    assert jn.tie_break_score(6.6, _dec("reject")) == 6.6
    assert jn.tie_break_score(6.8, _dec("grey")) == 6.8
    assert jn.tie_break_score(6.8, Decision(verdicts={jn.TIE_Q: Verdict(jn.TIE_Q, "shadow", "grey")})) == 6.8


class _Result:
    brand_fit_score = 6.8
    cross_brand_distinct = 3.0
    mission_present = True
    judge_score = 3.0
    feedback = "More restraint."
    model_used = "m"
    input_tokens = output_tokens = 1
    cost_usd = 0.0
    stop_reason = "end"
    account = "acc3"
    fallback_used = False


STATE = {"quality_score": 9.6, "generated": {"name": "N", "summary": "S", "highlights": ["h"]},
         "brand_core_idea": "quiet luxury", "tour": {"tour_id": "t1"}, "retry_count": 0}


def _run(decision):
    with patch.object(jn, "score_brand_fit", return_value=_Result()), \
         patch.object(jn, "has_brand_signals", return_value=True), \
         patch.object(jn, "record_call_sync"), \
         patch("shared.llm_client.decide.decide_sync", return_value=decision) as m:
        return jn.judge_node(dict(STATE)), m


def test_node_enforced_accept_passes_the_tour():
    out, m = _run(_dec("accept"))
    assert m.call_count == 1 and out["quality_score"] == 7.0 and out["judge_tiebreak"]["after"] == 7.0


def test_node_shadow_changes_nothing():
    out, _ = _run(_dec("grey", mode="shadow"))
    assert out["quality_score"] == 6.8 and out["judge_tiebreak"]["before"] == 6.8


def test_node_not_asked_outside_band_or_for_tenants():
    class Far(_Result):
        brand_fit_score = 8.5
    with patch.object(jn, "score_brand_fit", return_value=Far()), \
         patch.object(jn, "has_brand_signals", return_value=True), \
         patch.object(jn, "record_call_sync"), \
         patch("shared.llm_client.decide.decide_sync") as m:
        out = jn.judge_node(dict(STATE))
    assert m.call_count == 0 and out["judge_tiebreak"] is None
    with patch.object(jn, "score_brand_fit", return_value=_Result()), \
         patch.object(jn, "has_brand_signals", return_value=True), \
         patch.object(jn, "record_call_sync"), \
         patch("shared.llm_client.decide.decide_sync") as m:
        jn.judge_node({**STATE, "is_tenant_rewrite": True})
    assert m.call_count == 0
