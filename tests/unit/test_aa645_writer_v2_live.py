"""AA-645 step 0 — the S1 writer v2 on the live A1 path, behind S1_WRITER_V2 (default off).

* the flag: off by default; on, it applies to the A1 admin rewrite only — never a T2 tenant rewrite (risk T8), never
  another write stage, never a Bedrock Batch seed;
* a draft: writer + meta (+ one meta retry) on the s1_generate stage (the Model Gateway route picks the model; no
  model id), every reply one llm_call_log row (role writer, writer_version v2);
* graph: generate_node routes to v2 only when it applies; validate keeps the length-ratio / DFS codes report-only for
  a v2 draft; the judge logs brand fit but does not gate a v2 draft (d02).
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from services.content_generation import graph, judge_node as jn, s1_writer_v2_live as live
from services.content_generation.brand_fit import BrandFitResult

TOUR = {
    "name": "Hanoi and Halong Bay", "subtitle": "", "summary": "Two days in Hanoi and on Halong Bay.",
    "description": "", "highlights": ["Old Quarter", "Halong Bay"],
    "itineraries": "Day 1: Hanoi. Walk the Old Quarter.\nDay 2: Halong Bay. Cruise and kayak 3 km.",
    "country": "Vietnam", "duration": "2 days", "price": None, "inclusions": "Meals as per itinerary",
    "exclusions": "Flights",
}
WRITER = {"name": "Hanoi and Halong Bay — 2 Days", "subtitle": "Old Quarter walk, then a Halong Bay cruise.",
          "summary": "Two days: Hanoi's Old Quarter on foot, then a cruise on Halong Bay with kayaking.",
          "highlights": ["Old Quarter walk", "Halong Bay cruise", "Kayaking"],
          "itineraries": [{"day": 1, "title": "Hanoi Old Quarter", "body": "A walk through the Old Quarter."},
                          {"day": 2, "title": "Halong Bay", "body": "A cruise on the bay and 3 km of kayaking."}],
          "trip_type": "cultural"}
META_OK = {"seo_title": "Hanoi and Halong Bay in 2 Days",
           "seo_meta": ("A two-day tour with a walk through Hanoi's Old Quarter, then a cruise on Halong Bay with "
                        "3 km of kayaking between the limestone islands.")}


class _Resp:
    def __init__(self, content, cost=0.01):
        self.content = content if isinstance(content, str) else json.dumps(content)
        self.model_used, self.input_tokens, self.output_tokens, self.cost_usd = "sonnet-route", 100, 50, cost
        self.stop_reason, self.satellite_account, self.fallback_used = "end_turn", "acc3", False
        self.cache_read_tokens = self.cache_write_tokens = 0


def _client(*replies):
    c = MagicMock()
    c.generate = MagicMock(side_effect=[_Resp(r) for r in replies])
    return c


def _state(**kw):
    s = {"tour": dict(TOUR), "seo": {}, "brand_forbidden_words": [], "is_tenant_rewrite": False,
         "generate_stage": "s1_generate", "model_tier": None, "tenant_id": None, "feedback": "",
         "tour_agency": {"group_size": "2-12", "best_time_to_go": None}}
    s.update(kw)
    return s


# ── the flag ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("S1_WRITER_V2", raising=False)
    assert not live.enabled() and not live.applies(_state())


@pytest.mark.parametrize("override, applies", [
    ({}, True),
    ({"is_tenant_rewrite": True, "generate_stage": "t2_generate"}, False),   # T2: never (risk T8)
    ({"is_tenant_rewrite": True}, False),
    ({"generate_stage": "t2_generate"}, False),
    ({"batch_text": "{...}"}, False),                                         # Bedrock Batch seed: today's writer
    ({"generate_stage": None}, True),                                          # older A1 caller: s1_generate
])
def test_flag_applies_to_the_a1_admin_rewrite_only(monkeypatch, override, applies):
    monkeypatch.setenv("S1_WRITER_V2", "on")
    assert live.applies(_state(**override)) is applies


def test_is_v2_never_holds_for_a_tenant_rewrite():
    assert live.is_v2({"writer_version": "v2"})
    assert not live.is_v2({"writer_version": "v2", "is_tenant_rewrite": True})
    assert not live.is_v2({})


# ── one draft ────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_draft_is_writer_plus_meta_on_the_a1_stage_each_logged():
    client = _client(WRITER, META_OK)
    with patch.object(live, "record_call_sync") as rec, \
         patch("services.content_generation.s1_writer_v2.meta_problems", return_value=[]):
        out = live.write(_state(), client)
    assert client.generate.call_count == 2
    for call in client.generate.call_args_list:
        req = call.args[0]
        assert req.stage == "s1_generate" and req.model_tier is None    # the route picks the model
    rows = [c.kwargs for c in rec.call_args_list]
    assert [r["quality_signal"]["call"] for r in rows] == ["write", "meta"]
    assert all(r["stage"] == "s1_generate" and r["role"] == "writer" for r in rows)
    assert all(r["quality_signal"]["writer_version"] == "v2" for r in rows)
    page = out["generated"]
    assert page["seo_title"] == META_OK["seo_title"] and page["seo_meta"] == META_OK["seo_meta"]
    assert page["itineraries"].startswith("Day 1 — Hanoi Old Quarter\n")
    assert "\n\nDay 2 — Halong Bay\n" in page["itineraries"]
    assert out["calls"] == 2 and out["cost_usd"] == pytest.approx(0.02) and out["error"] == ""


def test_the_writer_prompt_is_the_w_fact_sheet_with_the_agency_fields():
    client = _client(WRITER, META_OK)
    with patch.object(live, "record_call_sync"), \
         patch("services.content_generation.s1_writer_v2.meta_problems", return_value=[]):
        live.write(_state(feedback="ITINERARY_STRUCTURE_WEAK"), client)
    req = client.generate.call_args_list[0].args[0]
    assert "FACT SHEET" in req.user_prompt and "Group size: 2-12" in req.user_prompt
    assert "white-label" in req.system_prompt and "Adventure Asia" in req.system_prompt   # "never name … AA"
    assert "PREVIOUS ATTEMPT FEEDBACK:\nITINERARY_STRUCTURE_WEAK" in req.user_prompt
    assert "CONTRAST REQUIREMENT" not in req.system_prompt          # no brand-differentiation block


def test_one_meta_retry_at_most():
    client = _client(WRITER, {"seo_title": "x", "seo_meta": "too short."}, {"seo_title": "y", "seo_meta": "still."})
    with patch.object(live, "record_call_sync"), \
         patch("services.content_generation.s1_writer_v2.meta_problems", return_value=["seo_meta too short"]):
        out = live.write(_state(), client)
    assert client.generate.call_count == 3 and out["calls"] == 3
    assert out["meta_problems"] == ["seo_meta too short"]
    assert out["generated"]["seo_meta"] == "still."                   # never cut, validate / flag_fix see it


def test_a_failed_writer_call_ends_the_draft_without_raising():
    client = MagicMock()
    client.generate = MagicMock(side_effect=RuntimeError("throttled"))
    out = live.write(_state(), client)
    assert out["generated"] == {} and "throttled" in out["error"] and out["calls"] == 1


def test_an_unparsable_writer_reply_is_logged_and_ends_the_draft():
    client = _client("not json at all")
    with patch.object(live, "record_call_sync") as rec, \
         patch("services.content_generation.s1_writer_v2.parse_json", return_value=None):
        out = live.write(_state(), client)
    assert out["generated"] == {} and client.generate.call_count == 1
    assert rec.call_args.kwargs["quality_signal"]["json_parsed"] is False


# ── graph wiring ──────────────────────────────────────────────────────────────────────────────────────────────

def test_generate_node_uses_v2_when_it_applies(monkeypatch):
    monkeypatch.setenv("S1_WRITER_V2", "true")
    fake = {"generated": {"name": "N", "summary": "S", "itineraries": "Day 1 — A\nB"}, "cost_usd": 0.05, "error": "",
            "prompt_version": "abcd1234", "cache_read_tokens": 0, "cache_write_tokens": 0, "model_used": "m"}
    with patch.object(graph, "LLMClient"), patch.object(live, "write", return_value=fake) as w:
        out = graph.generate_node(_state(cost_usd=0.01))
    w.assert_called_once()
    assert out["writer_version"] == "v2" and out["cost_usd"] == pytest.approx(0.06)
    assert out["itinerary_day_ratios"] == [] and out["prompt_version"] == "abcd1234"


def test_generate_node_never_uses_v2_for_a_tenant_rewrite(monkeypatch):
    monkeypatch.setenv("S1_WRITER_V2", "true")
    with patch.object(graph, "LLMClient") as client, patch.object(live, "write") as w, \
         patch.object(graph, "record_call_sync"):
        client.return_value.generate.side_effect = RuntimeError("stop after routing")
        out = graph.generate_node(_state(is_tenant_rewrite=True, generate_stage="t2_generate"))
    w.assert_not_called()
    assert "writer_version" not in out


def _validate(state):
    gen = {"name": "Hanoi and Halong Bay", "subtitle": "Old Quarter and a Halong Bay cruise",
           "summary": "Two days.", "highlights": ["a", "b", "c"],
           "itineraries": "Day 1 — Hanoi\nWalk.\n\nDay 2 — Halong Bay\nCruise.",
           "seo_title": "Hanoi and Halong Bay", "seo_meta": META_OK["seo_meta"]}
    tour = dict(TOUR, itineraries=("Day 1: Hanoi. " + "Walk the lanes of the Old Quarter and its temples. " * 12
                                   + "\nDay 2: Halong Bay. " + "Cruise between limestone islands and kayak. " * 12))
    base = {"tour": tour, "generated": gen, "seo": {"top_keywords": [{"keyword": "sapa trekking tours"}]},
            "brand_forbidden_words": [], "is_tenant_rewrite": False}
    return graph.validate_node({**base, **state})


def test_validate_keeps_length_ratio_and_dfs_codes_report_only_for_v2():
    v1 = _validate({})
    v2 = _validate({"writer_version": "v2"})
    assert {"ITINERARY_STILL_COMPRESSED", "DFS_INTENT_UNDERUSED"} <= set(v1["failure_codes"])
    assert not {"ITINERARY_STILL_COMPRESSED", "DFS_INTENT_UNDERUSED"} & set(v2["failure_codes"])
    reported = {f["code"] for f in v2["quality_shadow"] if f.get("detector") == "s1_writer_v2"}
    assert reported == {"ITINERARY_STILL_COMPRESSED", "DFS_INTENT_UNDERUSED"}
    assert v2["quality_score"] > v1["quality_score"]


def _brand_fit(score):
    return BrandFitResult(brand_fit_score=score, cross_brand_distinct=8.0, mission_present=True,
                          feedback="reads generic", judge_score=score, model_used="judge", cost_usd=0.002,
                          input_tokens=10, output_tokens=5, stop_reason="end_turn", account=None, fallback_used=False)


def _judge(state):
    base = {"generated": {"name": "N", "summary": "S", "highlights": []}, "quality_score": 9.0, "feedback": "",
            "cost_usd": 0.0, "brand_core_idea": "x", "brand_customer_mindset": "y", "brand_voice_examples": ["z"],
            "tour": {"tour_id": "t-1"}}
    with patch.object(jn, "score_brand_fit", return_value=_brand_fit(5.0)), \
         patch.object(jn, "_write_judge_rows") as rows, patch.object(jn, "_tie_break") as tie:
        out = jn.judge_node({**base, **state})
    return out, rows.call_args.args[0][0]["quality_signal"], tie


def test_judge_gates_a_v1_draft_on_brand_fit():
    out, signal, _ = _judge({})
    assert out["quality_score"] == 5.0 and "reads generic" in out["feedback"]
    assert signal["gate"] == "brand_fit" and signal["passed"] is False


def test_judge_logs_but_does_not_gate_a_v2_draft():
    out, signal, tie = _judge({"writer_version": "v2"})
    assert out["quality_score"] == 9.0 and out["feedback"] == ""   # validate's score, no brand feedback
    assert out["judge_brand_fit"] == 5.0                             # still computed and kept for the report
    assert signal["gate"] == jn.A1_REPORT_ONLY_GATE and signal["brand_fit_score"] == 5.0
    tie.assert_not_called()


def test_a_tenant_rewrite_is_still_gated_even_if_marked_v2():
    with patch.object(jn, "t2_mission", return_value=""):
        out, signal, _ = _judge({"writer_version": "v2", "is_tenant_rewrite": True, "tenant_id": "ten-1"})
    assert out["quality_score"] == 5.0 and signal["gate"] != jn.A1_REPORT_ONLY_GATE
