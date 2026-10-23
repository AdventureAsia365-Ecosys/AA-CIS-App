"""AA-645 (S249) — the offline brand-fit re-measure (scripts/aa645/brandfit_measure.py + brandfit_analyze.py) and the
shadow harness (scripts/aa645/shadow.py + shadow_report.py): the pure parts, no DB, no LLM."""
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.aa645 import brandfit_analyze as an
from scripts.aa645 import brandfit_measure as bm
from scripts.aa645 import shadow as sh
from scripts.aa645 import shadow_report as sr
from services.content_generation import brand_fit as bf
from services.content_generation import brand_fit_v2 as v2

ROOT = Path(__file__).resolve().parents[2]
TOUR = {"name": "Nam Song Kayak", "summary": "Kayak the Nam Song.", "description": "",
        "highlights": ["Nam Song"], "itineraries": "Day 1\nKayak 10 km on the Nam Song. Lunch.", "country": "Laos",
        "duration": "1 day", "inclusions": "Lunch", "exclusions": ""}
OUT = {"name": "Nam Song Kayak — 1 Day", "subtitle": "Kayak 10 km.", "summary": "A day on the Nam Song.",
       "highlights": ["Nam Song"], "itineraries": "Day 1 — Nam Song\nKayak 10 km. Meals: lunch.", "seo_title": "x",
       "seo_meta": "y"}


def _unit(sample, arm, model, **kw):
    return {"key": f"{sample}__{arm}__{model}", "sample_id": sample, "arm": arm, "model": model,
            "tour_id": "t-" + sample,
            "tour": TOUR, "output": OUT, **kw}


def _tarball(tmp_path, units):
    p = tmp_path / "pages.tgz"
    body = json.dumps(units).encode()
    with tarfile.open(p, "w:gz") as tf:
        info = tarfile.TarInfo("run/pages.json")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return str(p)


# ── brandfit_measure ─────────────────────────────────────────────────────────────────────────────────────────


def test_load_units_filters_and_orders_by_sample_rubric_arm(tmp_path):
    units = [_unit("s2", "w", "sonnet-5-5"), _unit("s1", "aplus", "haiku"), _unit("s1", "w", "sonnet-5-5"),
             _unit("s1", "aplus2", "haiku"), _unit("s1", "e", "gpt-6-luna", error="boom"),
             {**_unit("s3", "w", "sonnet-5-5"), "tour": None}]
    got = bm.load_units(_tarball(tmp_path, units), {"aplus|haiku", "w|sonnet-5-5", "e|gpt-6-luna"}, ["v1", "v2"])
    assert [u["unit_key"] for u in got] == [
        "s1__aplus__haiku|v1", "s1__w__sonnet-5-5|v1", "s1__aplus__haiku|v2", "s1__w__sonnet-5-5|v2",
        "s2__w__sonnet-5-5|v1", "s2__w__sonnet-5-5|v2"]
    assert len(bm.load_units(_tarball(tmp_path, units), {"w|sonnet-5-5"}, ["v2"], limit=1)) == 1


def test_load_units_reads_a_plain_json_file(tmp_path):
    p = tmp_path / "pages.json"
    p.write_text(json.dumps([_unit("s1", "w", "sonnet-5-5")]))
    assert len(bm.load_units(str(p), {"w|sonnet-5-5"}, ["v1"])) == 1


def test_spend_reserves_per_rubric_and_refuses_past_the_cap():
    s = bm.Spend(0.03)
    r = s.reserve("v2")
    assert r == pytest.approx(3 * bm.INITIAL_EST_CALL_USD["v2"])
    assert s.reserve("v2") is None                              # 0.018 + 0.018 > 0.03
    s.settle("v2", r, 0.004, 1)
    assert s.est_call("v2") == pytest.approx(bm.INITIAL_EST_CALL_USD["v2"])   # never below the start estimate
    assert s.usd == pytest.approx(0.004) and s.reserved == pytest.approx(0)


def _v2_result(score, caps=()):
    return bf.BrandFitResult(brand_fit_score=score, cross_brand_distinct=8.0, mission_present=True,
                             feedback="drop the generic line", judge_score=score, model_used="gpt-5.6-luna",
                             cost_usd=0.005, input_tokens=3000, output_tokens=100, stop_reason="end_turn",
                             account="acc3", fallback_used=False, rubric="v2", caps=tuple(caps),
                             unsupported=({"quote": "at sunset", "why": "-"},) if caps else (), raw_brand_fit=8.0)


def test_score_page_v2_passes_the_source_and_logs_the_rubric():
    unit = {"page": _unit("s1", "w", "sonnet-5-5"), "rubric": "v2", "unit_key": "s1__w__sonnet-5-5|v2"}
    with patch.object(bf, "score_brand_fit", return_value=_v2_result(6.0, ("unsupported",))) as score, \
            patch("services.content_generation.judge_node._write_judge_rows") as rows:
        out = bm.score_page(unit, {"brand_core_idea": "x"}, "gpt-5.6-luna")
    kw = score.call_args.kwargs
    assert kw["rubric"] == "v2" and kw["source"] is TOUR and kw["stage"] == "adhoc_aa645"
    assert kw["model_override"] == "gpt-5.6-luna"
    sig = rows.call_args.args[0][0]["quality_signal"]
    assert sig["measurement"] == bm.MEASUREMENT and sig["rubric"] == v2.RUBRIC_V2_VERSION
    assert sig["aa645_key"] == "s1__w__sonnet-5-5|v2" and sig["caps"] == ["unsupported"]
    assert out["ok"] and out["retry"] and out["rubric"] == "v2" and out["unsupported"] == 1
    assert out["rubric_version"] == v2.RUBRIC_V2_VERSION and out["raw_brand_fit"] == 8.0


def test_score_page_v1_is_the_s248_call():
    unit = {"page": _unit("s1", "aplus", "haiku"), "rubric": "v1", "unit_key": "s1__aplus__haiku|v1"}
    res = bf.BrandFitResult(brand_fit_score=8.0, cross_brand_distinct=8.0, mission_present=True, feedback="",
                            judge_score=8.0, model_used="m", cost_usd=0.002, input_tokens=1, output_tokens=1,
                            stop_reason="end_turn", account=None, fallback_used=False)
    with patch.object(bf, "score_brand_fit", return_value=res) as score, \
            patch("services.content_generation.judge_node._write_judge_rows") as rows:
        out = bm.score_page(unit, {"brand_core_idea": "x"}, "gpt-5.6-luna")
    assert "rubric" not in score.call_args.kwargs and "source" not in score.call_args.kwargs
    assert rows.call_args.args[0][0]["quality_signal"]["rubric"] == "v1" and out["retry"] is False


def test_score_page_logs_a_billed_failure():
    unit = {"page": _unit("s1", "w", "sonnet-5-5"), "rubric": "v2", "unit_key": "k|v2"}
    err = ValueError("bad json")
    err.billed_calls = [{"model": "m", "cost_usd": 0.005}]
    with patch.object(bf, "score_brand_fit", side_effect=err), \
            patch("shared.llm_client.call_log.record_billed_failure_sync") as fail:
        out = bm.score_page(unit, {}, "m")
    assert out["ok"] is False and out["cost_usd"] == pytest.approx(0.005)
    assert fail.call_args.kwargs["stage"] == "adhoc_aa645"


def test_summarise_splits_rubric_and_arm():
    rows = [{"rubric": "v2", "arm_model": "w|x", "ok": True, "brand_fit": 8.0, "retry": False, "tie_band": False,
             "feedback_says_generic": False, "caps": [], "unsupported": 0, "calls": 1, "cost_usd": 0.005},
            {"rubric": "v2", "arm_model": "w|x", "ok": True, "brand_fit": 6.0, "retry": True, "tie_band": False,
             "feedback_says_generic": True, "caps": ["unsupported"], "unsupported": 2, "calls": 3, "cost_usd": 0.015},
            {"rubric": "v1", "arm_model": "w|x", "ok": False, "calls": 1, "cost_usd": 0.002}]
    s = bm.summarise(rows)
    assert s["v2"]["w|x"]["retry_rate"] == 0.5 and s["v2"]["w|x"]["capped"] == 1
    assert s["v2"]["w|x"]["unsupported_mean"] == 1.0 and s["v1"]["w|x"]["failed"] == 1


def test_dry_run_offline_builds_a_prompt_per_rubric(tmp_path, capsys):
    brand = tmp_path / "brand.json"
    brand.write_text(json.dumps({"brand_core_idea": "x", "brand_name": "AA"}))
    pages = _tarball(tmp_path, [_unit("s1", "w", "sonnet-5-5"), _unit("s1", "aplus", "haiku")])
    assert bm.main(["--pages", pages, "--dry-run", "--brand-json", str(brand), "--model", "gpt-5.6-luna"]) == 0
    out = capsys.readouterr().out
    body = json.loads(out.split("BRANDFIT-BEGIN\n")[1].split("\nBRANDFIT-END")[0])
    assert body["dry_run"] and set(body["sample_prompts"]) == {"v1", "v2"}
    assert body["code"]["source"] == "image" and body["rubric_v2_version"] == v2.RUBRIC_V2_VERSION
    assert "SOURCE (the supplier's text" in body["sample_prompts"]["v2"]["prompt"]


def test_overlay_loads_the_branch_files_into_a_fresh_process(tmp_path):
    # A copy of this branch's two modules as the overlay; the child process must use THEM (not the import path's).
    root = tmp_path / "overlay"
    for rel in bm.OVERLAY_MODULES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text((ROOT / rel).read_text() + "\nAA645_OVERLAY_MARK = True\n")
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from scripts.aa645 import brandfit_measure as m\n"
            "info = m.apply_overlay(%r, force=True)\n"
            "import services.content_generation.brand_fit as bf, services.content_generation.brand_fit_v2 as v2\n"
            "from services.content_generation import judge_node\n"
            "assert info['source'] == 'overlay' and len(info['files']) == 2\n"
            "assert bf.AA645_OVERLAY_MARK and v2.AA645_OVERLAY_MARK\n"
            "assert judge_node.score_brand_fit is bf.score_brand_fit\n"
            "print('OK')\n") % (str(ROOT), str(root))
    env = {**os.environ, "ENVIRONMENT": "test", "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, env=env,
                       timeout=120)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-2000:]


def test_overlay_is_refused_without_a_dir_when_the_image_lacks_v2():
    with patch.object(bm, "_image_has_v2", return_value=False), pytest.raises(SystemExit, match="overlay"):
        bm.apply_overlay(None)


# ── brandfit_analyze: the pre-registered criterion ───────────────────────────────────────────────────────────


def test_criterion_pass_and_fail():
    table = {"aplus|haiku": {"retry_pct": 20.0}, "w|sonnet-5-5": {"retry_pct": 25.0}}
    ok = an.criterion(table, {"spearman_brand_fit_vs_serious_rate": -0.2})
    assert ok["pass"] is True and ok["retry_ok"] and ok["spearman_ok"]
    assert an.criterion(table, {"spearman_brand_fit_vs_serious_rate": 0.01})["pass"] is False
    table["w|sonnet-5-5"]["retry_pct"] = 25.1
    assert an.criterion(table, {"spearman_brand_fit_vs_serious_rate": -0.5})["pass"] is False
    assert an.criterion({}, {"spearman_brand_fit_vs_serious_rate": -0.5})["pass"] is None


def _row(sample, arm, fit, rubric="v2", **kw):
    return {"ok": True, "key": f"{sample}__{arm}|{rubric}", "page_key": f"{sample}__{arm}", "sample_id": sample,
            "arm_model": arm, "brand_fit": fit, "rubric": rubric, **kw}


def test_analyse_end_to_end_on_synthetic_rows():
    rows = []
    for i in range(6):
        rows.append(_row(f"s{i}", "aplus|haiku", 5.0 + (i % 2)))          # error-heavy pages score low
        rows.append(_row(f"s{i}", "w|sonnet-5-5", 8.0))                   # faithful pages score high
    panel = {}
    for i in range(6):
        panel[f"s{i}__aplus|haiku"] = {"rate": 0.04 + i / 1000}
        panel[f"s{i}__w|sonnet-5-5"] = {"rate": 0.003}
    score = {"aplus|haiku": {"serious_per_claim_pooled": 0.0385}, "w|sonnet-5-5": {"serious_per_claim_pooled": 0.0033}}
    out = an.analyse({"rows": rows}, score, panel)
    v = out["rubrics"]["v2"]
    assert v["arms"]["aplus|haiku"]["retry_pct"] == 100.0 and v["arms"]["w|sonnet-5-5"]["retry_pct"] == 0.0
    assert v["page_level"]["spearman_brand_fit_vs_serious_rate"] < 0
    assert v["criterion"]["pass"] is True and out["verdict"]["rubric_v2_passes"] is True
    assert "A1_V2_BRAND_FIT_GATE=enforce" in out["verdict"]["next"]
    assert "VERDICT rubric_v2_passes=True" in an.table_text(out)


def test_rows_of_reads_the_s248_v1_result_shape():
    s248 = {"rows": [{"ok": True, "key": "s1__w__sonnet-5-5", "sample_id": "s1", "arm_model": "w|sonnet-5-5",
                      "brand_fit": 6.0}, {"ok": False, "key": "x"}]}
    rows = an.rows_of(s248, "v1_s248")
    assert len(rows) == 1 and rows[0]["rubric"] == "v1_s248" and rows[0]["page_key"] == "s1__w__sonnet-5-5"


def test_sign_test_and_spearman():
    assert an.sign_test([-1, -1, -1, 0])["lower"] == 3 and an.sign_test([0, 0])["p"] is None
    assert an.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0 and an.spearman([1, 2], [1, 2]) is None


# ── shadow: selection ────────────────────────────────────────────────────────────────────────────────────────


def _cand(tid, country, places=False, **kw):
    return {"tour_id": tid, "country": country, "has_places": places, "src_name": f"Tour {tid}", **kw}


def test_elephant_and_excluded_tours_are_dropped():
    cands = [_cand("a", "Laos"), _cand("b", "Laos", src_itineraries="Day 2: an elephant ride in the camp"),
             _cand("c", "Thailand"), _cand("d", "Laos", src_highlights='["Elephant-back trek"]')]
    ok, drop = sh.eligible(cands, {"c"})
    assert [c["tour_id"] for c in ok] == ["a"] and drop == {"excluded_ids": 1, "elephant": 2}


def test_elephant_rule_is_the_brand_audit_rule():
    from services.content_generation.brand_audit_node import ELEPHANT_RIDING
    assert sh.ELEPHANT_RIDING.pattern == ELEPHANT_RIDING.pattern and sh.ELEPHANT_RIDING.flags == ELEPHANT_RIDING.flags


def test_leak_phrases_match_rubric_v2():
    assert sh.LEAK_PHRASES == v2.LEAK_PHRASES
    for page, src in (("Join Adventure Asia today", ""), ("Design this journey", "design this journey"),
                      ("Asian food", "")):
        assert sh.leaks(page, src) == v2.brand_leaks(page, src)


def test_pilot_prefers_built_places_in_the_country():
    cands = [_cand("a", "Laos"), _cand("b", "laos ", places=True), _cand("c", "Laos", places=True),
             _cand("d", "Vietnam", places=True)]
    got = [c["tour_id"] for c in sh.select_pilot(cands, 3, "Laos", "seed")]
    assert set(got[:2]) == {"b", "c"} and got[2] == "a"


def test_wave_is_stratified_by_country_and_capped():
    cands = [_cand(f"np{i}", "Nepal") for i in range(30)] + [_cand(f"vn{i}", "Vietnam") for i in range(15)] \
        + [_cand(f"bt{i}", "Bhutan") for i in range(4)] + [_cand("mn0", "Mongolia")]
    got = sh.select_wave(cands, 20, "seed")
    by = {}
    for c in got:
        by[c["country"]] = by.get(c["country"], 0) + 1
    assert len(got) == 20 and set(by) == {"Nepal", "Vietnam", "Bhutan", "Mongolia"} and by["Mongolia"] == 1
    assert by["Nepal"] > by["Vietnam"] > by["Bhutan"]
    assert len({c["tour_id"] for c in got}) == 20
    assert [c["country"] for c in got[:4]] == sorted(by)            # interleaved country by country
    assert sh.select_wave(cands, 20, "seed") == got                 # deterministic for a seed


def test_wave_size_is_capped_at_50():
    with pytest.raises(SystemExit):
        sh.parse_args(["--select", "wave", "--n", "51", "--max-cost-usd", "6"])
    assert sh.parse_args(["--select", "wave", "--n", "50", "--max-cost-usd", "6"]).n == 50


def test_load_ids_reads_a_list_and_an_aa729_fixture(tmp_path):
    a = tmp_path / "ids.json"
    a.write_text(json.dumps(["x", " y ", ""]))
    b = tmp_path / "fixture.json"
    b.write_text(json.dumps({"samples": [{"refs": {"tour_id": "z"}}, {"refs": {}}]}))
    assert sh.load_ids([str(a), str(b)]) == {"x", "y", "z"}


def test_env_is_prepared_and_a_foreign_stage_refused(monkeypatch):
    # setenv first so monkeypatch restores the ORIGINAL values (unset) after _prepare_env writes os.environ itself
    monkeypatch.setenv("AA729_STAGE", "x")
    monkeypatch.setenv("S1_WRITER_V2", "x")
    monkeypatch.delenv("AA729_STAGE")
    monkeypatch.delenv("S1_WRITER_V2")
    monkeypatch.delitem(sys.modules, "scripts.aa729.common", raising=False)
    sh._prepare_env()
    assert os.environ["AA729_STAGE"] == "adhoc_aa645" and os.environ["S1_WRITER_V2"] == "1"
    monkeypatch.setenv("AA729_STAGE", "adhoc_aa729")
    with pytest.raises(SystemExit):
        sh._prepare_env()                                      # a foreign stage in the environment
    monkeypatch.setenv("AA729_STAGE", "adhoc_aa645")
    monkeypatch.setitem(sys.modules, "scripts.aa729.common", SimpleNamespace(STAGE="adhoc_aa729"))
    with pytest.raises(SystemExit):
        sh._prepare_env()                                      # common already imported under another stage


def test_writer_model_patch_sets_model_tier_for_writer_calls_only(monkeypatch):
    from services.content_generation import s1_writer_v2_live as live
    seen = []
    monkeypatch.setattr(live, "_call", lambda client, state, **kw: seen.append(state.get("model_tier")) or {"ok": 1})
    sh.install_writer_model("sonnet-5-5")
    live._call(None, {"model_tier": None}, call="write", system="s", user="u", max_tokens=1)
    assert seen == ["sonnet-5-5"]
    sh.install_writer_model("sonnet-5-5")                      # idempotent: not wrapped twice
    live._call(None, {}, call="meta", system="s", user="u", max_tokens=1)
    assert seen == ["sonnet-5-5", "sonnet-5-5"]


def test_writer_model_route_means_no_patch(monkeypatch):
    from services.content_generation import s1_writer_v2_live as live
    marker = object()
    monkeypatch.setattr(live, "_call", marker)
    sh.install_writer_model("route")
    assert live._call is marker


def test_env_does_not_leak_out_of_the_test():
    # runs after test_env_is_prepared_and_a_foreign_stage_refused: writer v2 must not stay on for later tests
    assert os.environ.get("S1_WRITER_V2") != "1" and os.environ.get("AA729_STAGE") != "adhoc_aa645"


def test_seo_and_brand_rules_shapes():
    seo = sh.seo_from_cache({"top_keywords": json.dumps([{"keyword": "laos kayak"}, "vang vieng"]),
                             "people_also_ask": [{"question": "Is it safe?"}]})
    assert seo == {"top_keywords": ["laos kayak", "vang vieng"], "keywords": {"top_keywords": ["laos kayak",
                                                                                               "vang vieng"]},
                   "people_also_ask": ["Is it safe?"]}
    br = sh.brand_rules_from_row({"forbidden_words": '["explore"]', "voice_examples": None, "core_idea": "c"})
    assert br["forbidden_words"] == ["explore"] and br["voice_examples"] == [] and br["core_idea"] == "c"
    assert sh.brand_rules_from_row(None) == {}


# ── shadow: per-tour verdict + report ───────────────────────────────────────────────────────────────────────


def test_tour_verdict_reads_the_final_state():
    result = {"status": "success", "writer_version": "v2", "model_used": "satellite-sonnet-5-5", "retry_count": 1,
              "quality_score": 8.2, "brand_audit_status": "pass", "failure_codes": [], "fix_pass_applied": True,
              "judge_brand_fit": 6.5, "judge_score": 6.5, "judge_rubric": v2.RUBRIC_V2_VERSION,
              "judge_caps": ["unsupported"], "judge_unsupported": [{"quote": "q", "why": "w"}],
              "grounding_ran": True, "grounding_found": [{}, {}], "grounding_violations": [{}],
              "degraded": [], "quality_shadow": [{"code": "DAY_PLACE_MISMATCH"}],
              "generated": {**OUT, "summary": OUT["summary"] + " Adventure Asia leads it."}}
    live = {**OUT, "itineraries": "Day 1 — Nam Song\nKayak.\n\nDay 2 — Back\nDrive."}
    v = sh.tour_verdict(result, TOUR, live, {"writer_version": None})
    assert v["attempts"] == 2 and v["publishable"] is True and v["judge"]["pass"] is False
    assert v["judge"]["rubric"] == v2.RUBRIC_V2_VERSION and v["judge"]["unsupported"] == 1
    assert v["grounding"] == {"ran": True, "found": 2, "still_unsupported": 1, "repaired_fields": []}
    assert v["leaks"] == ["Adventure Asia"] and v["live_writer_version"] == "v1"
    assert v["days"]["new"] == [1] and v["days"]["live"] == [1, 2] and v["days"]["count_dropped"] is True
    assert v["days"]["legacy_path"] is False and v["days"]["matches_plan"] is True
    assert v["quality_shadow_codes"] == ["DAY_PLACE_MISMATCH"] and v["new_page"]["canonical_days"] is True


def test_tour_verdict_flags_the_legacy_day_path():
    result = {"status": "success", "writer_version": "v2", "retry_count": 0, "quality_score": 8.0,
              "generated": {**OUT, "itineraries": "We kayak the Nam Song and have lunch."}}
    v = sh.tour_verdict(result, TOUR, None, None)
    assert v["days"]["legacy_path"] is True and v["live_page"] is None and v["days"]["count_dropped"] is False


def _t(country, **kw):
    base = {"tour_id": kw.pop("tid", country), "country": country, "status": "success", "publishable": True,
            "attempts": 1, "judge": {"brand_fit": 8.0, "caps": []},
            "judged_drafts": [{"brand_fit": 8.0, "gate": "brand_fit_report_only", "rubric": v2.RUBRIC_V2_VERSION}],
            "new_page": {"gate_codes": [], "figure_coverage": 0.7}, "live_page": {"gate_codes": ["META_LENGTH"],
                                                                                  "figure_coverage": 0.4},
            "grounding": {"found": 1, "still_unsupported": 0}, "jev": {"ran": True, "problem": False},
            "cost_usd": 0.08, "usd_by_stage": {"s1_generate": 0.07, "s1_judge": 0.005},
            "days": {"legacy_path": False, "matches_plan": True}, "leaks": []}
    base.update(kw)
    return base


def test_report_clean_run_has_no_stop():
    s = sr.summarize([_t("Laos", tid="a"), _t("Laos", tid="b"), _t("Nepal", tid="c")])
    a = s["countries"]["ALL"]
    assert a["publishable_rate"] == 1.0 and a["gate_page_rate_new"] == 0.0 and a["gate_page_rate_live"] == 1.0
    assert a["judge_pass_rate_drafts"] == 1.0 and a["cost_per_tour_mean"] == 0.08 and not s["stop"]
    assert set(s["countries"]) == {"Laos", "Nepal", "ALL"}
    assert a["cost_per_tour_by_stage"]["s1_generate"] == 0.07
    assert "Stop conditions: none" in sr.markdown(s)


@pytest.mark.parametrize("tour, stop", [
    (dict(publishable=False), "stayed_v1_over_10pct"),
    (dict(days={"legacy_path": True, "matches_plan": False}), "legacy_itinerary_path"),
    (dict(cost_usd=0.5), "cost_per_tour_over_model"),
    (dict(throttle_rows=1), "bedrock_throttle"),
    (dict(error="RuntimeError: boom"), "tour_errors"),
    (dict(error="not written by writer v2 (flag / applies() check)"), "not_written_by_v2"),
    (dict(degraded_hard=["jev_unavailable"]), "degraded_hard"),
    (dict(leaks=["Adventure Asia"]), "brand_leak_pages"),
    (dict(new_page={"gate_codes": ["META_CUT"], "figure_coverage": 0.5}), "not_better_than_live"),
])
def test_report_stop_conditions(tour, stop):
    tours = [_t("Laos", tid="a"), _t("Laos", tid="b", **tour)]
    if stop == "not_better_than_live":
        tours = [_t("Laos", tid="a", **tour), _t("Laos", tid="b", **tour)]
    s = sr.summarize(tours)
    assert s["stop_conditions"][stop] is True and s["stop"] is True


def test_report_cost_cap_skip_is_not_an_error():
    s = sr.summarize([_t("Laos", tid="a"), _t("Laos", tid="b", error="CostCapReached: cap $0.50")])
    assert s["countries"]["ALL"]["errors"] == 0 and s["countries"]["ALL"]["cost_cap_skipped"] == 1
    assert not s["stop_conditions"]["tour_errors"]


# ── wave report: the rubric label ────────────────────────────────────────────────────────────────────────────


def test_wave_report_labels_the_brand_fit_rubric():
    from scripts.aa645 import wave_report as wr
    from services.quality.s1_facts import build_fact_table
    facts = build_fact_table(TOUR)
    stored = {"id": "v2id", "status": "approved", "aa_name": "N", "aa_itineraries": "Day 1 — A\nx",
              "metadata": json.dumps({"writer_version": "v2",
                                      "judge": {"brand_fit": 6, "rubric": v2.RUBRIC_V2_VERSION}})}
    assert wr.version_facts(stored, TOUR, facts)["brand_fit_rubric"] == v2.RUBRIC_V2_VERSION
    old = {**stored, "metadata": json.dumps({"judge": {"brand_fit": 8}})}
    assert wr.version_facts(old, TOUR, facts)["brand_fit_rubric"] == "v1"


def test_shadow_public_output_drops_page_text():
    doc = {"tours": [{"tour_id": "a", "_output": {"x": 1}, "_tour": {}, "cost_usd": 0.1}]}
    assert sh._public(doc)["tours"] == [{"tour_id": "a", "cost_usd": 0.1}]


def test_judged_and_calls_by_job_group_rows():
    rows = [{"job_id": "j", "role": "writer", "provider": "bedrock-satellite", "real_stage": "s1_generate", "n": 2,
             "usd": 0.06, "fallback_rows": 0, "throttle_rows": 0, "unusable_rows": 0},
            {"job_id": "j", "role": "judge", "provider": "bedrock-satellite", "real_stage": "s1_judge", "n": 3,
             "usd": 0.01, "fallback_rows": 1, "throttle_rows": 0, "unusable_rows": 0}]
    c = sh._calls_by_job(rows)["j"]
    assert c["cost_usd"] == pytest.approx(0.07) and c["by_stage"] == {"s1_generate": 2, "s1_judge": 3}
    assert c["fallback_rows"] == 1 and c["calls"] == {"writer": 2, "judge": 3}
    j = sh._judged_by_job([SimpleNamespace(**{"job_id": "j", "brand_fit": 7.5, "gate": "g", "rubric": "v1"}).__dict__])
    assert j == {"j": [{"brand_fit": 7.5, "gate": "g", "rubric": "v1"}]}


def test_panel_files_pair_the_new_page_with_the_live_page(tmp_path):
    doc = {"run_id": "11111111-1111-1111-1111-111111111111", "writer_model": "sonnet-5-5",
           "tours": [{"tour_id": "a", "_output": OUT, "_tour": TOUR, "_live": OUT},
                     {"tour_id": "b", "_output": OUT, "_tour": TOUR, "_live": None},
                     {"tour_id": "c", "error": "boom"}]}
    sh._write_panel(doc, "k/shadow", str(tmp_path), upload=False)
    units = json.loads((tmp_path / "k__shadow-panel-pages.json").read_text())
    assert [(u["sample_id"], u["arm"]) for u in units] == [("a", "shadow_w"), ("a", "stored"), ("b", "shadow_w")]
    key = json.loads((tmp_path / "k__shadow-panel__key.json").read_text())
    packet = json.loads((tmp_path / "k__shadow-panel__packet.json").read_text())
    assert len(key) == 3 and len(packet["groups"]) == 2
    assert all("arm" not in p for g in packet["groups"] for p in g["pages"])      # blind: arms only in the key
