"""AA-645 — the offline screen harness (scripts/aa645/s1_screen.py) and its scorer, with a fake gateway: no LLM, no
DB. Pins the call budget (≤ 3 per unit), the adhoc_aa645 / writer logging and the scoring maths."""
import json
from unittest.mock import patch

from scripts.aa645 import s1_screen, score_screen
from shared.llm_client.models import LLMResponse

TOUR = {
    "name": "Island Loop", "country": "Sri Lanka", "duration": "2 days",
    "summary": "A short loop by road.",
    "itineraries": "Day 1: Colombo to Chilaw\nRelax at the beach in Chilaw.\nDay 2: Chilaw to Colombo\nDrive back.",
    "inclusions": "Breakfast\nGuide", "exclusions": "Drinks",
}
SAMPLE = {"sample_id": "t1", "tour_id": "t1", "tour": TOUR, "seo": {"top_keywords": ["sri lanka loop"]},
          "stored": None}
WRITER = {"name": "Sri Lanka Loop — 2 Days", "subtitle": "Two days from Colombo to Chilaw and back.",
          "summary": "A two-day road loop from Colombo to the beach at Chilaw and back.",
          "highlights": ["Beach at Chilaw", "Colombo", "Road loop"],
          "itineraries": [{"day": 1, "title": "Colombo to Chilaw", "body": "Time at the beach in Chilaw."},
                          {"day": 2, "title": "Chilaw to Colombo", "body": "The drive back to Colombo."}]}
GOOD_META = {"seo_title": "Sri Lanka Loop: Colombo and Chilaw in 2 Days",
             "seo_meta": ("A two-day Sri Lanka road loop from Colombo to the beach at Chilaw and back again, with "
                          "breakfast and a local guide included on both of the days.")}
BAD_META = {"seo_title": "Sri Lanka Loop", "seo_meta": "A short loop."}


def resp(obj):
    return LLMResponse(content=json.dumps(obj), model_used="m", provider="bedrock", input_tokens=10,
                       output_tokens=5, cost_usd=0.001, stop_reason="end_turn")


class FakeClient:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def generate(self, req):
        self.requests.append(req)
        return resp(self.replies.pop(0))


def run(replies, arm="w"):
    client = FakeClient(replies)
    unit = {"key": f"t1__{arm}__m", "sample_id": "t1", "arm": arm, "model": "haiku-5-5"}
    ctx = {"brand": {}, "forbidden": [], "background": "allow", "examples": None}
    with patch("shared.llm_client.call_log.record_call_sync") as rec:
        out = s1_screen.run_unit(client, "00000000-0000-0000-0000-000000000000", unit, SAMPLE, ctx)
    return out, client, rec


def test_good_meta_first_time_is_two_calls_logged_as_adhoc_writer():
    out, client, rec = run([WRITER, GOOD_META])
    assert out["error"] is None and out["n_calls"] == 2
    assert all(r.stage == "adhoc_aa645" and r.model_tier == "haiku-5-5" for r in client.requests)
    assert [c.kwargs["stage"] for c in rec.call_args_list] == ["adhoc_aa645"] * 2
    assert {c.kwargs["role"] for c in rec.call_args_list} == {"writer"}
    assert [c.kwargs["quality_signal"]["call"] for c in rec.call_args_list] == ["write", "meta"]
    assert out["meta_first_problems"] == [] and out["output"]["seo_meta"] == GOOD_META["seo_meta"]
    assert out["output"]["itineraries"].startswith("Day 1 — Colombo to Chilaw")
    assert not [f for f in out["findings"] if f["code"].startswith(("DAY_", "META_", "TEXT_"))]


def test_a_bad_meta_gets_exactly_one_retry_and_is_never_cut():
    out, client, _ = run([WRITER, BAD_META, BAD_META])
    assert out["n_calls"] == 3
    assert "Its seo_meta has 13 characters" in client.requests[2].user_prompt
    assert out["meta_final_problems"] and out["output"]["seo_meta"] == "A short loop."


def test_writer_failure_stops_the_unit_after_one_call():
    client = FakeClient([])
    client.generate = lambda req: LLMResponse(
        content="not json", model_used="m", provider="bedrock", cost_usd=0.002)
    with patch("shared.llm_client.call_log.record_call_sync") as rec:
        out = s1_screen.run_unit(client, "00000000-0000-0000-0000-000000000000",
                                 {"key": "k", "sample_id": "t1", "arm": "w", "model": "x"}, SAMPLE,
                                 {"brand": {}, "forbidden": [], "background": "allow", "examples": None})
    assert out["error"] == "writer reply is not JSON" and out["n_calls"] == 1
    assert rec.call_args.kwargs["quality_signal"]["json_parsed"] is False


def test_e_and_aplus_arms_run_through_the_same_budget():
    e_llm = {"name": "Loop", "subtitle": "Two days.", "hook": "A two-day loop.", "highlights": ["a", "b", "c"],
             "days": [{"day": 1, "title": "Colombo to Chilaw", "sentence": "Beach time."},
                      {"day": 2, "title": "Chilaw to Colombo", "sentence": "Drive back."}]}
    out, _, _ = run([e_llm, GOOD_META], arm="e")
    assert out["n_calls"] == 2 and "At a glance" in out["output"]["summary"]
    out, client, _ = run([WRITER, GOOD_META], arm="aplus2")
    assert out["n_calls"] == 2 and "DAY STRUCTURE (checked by code;" in client.requests[0].user_prompt


def test_stratified_forces_then_round_robins_duration_kinds():
    mk = lambda i, d: {"sample_id": f"s{i}", "tour": {"name": "x", "duration": d, "itineraries": ""}}  # noqa: E731
    cands = [mk(1, "3 hours"), mk(2, "3 hours"), mk(3, "5 days"), mk(4, "12 days"), mk(5, "1 day")]
    got = s1_screen.stratified(cands, 4, [mk(9, "2 days")], "seed")
    assert got[0]["sample_id"] == "s9" and len(got) == 4
    assert len({g["tour"]["duration"] for g in got[1:]}) == 3


def test_figure_coverage_counts_source_unit_figures_in_the_page():
    from services.quality.s1_facts import build_fact_table
    facts = build_fact_table({**TOUR, "itineraries": TOUR["itineraries"] + " 40 km by car."})
    assert s1_screen.figure_coverage(facts, {"summary": "A 40 km drive."}) == 1.0
    assert s1_screen.figure_coverage(facts, {"summary": "A drive."}) == 0.0
    assert s1_screen.figure_coverage(build_fact_table(TOUR), {"summary": "x"}) is None


def _units():
    def u(sid, model, findings=()):
        return {"key": f"{sid}__w__{model}", "sample_id": sid, "arm": "w", "model": model, "n_calls": 2,
                "cost_usd": 0.01 if model == "cheap" else 0.1, "findings": list(findings), "figure_coverage": 0.5}
    return [u("a", "cheap"), u("b", "cheap"), u("a", "dear"), u("b", "dear")]


def test_score_pick_prefers_the_cheaper_model_inside_the_tie_band():
    units = _units()
    key = {f"p{i}": {"key": x["key"]} for i, x in enumerate(units)}
    passes = [{"pages": [{"page_id": f"p{i}", "checkable_claims": 20,
                          "errors": [{"severity": "serious"}] * (2 if units[i]["model"] == "cheap" else 2)}
                         for i in range(4)],
               "ranks": [{"group_id": "g", "order": [["p0"], ["p2"]]}]}]
    table = score_screen.score(units, score_screen.panel_by_unit(passes, key), None)
    assert table["w|cheap"]["serious_per_claim_pooled"] == 0.1
    assert table["w|cheap"]["rank_mean"] == 0.0 and table["w|dear"]["rank_mean"] == 1.0
    assert score_screen.pick(table)["pick"] == "w|cheap"


def test_score_pick_excludes_a_model_with_gate_codes():
    units = _units()
    for x in units:
        if x["model"] == "cheap":
            x["findings"] = [{"code": "META_CUT"}]
    key = {f"p{i}": {"key": x["key"]} for i, x in enumerate(units)}
    passes = [{"pages": [{"page_id": f"p{i}", "checkable_claims": 20, "errors": []} for i in range(4)]}]
    table = score_screen.score(units, score_screen.panel_by_unit(passes, key), None)
    assert table["w|cheap"]["pages_with_gate_code"] == 2
    assert score_screen.pick(table)["pick"] == "w|dear"


def test_wilcoxon_matches_a_known_case_and_drops_zeros():
    n, p = score_screen.wilcoxon([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert n == 10 and p < 0.01
    assert score_screen.wilcoxon([0, 0]) == (0, None)
    n, p = score_screen.wilcoxon([1, -1, 2, -2])
    assert n == 4 and p == 1.0


def test_export_examples_takes_only_the_chosen_arm_model_and_prefixes():
    units = [{"key": "k", "sample_id": "af0ded16-x", "tour_id": "af0ded16-x", "arm": "w", "model": "m",
              "output": {"name": "N", "trip_type": "t"}},
             {"key": "k2", "sample_id": "zz", "arm": "w", "model": "m", "output": {"name": "Z"}}]
    ex = score_screen.export_examples(units, ["af0ded16"], "w", "m")
    assert ex == [{"tour_id": "af0ded16-x", "draft_from": "w|m", "edited_by_nghiep": False,
                   "page": {"name": "N"}}]


def test_sample_from_row_builds_the_s1_shapes_and_agency_fields():
    row = {"tour_id": "t9", "src_name": "N", "src_highlights": '["a"]', "src_itineraries": "Day 1: A\\nx",
           "duration": "1 day", "group_size": "Max 8", "top_keywords": '["k1", {"keyword": "k2"}]',
           "people_also_ask": [{"question": "Q1?"}, "Q2?"], "aa_name": "Live", "aa_highlights": '["h"]'}
    s = s1_screen.sample_from_row(row)
    assert s["tour"]["highlights"] == ["a"] and s["tour"]["description"].endswith("Group size: Max 8")
    assert s["seo"] == {"top_keywords": ["k1", "k2"], "keywords": {"top_keywords": ["k1", "k2"]},
                        "people_also_ask": ["Q1?", "Q2?"]}
    assert s["stored"]["name"] == "Live" and s["stored"]["highlights"] == ["h"]
