"""AA-1049 W3 (root ADR 0020, S1 train) — the S1 control plane behind S1_CONTROL
(services/content_generation/s1_train.py).

1. switch off = main byte for byte: the full-chain harness trace (every model request with its prompt sha, the stored
   page, codes, statuses, the final contract, the publish gate) equals the golden RECORDED ON MAIN before any code of
   the train existed (tests/unit/fixtures/aa1049_s1_parity_golden.json, first commit of the branch), for unset,
   ``graph`` and unknown values. Never re-record it to make this test pass: a PR that changes the A1 path on purpose
   re-records it on its own commit (python -m tests.unit.fixtures.aa1049_s1_harness <golden>).
2. (a) contract composition: day headings numbered by code from the plan, the writer's fact line replaced by the
   source day's verbatim lines, an extra / duplicate day dropped, a dropped day left missing, the title fit and the
   code meta template.
3. (b) the S1 route rows: the lint (one row per S1 code of the taxonomy) and the schema.
4. (c) local fix and the contract re-check; (d) the judge off the live path; (e) the class router.
5. the free replay: defaults vs a replay RECORDED ON MAIN = 0 diffs; the switch on = declared diffs only.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from services.acp_content_writing import route_table as rt
from services.content_generation import s1_train
from services.content_generation import s1_writer_v2 as v2
from services.quality import registry as qr
from services.quality.s1_facts import build_fact_table
from tests.unit.fixtures import aa1049_s1_harness as h

FIX = Path(__file__).resolve().parent / "fixtures"
GOLDEN = json.loads((FIX / "aa1049_s1_parity_golden.json").read_text(encoding="utf-8"))
ON = {"S1_CONTROL": "contract"}


def _tour():
    tour = v2.with_agency_fields(h.TOUR, h.TOUR_AGENCY)
    facts = build_fact_table(tour)
    return tour, facts, v2.day_plan(tour, facts)


# ── 1. switch off = main ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(h.SCENARIOS))
def test_switch_unset_equals_the_main_golden(name):
    assert h.run(name) == GOLDEN[name]


@pytest.mark.parametrize("name", sorted(h.SCENARIOS))
def test_switch_graph_equals_the_main_golden(name):
    assert h.run(name, env={"S1_CONTROL": "graph"}) == GOLDEN[name]


@pytest.mark.parametrize("value", ["", "on", "true", "1", "contracts", "con tract", "linear"])
def test_unknown_values_are_main(value):
    for name in ("meal_in_prose", "day_dropped"):
        assert h.run(name, env={"S1_CONTROL": value}) == GOLDEN[name], (value, name)


def test_mode_reader(monkeypatch):
    for raw, want in (("contract", "contract"), (" CONTRACT ", "contract"), ("graph", "graph"), ("on", "graph"),
                      ("", "graph")):
        monkeypatch.setenv("S1_CONTROL", raw)
        assert s1_train.mode() == want, raw
    monkeypatch.delenv("S1_CONTROL")
    assert s1_train.mode() == "graph"


def test_active_only_for_an_a1_v2_draft(monkeypatch):
    monkeypatch.setenv("S1_CONTROL", "contract")
    assert s1_train.active({"writer_version": "v2"})
    assert not s1_train.active({"writer_version": "v2", "is_tenant_rewrite": True})
    assert not s1_train.active({"writer_version": "v1"})
    assert not s1_train.active({"writer_version": "v2", "batch_text": "x"})
    monkeypatch.setenv("S1_CONTROL", "graph")
    assert not s1_train.active({"writer_version": "v2"})


def test_config_fp_records_the_switch_only_when_it_changes_the_path():
    from services.writing_memory import config_fp as cf
    assert "S1_CONTROL" in cf.MODE_FLAGS["a1"]
    assert cf.mode_flags("a1", {"S1_CONTROL": "contract"}).get("S1_CONTROL") == "contract"
    for v in ("graph", "on", "", "contract2"):
        assert "S1_CONTROL" not in cf.mode_flags("a1", {"S1_CONTROL": v}), v


# ── 2. (a) contract composition ──────────────────────────────────────────────────────────────────────


def test_compose_numbers_days_by_plan_and_replaces_the_writer_fact_line():
    tour, facts, plan = _tour()
    items = h._days({2: {"body": "Walk up Corran Ridge to the chapel.\nDistance: 40 km. Overnight: Elsewhere. "
                                 "Meals: breakfast, lunch, dinner."}})
    items = [items[1], items[0], items[2], {"day": 9, "title": "Extra", "body": "Not in the plan."},
             {"day": 2, "title": "Again", "body": "A duplicate."}]
    itin, rep = s1_train.compose_itineraries(items, tour, facts, plan)
    heads = [ln for ln in itin.splitlines() if ln.startswith("Day ")]
    assert heads == ["Day 1 — Arrive in Velmora", "Day 2 — Corran Ridge walk", "Day 3 — Velmora market and departure"]
    assert "40 km" not in itin and "Elsewhere" not in itin and "dinner" not in itin
    assert "Distance: 12 km\nOvernight: Velmora\nMeals: B, L" in itin           # verbatim source lines of day 2
    assert rep["extra"] == [9] and rep["duplicate"] == [2] and rep["missing"] == []
    assert rep["fact_lines"] == "source" and rep["writer_fact_lines_removed"] == 3


def test_compose_leaves_a_dropped_day_missing_so_validate_still_fires():
    tour, facts, plan = _tour()
    itin, rep = s1_train.compose_itineraries([d for d in h._days() if d["day"] != 2], tour, facts, plan)
    assert rep["missing"] == [2] and "Day 2 —" not in itin


def test_compose_keeps_a_range_object_on_a_ranged_source():
    tour = {**h.TOUR, "duration": "4 days",
            "itineraries": "Day 1: Arrive in Velmora.\nOvernight: Velmora\nDays 2-3: Corran Ridge camp, two nights."
                           "\nMeals: B, L, D\nDay 4: Depart."}
    facts = build_fact_table(tour)
    plan = v2.day_plan(tour, facts)
    items = [{"day": 1, "title": "Arrive", "body": "Arrive in Velmora."},
             {"day": "2-3", "title": "Corran Ridge camp", "body": "Two nights at the ridge camp."},
             {"day": 4, "title": "Depart", "body": "Leave Velmora."}]
    itin, rep = s1_train.compose_itineraries(items, tour, facts, plan)
    assert "Day 2-3 — Corran Ridge camp" in itin and rep["missing"] == [] and rep["days"] == 3
    assert itin.count("Meals: B, L, D") == 1


def test_compose_keeps_the_writer_fact_line_when_the_source_has_no_day_blocks():
    tour = {**h.TOUR, "itineraries": "Three walking days around Velmora and Corran Ridge."}
    facts = build_fact_table(tour)
    plan = v2.day_plan(tour, facts)
    assert plan["mode"] == "duration_days"
    itin, rep = s1_train.compose_itineraries(h._days(), tour, facts, plan)
    assert rep["fact_lines"] == "writer" and "Overnight: Velmora. Meals: dinner." in itin


def test_compose_without_a_usable_list_leaves_the_page_alone():
    tour, facts, plan = _tour()
    assert s1_train.compose_itineraries("Day 1 — x", tour, facts, plan) == (None, {"composed": False})


@pytest.mark.parametrize("body, want, removed", [
    ("Walk to the chapel.\nOvernight: Velmora. Meals: dinner.", "Walk to the chapel.", 1),
    ("Walk to the chapel. Overnight: Velmora. Meals: dinner.", "Walk to the chapel.", 1),
    ("Distance: 12 km", "", 1),
    ("The meals here are simple: rice and beans.", "The meals here are simple: rice and beans.", 0),
    ("Walk to the chapel.", "Walk to the chapel.", 0),
    ("Walk to the chapel: two hours on a forest path.", "Walk to the chapel: two hours on a forest path.", 0),
    ("Cross the pass.\nDistance: 12.5 km. Driving time: 3 hours.", "Cross the pass.", 1),
    ("- Meals included: breakfast", "", 1),
])
def test_strip_writer_fact_lines(body, want, removed):
    assert s1_train.strip_writer_fact_lines(body) == (want, removed)


def test_meta_template_lands_in_band_and_passes_the_code_checks():
    from services.content_generation.seo_meta_utils import SEO_META_MAX, SEO_META_MIN
    tour, facts, _plan = _tour()
    page = h.writer_page()
    meta = s1_train.meta_template(page, tour, facts)
    assert meta and SEO_META_MIN <= len(meta) <= SEO_META_MAX and meta.endswith(".")
    assert v2.meta_problems({"seo_title": h._TITLE, "seo_meta": meta}, tour, facts, ["breathtaking"]) == []
    assert "lunch at a farm kitchen" in meta          # a common-word opener reads as a list item
    assert s1_train.meta_template({**page, "highlights": ["Walk"]}, tour, facts) is None


def test_compose_page_fits_the_title_and_uses_the_template_only_for_a_failing_meta():
    tour, facts, plan = _tour()
    page = {**v2.assemble_page(h.writer_page()), "seo_title": h._TITLE + " and the Old Market of Velmora Town",
            "seo_meta": h.META_LONG}
    out, rep = s1_train.compose_page(page, h.writer_page(), tour, facts, plan, ["breathtaking"], ["too long"])
    assert len(out["seo_title"]) <= 60 and rep["seo_title"] == "fitted" and rep["meta"] == "template"
    out2, rep2 = s1_train.compose_page({**page, "seo_meta": h.META_OK}, h.writer_page(), tour, facts, plan,
                                       ["breathtaking"], [])
    assert out2["seo_meta"] == h.META_OK and rep2["meta"] == "writer"


# ── 3. (b) the S1 route rows ─────────────────────────────────────────────────────────────────────────


def test_s1_lint_one_row_per_s1_code():
    assert rt.s1_schema_errors() == [] and rt.s1_coverage_errors() == []
    assert set(rt.S1_TABLE) == set(qr.a1_codes())
    assert {r.action for r in rt.S1_TABLE.values()} == set(rt.S1_ACTIONS)


def test_s1_lint_catches_a_missing_an_extra_and_a_doubled_row():
    doc = copy.deepcopy(rt.mechanics())
    rows = doc["s1"]["rows"]
    doc["s1"]["rows"] = [r for r in rows if r["code"] != "META_CUT"] + [dict(rows[0]), {"code": "F1_grounding",
                                                                                          "action": "hold"}]
    errors = rt.s1_coverage_errors(doc)
    assert any("META_CUT" in e and "no route row" in e for e in errors)
    assert any("F1_grounding" in e and "not an S1 code" in e for e in errors)
    assert any("2 route rows" in e for e in errors)
    assert any("exactly one row per code" in e for e in rt.s1_schema_errors(doc))


@pytest.mark.parametrize("row, needle", [
    ({"code": "META_CUT", "action": "delete"}, "action must be one of"),
    ({"code": "META_CUT", "action": "cut", "parse": "x"}, "unknown keys"),
    ({"action": "cut"}, "a code is required"),
])
def test_s1_schema_refuses_a_bad_row(row, needle):
    doc = copy.deepcopy(rt.mechanics())
    doc["s1"]["rows"] = [row]
    assert any(needle in e for e in rt.s1_schema_errors(doc))


def test_s1_route_defaults_to_hold_and_routes_by_class():
    assert rt.s1_route("NOT_A_CODE").action == rt.HOLD
    assert rt.s1_route(None).action == rt.HOLD
    for code, r in rt.S1_TABLE.items():
        if r.action == rt.REWRITE:   # only day structure and truncation redraft the page
            assert set(r.classes) <= {"struct.day_structure", "struct.truncated"}, code
    assert rt.s1_route("ITINERARY_DAY_COUNT_MISMATCH").action == rt.REWRITE
    assert rt.s1_route("ITINERARY_MEAL_TIME_INVENTED").action == rt.CUT
    assert rt.s1_route("SEO_META_TOO_LONG").action == rt.REGENERATE_SECTION
    assert rt.s1_route("FACT_CHECK_MANUAL_CHECK").action == rt.HOLD
    # the hard codes of the publish gate all have a row that names how they are cleared or held
    from services.content_generation.graph import _HARD_BLOCK_CODES, V2_HARD_BLOCK_CODES
    assert (_HARD_BLOCK_CODES | V2_HARD_BLOCK_CODES) <= set(rt.S1_TABLE)


# ── 4. (c) local fix, (d) judge, (e) router — through the production graph ───────────────────────────


def test_on_meal_in_prose_is_cut_by_code_without_a_model_call():
    t = h.run("meal_in_prose", env=ON)
    stages = [c[0] for c in t["llm"]]
    assert "s1_flag_fix" not in stages and t["unexpected"] == []
    r = t["result"]
    assert "Lunch is included at the station cafe" not in r["generated"]["itineraries"]
    assert r["fix_pass_applied"] and r["brand_audit_codes"] == [] and r["publishable"]


def test_on_wrong_writer_fact_line_is_replaced_by_the_source_line():
    t = h.run("fact_line_wrong", env=ON)
    r = t["result"]
    assert [c[0] for c in t["llm"]] == ["s1_generate", "s1_generate"]
    assert "dinner" not in r["generated"]["itineraries"].split("Day 3")[0].split("Day 2")[1]
    assert r["brand_audit_codes"] == [] and r["publishable"]


def test_on_meta_out_of_band_becomes_the_code_template_never_a_re_repair():
    from services.content_generation.seo_meta_utils import SEO_META_MAX, SEO_META_MIN
    t = h.run("meta_long", env=ON)
    r = t["result"]
    assert [c[0] for c in t["llm"]] == ["s1_generate"] * 3          # writer, meta, one meta retry; no s1_flag_fix
    assert SEO_META_MIN <= len(r["generated"]["seo_meta"]) <= SEO_META_MAX and r["publishable"]
    assert GOLDEN["meta_long"]["result"]["publishable"] is False      # main held it after 4 repair calls


def test_on_dropped_day_redrafts_once():
    t = h.run("day_dropped", env=ON)
    r = t["result"]
    assert [c[0] for c in t["llm"]] == ["s1_generate"] * 4            # 2 drafts (writer + meta each)
    assert r["retry_count"] == 1 and "ITINERARY_DAY_COUNT_MISMATCH" not in r["failure_codes"] and r["publishable"]
    assert GOLDEN["day_dropped"]["result"]["publishable"] is False    # main: saturated score, no redraft


def test_on_dropped_day_twice_goes_to_the_hitl_exit_after_two_drafts(monkeypatch):
    sc = copy.deepcopy(h.SCENARIOS["day_dropped"])
    sc["script"]["s1_generate"] = sc["script"]["s1_generate"][:2] * 2  # both drafts drop day 2
    monkeypatch.setitem(h.SCENARIOS, "day_dropped_twice", sc)
    t = h.run("day_dropped_twice", env=ON)
    r = t["result"]
    assert [c[0] for c in t["llm"]] == ["s1_generate"] * 4
    assert r["exit_path"] == "hitl" and r["retry_count"] == 1 and not r["publishable"]
    assert "ITINERARY_DAY_COUNT_MISMATCH" in r["failure_codes"]


def test_on_grounding_keeps_one_source_aware_repair_and_no_second_round():
    t = h.run("number", env=ON)
    assert [c[0] for c in t["llm"]].count("s1_flag_fix") == 1          # the grounding sentence repair only
    assert "412" not in t["result"]["generated"]["summary"]


def test_on_elephant_is_kept_for_a_human_with_no_edit():
    t = h.run("elephant", env=ON)
    r = t["result"]
    assert r["brand_audit_status"] == "manual_check" and not r["publishable"] and not r["fix_pass_applied"]
    assert r["final_contract"]["verdict"] == "policy_hold"


def test_on_the_judge_stays_off_even_with_the_break_glass(monkeypatch):
    t = h.run("clean", env={**ON, "S1_JUDGE_LIVE": "on"})
    assert "s1_judge" not in [c[0] for c in t["llm"]] and t["unexpected"] == []


def test_on_the_train_report_reaches_the_result_and_the_version_metadata():
    """The report (state["s1_train"]) is a ContentState key, so LangGraph keeps it to the end of the graph,
    _rewrite_tour returns it and the version metadata stores it (the AA-209 gotcha: an undeclared key is dropped)."""
    from api.routers.admin_pipeline import _build_generated_metadata
    t = h.run("meal_in_prose", env=ON, extra_keys=("s1_train",))
    rep = t["result"]["s1_train"]
    assert rep["compose"]["composed"] is True
    assert rep["local_fix"]["routes"] and rep["local_fix"]["left"] == []
    meta = _build_generated_metadata({"s1_train": rep}, brand_rule_id=None, brand_name=None, seo_mode="disabled",
                                     model_used="m", llm_cost_usd=0.0, dataforseo_used=False)
    assert meta["s1_train"] == rep


@pytest.mark.parametrize("env", [None, {"S1_CONTROL": "graph"}])
def test_off_the_train_report_is_absent(env):
    from api.routers.admin_pipeline import _build_generated_metadata
    t = h.run("meal_in_prose", env=env, extra_keys=("s1_train",))
    assert t["result"]["s1_train"] is None
    meta = _build_generated_metadata({}, brand_rule_id=None, brand_name=None, seo_mode="disabled",
                                     model_used="m", llm_cost_usd=0.0, dataforseo_used=False)
    assert "s1_train" not in meta


def test_router_unit():
    base = {"writer_version": "v2", "quality_score": 10.0}
    assert s1_train.route_after_judge({**base, "failure_codes": []}) == "done"
    assert s1_train.route_after_judge({**base, "failure_codes": ["ITINERARY_DAY_COUNT_MISMATCH"]}) == "retry"
    assert s1_train.route_after_judge({**base, "failure_codes": ["ITINERARY_DAY_COUNT_MISMATCH"],
                                       "retry_count": 1}) == "hitl"
    assert s1_train.route_after_judge({**base, "failure_codes": ["GENERIC_AI_WORDING"]}) == "done"   # not enforced
    assert s1_train.route_after_judge({**base, "quality_score": 3.0, "failure_codes": []}) == "hitl"


def test_local_fix_holds_what_no_code_step_clears():
    st = {"writer_version": "v2", "generated": {"name": "X", "subtitle": "A, B, C"}, "tour": h.TOUR,
          "failure_codes": [], "brand_audit_codes": ["SUBTITLE_CITY_LIST"], "brand_audit_status": "flagged"}
    out = s1_train.local_fix_node(st)
    assert out["brand_audit_status"] == "manual_check" and out["fix_pass_applied"] is False
    assert out["s1_train"]["local_fix"]["left"] == ["SUBTITLE_CITY_LIST"]


def test_local_fix_reverts_a_field_the_repair_contract_refuses(monkeypatch):
    from services.content_generation import graph

    def revert(state, before, *, detector):
        state["generated"].clear()
        state["generated"].update(before)
        return {**state, "degraded": [{"name": "repair_contract", "detector": detector}]}
    monkeypatch.setattr(graph, "_contract_after_edit", revert)
    itin = ("Day 1 — Arrive in Velmora\nArrive in Velmora. Lunch is included at the canal cafe.\nMeals: D")
    st = {"writer_version": "v2", "generated": {"name": "X", "itineraries": itin}, "tour": h.TOUR,
          "failure_codes": [], "brand_audit_codes": ["ITINERARY_MEAL_TIME_INVENTED"], "brand_audit_status": "flagged"}
    out = s1_train.local_fix_node(st)
    assert out["generated"]["itineraries"] == itin and out["fix_pass_applied"] is False
    assert out["degraded"][0]["detector"] == "s1_local_fix"


def test_meal_cut_keeps_one_sentence_per_day_and_the_source_lines():
    itin = ("Day 1 — Arrive in Velmora\nLunch is included at the canal cafe.\nMeals: D\n\n"
            "Day 3 — Depart\nA free morning. Dinner is served at 7pm on the terrace.\nMeals: B")
    out, cut = s1_train._cut_meal_sentences({"itineraries": itin}, h.TOUR)
    assert cut == ["Dinner is served at 7pm on the terrace."]
    assert "Lunch is included at the canal cafe." in out["itineraries"]     # the only sentence of day 1 stays
    assert "Meals: D" in out["itineraries"] and "Meals: B" in out["itineraries"]


# ── 5. the free replay ───────────────────────────────────────────────────────────────────────────────


def _replay(monkeypatch, tmp_path, env: dict, opt_in: bool):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "replay_aa1049_s1", Path(__file__).resolve().parents[2] / "scripts" / "quality" / "replay_aa1049_s1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    masters = tmp_path / "m.json"
    masters.write_text(json.dumps(h.replay_masters()), encoding="utf-8")
    for k in mod.S1_SWITCHES:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    out = tmp_path / "out.json"
    argv = ["--masters", str(masters), "--base", str(FIX / "aa1049_s1_replay_main_base.json"), "--out", str(out),
            "--report", str(tmp_path / "r.md")] + (["--opt-in"] if opt_in else [])
    code = mod.main(argv)
    return code, json.loads(out.read_text(encoding="utf-8")), mod


def test_replay_defaults_equal_a_replay_recorded_on_main(monkeypatch, tmp_path):
    code, doc, _ = _replay(monkeypatch, tmp_path, {}, opt_in=False)
    d = doc["diff"]
    assert code == 0 and d["declared"] == {} and d["undeclared"] == 0 and d["miss"] == 0 and d["missing"] == []
    assert d["equal"] == len(h.replay_masters()["masters"])


def test_replay_switch_on_shows_only_declared_diffs(monkeypatch, tmp_path):
    code, doc, mod = _replay(monkeypatch, tmp_path, ON, opt_in=True)
    d = doc["diff"]
    assert d["undeclared"] == 0 and d["missing"] == [] and code in (0, 3)
    assert set(d["declared"]) <= set(mod.DECLARED) and d["declared"].get("D-s1-compose")
    kinds = {r["tour_id"]: r["kind"] for r in d["rows"]}
    assert kinds["m-fact-line"] == "miss" and kinds["m-meta-long"] == "miss"   # main met a flag_fix cassette miss
    assert doc["summary"]["misses"] == {} and doc["summary"]["llm_calls"].get("s1_flag_fix:miss") is None


def test_replay_switch_on_without_opt_in_is_undeclared(monkeypatch, tmp_path):
    code, doc, _ = _replay(monkeypatch, tmp_path, ON, opt_in=False)
    assert code == 1 and doc["diff"]["undeclared"] > 0


def test_replay_never_declares_a_looser_outcome():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "replay_aa1049_s1", Path(__file__).resolve().parents[2] / "scripts" / "quality" / "replay_aa1049_s1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    base = {"tour_id": "t", "publishable": False, "contract_verdict": "review", "misses": [], "llm_calls": {}}
    branch = {**base, "publishable": True, "contract_verdict": "publish"}
    assert mod.classify(base, branch, declare=True)[0] == "undeclared"
    assert mod.classify({**base, "misses": ["s1_flag_fix"]}, branch, declare=True)[0] == "miss"
    stricter = {**base, "publishable": False, "brand_audit_status": "manual_check"}
    assert mod.classify({**base, "brand_audit_status": "fixed"}, stricter, declare=True) == (
        "declared", ["D-s1-status"], ["brand_audit_status"])
    more_calls = {**base, "llm_calls": {"s1_flag_fix:writer": 1}}
    assert mod.classify(base, more_calls, declare=True)[0] == "undeclared"


def test_replay_cassette_splits_the_stored_itinerary_back_into_writer_days():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "replay_aa1049_s1", Path(__file__).resolve().parents[2] / "scripts" / "quality" / "replay_aa1049_s1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    items = mod.writer_items("Day 1 — Arrive\nLine one.\nMeals: D\n\nDay 2-3 — Ridge camp\nTwo nights.")
    assert items == [{"day": "1", "title": "Arrive", "body": "Line one.\nMeals: D"},
                     {"day": "2-3", "title": "Ridge camp", "body": "Two nights."}]
