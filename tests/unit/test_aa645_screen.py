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


# ------------------------------------------------------------------ review round 1 (S248)

def test_gate_set_is_exact_and_day_place_mismatch_is_report_only():
    from scripts.aa645 import gates
    from services.quality import day_oracle, s1_checks
    assert not gates.is_gate("DAY_PLACE_MISMATCH") and gates.is_report("DAY_PLACE_MISMATCH")
    assert not gates.is_gate("SOURCE_DAY_CONTRADICTION") and not gates.is_gate("DAY_CHECK_SKIPPED")
    oracle_day = {v for k, v in vars(day_oracle).items() if k.startswith("DAY_") and isinstance(v, str)
                  and v.startswith("DAY_")} - {day_oracle.DAY_CHECK_SKIPPED}
    assert oracle_day == gates.DAY_GATE_CODES          # pinned to the oracle's names
    for c in (s1_checks.META_CUT, s1_checks.META_LENGTH, s1_checks.TEXT_TRUNCATED, s1_checks.NUMBER_NOT_IN_SOURCE,
              s1_checks.INCLUSION_CONTRADICTED):
        assert gates.is_gate(c)
    for c in (s1_checks.NUMBER_NOUN_MISMATCH, s1_checks.NAME_NOT_IN_SOURCE, s1_checks.DAY_PLACE_MISMATCH):
        assert not gates.is_gate(c)


def test_day_place_mismatch_is_not_a_gate_in_the_summary_or_the_pick():
    units = _units()
    for x in units:
        if x["model"] == "cheap":
            x["findings"] = [{"code": "DAY_PLACE_MISMATCH"}]
    summary = s1_screen.summarize([{**u, "calls": []} for u in units])
    assert summary["w|cheap"]["pages_with_any_gate_code"] == 0
    assert summary["w|cheap"]["report_codes_pages"] == {"DAY_PLACE_MISMATCH": 2}
    key = {f"p{i}": {"key": x["key"]} for i, x in enumerate(units)}
    passes = [{"pages": [{"page_id": f"p{i}", "checkable_claims": 20, "errors": []} for i in range(4)]}]
    table = score_screen.score(units, score_screen.panel_by_unit(passes, key), None)
    assert table["w|cheap"]["pages_with_gate_code"] == 0
    assert score_screen.pick(table)["pick"] == "w|cheap"


def test_pick_is_none_when_no_model_is_gate_clean():
    units = _units()
    for x in units:
        x["findings"] = [{"code": "TEXT_TRUNCATED"}]
    key = {f"p{i}": {"key": x["key"]} for i, x in enumerate(units)}
    passes = [{"pages": [{"page_id": f"p{i}", "checkable_claims": 20, "errors": []} for i in range(4)]}]
    got = score_screen.pick(score_screen.score(units, score_screen.panel_by_unit(passes, key), None))
    assert got["pick"] is None and got["gate_clean_pick"] is False
    assert got["rows"]["w|cheap"]["pages_with_gate_code"] == 2


def test_pick_needs_zero_writer_errors_and_compares_on_common_tours():
    # cheap failed tour b (its hard tour); on tour a it ties with dear. Without the common-tour rule cheap would
    # win on an easier set; with it cheap is not gate-clean (errors) and dear is picked.
    units = _units() + [{"key": "c__w__cheap", "sample_id": "c", "arm": "w", "model": "cheap", "n_calls": 1,
                         "cost_usd": 0.01, "error": "writer reply is not JSON", "findings": []},
                        {"key": "c__w__dear", "sample_id": "c", "arm": "w", "model": "dear", "n_calls": 2,
                         "cost_usd": 0.1, "findings": []}]
    key = {f"p{i}": {"key": x["key"]} for i, x in enumerate(units)}
    serious = {"a__w__cheap": 0, "b__w__cheap": 0, "a__w__dear": 0, "b__w__dear": 4, "c__w__dear": 0}
    passes = [{"pages": [{"page_id": p, "checkable_claims": 20, "errors": [{"severity": "serious"}] * serious[k["key"]]}
                         for p, k in key.items() if k["key"] in serious]}]
    table = score_screen.score(units, score_screen.panel_by_unit(passes, key), None)
    assert table["w|cheap"]["common_tours"] == 2 and table["w|dear"]["common_tours"] == 2
    assert table["w|dear"]["serious_per_claim_common"] == 0.1 and table["w|cheap"]["serious_per_claim_common"] == 0.0
    got = score_screen.pick(table)
    assert got["pick"] == "w|dear" and got["gate_clean"] == ["w|dear"]
    assert got["rows"]["w|cheap"]["errors"] == 1 and got["rows"]["w|cheap"]["panel_scored"] == 2


def test_tie_band_has_an_absolute_floor_when_the_best_rate_is_zero():
    table = {"w|dear": {"serious_per_claim_common": 0.0, "pages_with_gate_code": 0, "errors": 0, "usd_per_tour": 0.1,
                        "units": 12, "panel_scored": 12, "common_tours": 12},
             "w|cheap": {"serious_per_claim_common": 0.004, "pages_with_gate_code": 0, "errors": 0,
                         "usd_per_tour": 0.01, "units": 12, "panel_scored": 12, "common_tours": 12}}
    assert score_screen.pick(table)["pick"] == "w|cheap"
    table["w|cheap"]["serious_per_claim_common"] = 0.006
    assert score_screen.pick(table)["pick"] == "w|dear"


def test_wilcoxon_rounds_float_noise_before_ties_and_zeros():
    assert score_screen.wilcoxon([0.1 + 0.2 - 0.3]) == (0, None)
    n, p = score_screen.wilcoxon([0.3 - 0.1, 0.2, -(0.25 - 0.05), -0.2])
    assert n == 4 and p == 1.0           # all four |d| are one tie group


class BilledError(RuntimeError):
    pass


def _billed(cost, stop):
    e = BilledError("Bedrock reply unusable")
    e.billed_usage = ({"model": "m", "account": "acc3", "tokens_in": 100, "tokens_out": 16000, "cost_usd": cost,
                       "stop_reason": stop, "provider_requests": 1},)
    return e


def test_billed_unusable_writer_counts_in_spend_cost_and_writer_stop_max_tokens():
    spend = s1_screen.Spend()
    client = FakeClient([])

    def boom(req):
        raise _billed(0.03, "max_tokens")
    client.generate = boom
    with patch.object(s1_screen, "SPEND", spend), patch("shared.llm_client.call_log.record_call_sync") as rec:
        out = s1_screen.run_unit(client, "00000000-0000-0000-0000-000000000000",
                                 {"key": "k", "sample_id": "t1", "arm": "w", "model": "x"}, SAMPLE,
                                 {"brand": {}, "forbidden": [], "background": "allow", "examples": None})
    rec.assert_not_called()                     # the gateway wrote the billed_unusable row itself
    assert spend.usd == 0.03 and spend.calls == 1
    assert out["cost_usd"] == 0.03 and out["calls"][0]["billed_unusable"] is True
    assert out["calls"][0]["stop_reason"] == "max_tokens"
    summary = s1_screen.summarize([out])
    assert summary["w|x"]["writer_stop_max_tokens"] == 1 and summary["w|x"]["usd_total"] == 0.03
    assert summary["w|x"]["billed_unusable_calls"] == 1


def test_meta_is_retried_after_an_unparsable_first_answer():
    client = FakeClient([WRITER])
    replies = iter(["not json at all", json.dumps(GOOD_META)])
    first = client.generate

    def gen(req):
        if not client.replies:
            client.requests.append(req)
            return LLMResponse(content=next(replies), model_used="m", provider="bedrock", cost_usd=0.001)
        return first(req)
    client.generate = gen
    with patch("shared.llm_client.call_log.record_call_sync"):
        out = s1_screen.run_unit(client, "00000000-0000-0000-0000-000000000000",
                                 {"key": "k", "sample_id": "t1", "arm": "w", "model": "x"}, SAMPLE,
                                 {"brand": {}, "forbidden": [], "background": "allow", "examples": None})
    assert out["n_calls"] == 3 and out["meta_final_problems"] == []
    assert "YOUR PREVIOUS ANSWER was not usable" in client.requests[2].user_prompt


def test_aplus_brand_is_loaded_with_a_fixture_source(tmp_path):
    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps({"samples": [{"sample_id": "s1-001", "kind": "s1", "refs": {"tour_id": "t"},
                                           "input": {"tour": TOUR}, "output": None}]}))
    args = s1_screen.parse_args(["--source", "fixture", "--fixture", str(fx), "--aplus-brand", "--arms", "aplus"])

    async def fake_brand():
        return {"brand_system_prompt": "S", "brand_forbidden_words": ["curated"]}
    with patch.object(s1_screen, "load_brand_standalone", fake_brand):
        samples, brand = s1_screen.load_inputs(args)
    assert [s["sample_id"] for s in samples] == ["s1-001"] and brand["brand_system_prompt"] == "S"

    async def no_brand():
        return {}
    with patch.object(s1_screen, "load_brand_standalone", no_brand):
        try:
            s1_screen.load_inputs(args)
            raise AssertionError("an empty brand rule must stop the run")
        except SystemExit as e:
            assert "no A1 brand rule" in str(e)
    args = s1_screen.parse_args(["--source", "fixture", "--fixture", str(fx), "--arms", "w"])
    assert s1_screen.load_inputs(args)[1] == {}


def test_cost_cap_reserves_each_units_worst_case_before_it_starts():
    import threading
    spend = s1_screen.Spend()
    assert spend.reserve(0.3, 1.0) and spend.reserve(0.3, 1.0) and spend.reserve(0.3, 1.0)
    started = []
    t = threading.Thread(target=lambda: started.append(spend.reserve(0.3, 1.0)))
    t.start()
    t.join(0.2)
    assert t.is_alive() and not started       # a 4th unit waits: 4 x 0.3 would pass the cap
    spend.add(0.05)
    spend.release(0.3)
    t.join(2)
    assert started == [True]
    spend.add(0.8)
    assert spend.reserve(0.3, 1.0) is False   # spent 0.85 + 0.3 > 1.0: skipped even alone


def test_unit_margin_uses_catalog_prices_and_a_conservative_fallback():
    from types import SimpleNamespace
    sonnet = SimpleNamespace(price_in_per_mtok=2.0, price_out_per_mtok=10.0)
    with patch("shared.llm_client.catalog.get_model_sync", return_value=sonnet):
        assert s1_screen.unit_margin("sonnet-5-5") == round((24000 * 10 + 36000 * 2) / 1e6, 4)
    with patch("shared.llm_client.catalog.get_model_sync", return_value=None):
        assert s1_screen.unit_margin("sonnet-x") == 0.47 and s1_screen.unit_margin("mystery") == 0.47


def test_run_capped_skips_a_unit_that_cannot_fit_and_releases_after_running():
    spend = s1_screen.Spend()
    spend.add(0.99)
    unit = {"key": "k", "sample_id": "t1", "arm": "w", "model": "x"}
    with patch.object(s1_screen, "unit_margin", return_value=0.05):
        got = s1_screen.run_capped(FakeClient([]), "r", unit, SAMPLE, {}, 1.0, spend)
    assert got["error"] == "skipped_cost_cap"
    spend = s1_screen.Spend()
    with patch.object(s1_screen, "unit_margin", return_value=0.05), \
            patch.object(s1_screen, "run_unit", return_value={"key": "k", "error": None}):
        assert s1_screen.run_capped(FakeClient([]), "r", unit, SAMPLE, {}, 1.0, spend)["error"] is None
    assert spend.reserved == 0.0


def test_resume_retry_errors_flag_parses():
    assert s1_screen.parse_args(["--source", "db", "--resume", "--retry-errors"]).retry_errors is True


def test_figure_coverage_ignores_day_headings():
    from services.quality.s1_facts import build_fact_table
    facts = build_fact_table({**TOUR, "itineraries": TOUR["itineraries"] + " 2 km walk."})
    assert s1_screen.figure_coverage(facts, {"itineraries": "Day 2 — Chilaw\nA walk."}) == 0.0
    assert s1_screen.figure_coverage(facts, {"itineraries": "Day 1 — Chilaw\nA 2 km walk."}) == 1.0
