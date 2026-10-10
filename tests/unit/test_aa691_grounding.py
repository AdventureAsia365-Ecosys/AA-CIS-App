"""AA-691 — A1 grounding: split, numeric + Jev classification, sentence repair, revalidate recheck."""
from types import SimpleNamespace

from services.content_generation import graph as g
from services.content_generation import grounding as gr

TOUR = {
    "name": "Bhutan Cultural Journey",
    "summary": "Seven days across Paro and Thimphu.",
    "itineraries": "Day 1\nArrive in Paro. Visit Rinpung Dzong.\nDay 2\nHike to Taktsang Monastery.",
    "duration": "7 days",
    "highlights": ["Taktsang Monastery", "Rinpung Dzong"],
}

GEN = {
    "subtitle": "Seven days of dzongs and monasteries",
    "summary": "A week across Paro and Thimphu. The palace grounds cover approximately 40 hectares.",
    "highlights": ["Taktsang Monastery perched at 3,120 meters", "Rinpung Dzong at dawn in Paro",
                   "Prayer flags above the Paro valley"],
    "itineraries": "Day 1 — Arrival in Paro\nArrive in Paro and visit Rinpung Dzong. The fortress dates to 1646.\n\n"
                   "Day 2 — Tiger's Nest\nHike to Taktsang Monastery through pine forest.",
}


def _v(zone, p, mode="enforce"):
    return SimpleNamespace(zone=zone, probability=p, mode=mode,
                           enforced=(mode == "enforce" and zone in ("accept", "reject")))


def test_sentence_units_skip_day_titles_and_short_units():
    units = gr.sentence_units(GEN)
    sentences = [u["sentence"] for u in units]
    assert not any(s.startswith("Day ") for s in sentences)
    assert "The fortress dates to 1646." in sentences
    assert "Taktsang Monastery perched at 3,120 meters" in sentences     # one unit per highlight
    assert all(len(s.split()) >= gr.MIN_WORDS for s in sentences)


def test_numeric_hits_found_without_jev():
    res = gr.check_grounding(GEN, TOUR, use_jev=False)
    hits = {v["sentence"]: v for v in res["violations"]}
    assert "The palace grounds cover approximately 40 hectares." in hits
    assert hits["Taktsang Monastery perched at 3,120 meters"]["novel_numbers"] == ["3120"]
    assert "The fortress dates to 1646." in hits
    assert all(v["code"] == "UNSUPPORTED_NUMBER" for v in res["violations"])
    assert res["jev_asked"] == 0


def test_classify_numeric_cleared_only_by_enforced_accept():
    units = [{"field": "summary", "sentence": "It is 13,000 feet high."}]
    numeric = {0: ["13000"]}
    assert gr.classify(units, numeric, {0: _v("accept", 0.97)})["violations"] == []
    # shadow accept, grey, error and no verdict all keep the deterministic hit (numbers fail closed)
    for v in (_v("accept", 0.97, mode="shadow"), _v("grey", 0.6), _v("error", None), None):
        out = gr.classify(units, numeric, {0: v} if v else {})
        assert [x["code"] for x in out["violations"]] == ["UNSUPPORTED_NUMBER"]


def test_classify_claim_needs_enforced_reject_else_note():
    units = [{"field": "itineraries", "sentence": "Amenities include spa services at the lodge."}]
    assert gr.classify(units, {}, {0: _v("reject", 0.05)})["violations"][0]["code"] == "UNSUPPORTED_CLAIM"
    shadow = gr.classify(units, {}, {0: _v("reject", 0.05, mode="shadow")})
    assert shadow["violations"] == [] and len(shadow["notes"]) == 1
    assert gr.classify(units, {}, {0: _v("grey", 0.8)}) == {"violations": [], "notes": []}


def test_subject_key_stable_per_source_and_sentence():
    a = gr.subject_key("src", "A sentence here.")
    assert a == gr.subject_key("src", "A sentence here.")
    assert a != gr.subject_key("src2", "A sentence here.")
    assert a.startswith("claim:")


def test_apply_replacements_replaces_deletes_and_refuses():
    violations = [
        {"field": "summary", "sentence": "The palace grounds cover approximately 40 hectares.",
         "code": "UNSUPPORTED_NUMBER"},
        {"field": "highlights", "sentence": "Taktsang Monastery perched at 3,120 meters", "code": "UNSUPPORTED_NUMBER"},
        {"field": "itineraries", "sentence": "The fortress dates to 1646.", "code": "UNSUPPORTED_NUMBER"},
    ]
    parts = [gr._as_text(TOUR.get(f)) for f in gr.SOURCE_FIELDS]
    reps = {"1": "", "2": "Taktsang Monastery on its cliff above Paro", "3": "The fortress is 400 years old."}
    out, changed = gr.apply_replacements(GEN, violations, reps, parts, ["stunning"])
    assert "40 hectares" not in out["summary"] and out["summary"].startswith("A week across Paro")
    assert out["highlights"][0] == "Taktsang Monastery on its cliff above Paro"
    assert "1646" in out["itineraries"]            # replacement with a new novel number is refused
    assert changed == ["highlights", "summary"]


def test_apply_replacements_refuses_new_forbidden_word_and_keeps_three_highlights():
    violations = [{"field": "highlights", "sentence": "Taktsang Monastery perched at 3,120 meters", "code": "X"}]
    out, changed = gr.apply_replacements(GEN, violations, {"1": "A stunning monastery"}, [], ["stunning"])
    assert changed == [] and out["highlights"] == GEN["highlights"]
    out, changed = gr.apply_replacements(GEN, violations, {"1": ""}, [], [])
    assert changed == [] and len(out["highlights"]) == 3     # would drop below 3 → kept


def test_grounding_node_passthrough_for_tenant_rewrite():
    out = gr.grounding_node({"is_tenant_rewrite": True, "generated": GEN, "tour": TOUR})
    assert out["grounding_ran"] is False and "grounding_violations" not in out


def test_grounding_node_repairs_then_rechecks(monkeypatch):
    monkeypatch.setattr(gr, "judge_units", lambda units, sources: {})
    fixed = {**GEN, "summary": "A week across Paro and Thimphu.",
             "highlights": ["Taktsang Monastery on its cliff", *GEN["highlights"][1:]],
             "itineraries": GEN["itineraries"].replace(" The fortress dates to 1646.", "")}
    calls = []

    def fake_repair(generated, violations, tour, **kw):
        calls.append(len(violations))
        return {"generated": fixed, "fields": ["highlights", "itineraries", "summary"], "cost_usd": 0.002, "error": ""}

    monkeypatch.setattr(gr, "repair", fake_repair)
    out = gr.grounding_node({"generated": GEN, "tour": TOUR, "failure_codes": ["X"], "cost_usd": 0.01})
    assert calls == [3]
    assert out["grounding_ran"] is True and len(out["grounding_found"]) == 3
    assert out["grounding_violations"] == []
    assert out["generated"] == fixed
    assert out["failure_codes"] == ["X"]
    assert abs(out["cost_usd"] - 0.012) < 1e-9


def test_grounding_node_keeps_unrepaired_violations(monkeypatch):
    monkeypatch.setattr(gr, "judge_units", lambda units, sources: {})
    monkeypatch.setattr(gr, "repair", lambda generated, violations, tour, **kw:
                        {"generated": generated, "fields": [], "cost_usd": 0.0, "error": "boom"})
    out = gr.grounding_node({"generated": GEN, "tour": TOUR, "failure_codes": []})
    assert len(out["grounding_violations"]) == 3
    assert out["failure_codes"] == ["UNSUPPORTED_NUMBER"]


def test_revalidate_sends_unfixed_grounding_violation_to_manual_check():
    state = {"grounding_ran": True, "fix_pass_applied": False, "brand_audit_status": "pass",
             "grounding_violations": [{"code": "UNSUPPORTED_NUMBER", "field": "summary", "sentence": "s"}],
             "failure_codes": []}
    out = g.revalidate_node(state)
    assert out["brand_audit_status"] == "manual_check"
    assert out["failure_codes"] == ["UNSUPPORTED_NUMBER"]


def test_revalidate_rechecks_after_flag_fix(monkeypatch):
    monkeypatch.setattr(g, "validate_node", lambda st: {**st, "quality_score": 9.0, "failure_codes": []})
    monkeypatch.setattr(g, "judge_node", lambda st: {**st, "quality_score": 9.0})
    monkeypatch.setattr(g, "regrounding", lambda st: {"violations": [{"code": "UNSUPPORTED_CLAIM", "field": "summary",
                                                                      "sentence": "s"}], "notes": []})
    monkeypatch.setattr(g, "repair_and_recheck", lambda gen, v, tour, **kw: {
        "generated": gen, "violations": v, "notes": None, "fields": [], "cost_usd": 0.0})
    out = g.revalidate_node({"grounding_ran": True, "fix_pass_applied": True, "brand_audit_status": "flagged",
                             "grounding_violations": [], "failure_codes": []})
    assert out["revalidate_passed"] is False
    assert out["brand_audit_status"] == "manual_check"
    assert out["failure_codes"] == ["UNSUPPORTED_CLAIM"]
    monkeypatch.setattr(g, "regrounding", lambda st: {"violations": [], "notes": []})
    ok = g.revalidate_node({"grounding_ran": True, "fix_pass_applied": True, "brand_audit_status": "flagged",
                            "grounding_violations": [], "failure_codes": []})
    assert ok["brand_audit_status"] == "fixed" and ok["revalidate_passed"] is True


def test_revalidate_untouched_when_grounding_did_not_run():
    out = g.revalidate_node({"fix_pass_applied": False, "brand_audit_status": "pass", "failure_codes": []})
    assert out["brand_audit_status"] == "pass"


def test_compiled_graphs_route_brand_audit_through_grounding():
    for build in (g.build_graph, g.build_graph_from_generated):
        edges = {(e.source, e.target) for e in build().get_graph().edges}
        assert ("brand_audit", "grounding") in edges and ("grounding", "flag_fix") in edges
        assert ("brand_audit", "flag_fix") not in edges


def test_judge_units_fails_open_on_event_loop():
    import asyncio

    async def inner():
        return gr.judge_units([{"field": "summary", "sentence": "one two three four"}], {0: "src"})

    assert asyncio.run(inner()) == {}


def test_source_number_parts_derive_conversions_and_split_glued_numbers():
    tour = {"itineraries": "Auto Rickshaw Jhansi - Orchha 1h30m\nLocal Bus Jeonju - Busan 3h260km\n"
                           "Train at 12.30 PM. Drive (2hr to 2hr 30min). Walk 90 min."}
    parts = gr.source_number_parts(tour)
    for sentence in ("A 90-minute auto rickshaw ride to Orchha.",
                     "A three-hour journey of 260 kilometers.",
                     "Depart at 12:30 PM toward Bandarawela.",
                     "A 2- to 2.5-hour drive to Ho Chi Minh City.",
                     "A 1.5-hour walk through the village."):
        assert gr.find_novel_numeric_claims(sentence, parts) == [], sentence
    assert gr.find_novel_numeric_claims("The fort covers 40 hectares.", parts) == ["40"]


def test_revalidate_second_repair_clears_figures_reintroduced_by_flag_fix(monkeypatch):
    monkeypatch.setattr(g, "validate_node", lambda st: {**st, "quality_score": 9.0, "failure_codes": []})
    monkeypatch.setattr(g, "judge_node", lambda st: {**st, "quality_score": 9.0})
    v = [{"code": "UNSUPPORTED_NUMBER", "field": "itineraries", "sentence": "Drive 75 kilometers."}]
    monkeypatch.setattr(g, "regrounding", lambda st: {"violations": v, "notes": []})
    calls = []

    def fake(gen, violations, tour, **kw):
        calls.append(len(violations))
        return {"generated": {**gen, "itineraries": "Drive to Punakha."}, "violations": [], "notes": [],
                "fields": ["itineraries"], "cost_usd": 0.002}

    monkeypatch.setattr(g, "repair_and_recheck", fake)
    out = g.revalidate_node({"grounding_ran": True, "fix_pass_applied": True, "brand_audit_status": "flagged",
                             "grounding_violations": [], "failure_codes": [], "cost_usd": 0.01,
                             "generated": {"itineraries": "Drive 75 kilometers."}, "tour": {}})
    assert calls == [1]
    assert out["brand_audit_status"] == "fixed" and out["grounding_violations"] == []
    assert out["generated"]["itineraries"] == "Drive to Punakha."
    assert abs(out["cost_usd"] - 0.012) < 1e-9
