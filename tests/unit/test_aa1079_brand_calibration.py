"""AA-1079 (S277) — brand-fit calibration: the brand label format on the golden set (services/quality/brand_labels.py),
the deterministic cross-brand overlap check (services/quality/brand_overlap.py), the agreement maths and the decision
rule (services/quality/brand_agreement.py), the panel CLI (scripts/aa729/brand_labels.py) and the measurement script
(scripts/quality/measure_aa1079_brand_fit.py). Pure: no DB, no model call. Invented brands, tours and text only.
"""
from __future__ import annotations

import json

import pytest

from services.quality import brand_agreement as ba
from services.quality import brand_labels as bl
from services.quality import brand_overlap as bo

VOICE = "unhurried mornings on the water, a lantern by the quay"


def _t2(sid, slug, tour, text, verdict="pass", version_id=None):
    return {"sample_id": sid, "kind": "t2", "stratum": "t2", "channel": None,
            "refs": {"version_id": version_id or f"0000000{sid[-1]}-0000-4000-8000-000000000000",
                     "tenant_slug": slug, "published_tour_id": tour},
            "input": {"brand_rules_ref": f"t2:{slug}", "tour": {"name": "Lake Orrin loop"}},
            "output": {"name": "Lake Orrin loop", "summary": text}, "pipeline_verdict": {"score": 9.9},
            "label": {"verdict": verdict, "source": "agent_panel"}, "label_source": "agent_panel"}


def _t9(sid, slug, passed):
    return {"sample_id": sid, "kind": "t9", "stratum": "t9_blog", "channel": "blog",
            "refs": {"tenant_slug": slug, "piece_id": f"p-{sid}"},
            "input": {"context": {"channel": "blog", "brand_audience": {"who": "slow travellers"},
                                  "brand_rubric_text": "calm, specific"}},
            "output": {"content_text": "A morning walk above the Tamsel terraces.",
                       "gate_ledger": [{"gate": "F9_brand_style", "passed": not passed},
                                       {"gate": "F4_length", "passed": True},
                                       {"gate": "F9_brand_style", "passed": passed}],
                       "flags": ["x"]},
            "pipeline_verdict": {"passed": True}, "label": {"verdict": "pass"}, "label_source": "agent_panel"}


def _fixture():
    return {"fixture_version": "1.4.0",
            "shared_inputs": {"brand_rules": {
                "t2:velmora": {"core_idea": "slow water travel", "customer_segment": "couples",
                               "voice_examples": [VOICE], "system_prompt": "hidden", "forbidden_words": []},
                "t2:harrow": {"core_idea": "family first", "customer_segment": "families"}}},
            "samples": [
                _t2("t2-001", "velmora", "tour-a", "Drift across Lake Orrin at first light, the quay still quiet."),
                _t2("t2-002", "harrow", "tour-a", "Bring the children to Lake Orrin, with short walks each day."),
                _t9("t9b-001", "velmora", True),
                {"sample_id": "inj-001", "kind": "injection_t9", "stratum": "injection", "channel": "blog",
                 "input": {"context": {"channel": "blog"}}, "output": {}, "label": {"verdict": "fail"}},
                {"sample_id": "s1-001", "kind": "s1", "stratum": "s1", "channel": None, "input": {"tour": {}},
                 "output": {}, "label": {"verdict": "pass"}}]}


# ── the label format ────────────────────────────────────────────────────────────────────────────────────────────

def test_only_tenant_voiced_items_take_a_brand_label():
    ids = [s["sample_id"] for s in _fixture()["samples"] if bl.is_brand_item(s)]
    assert ids == ["t2-001", "t2-002", "t9b-001"]


def test_make_brand_label_validates_every_field():
    lab = bl.make_brand_label("partly", "absent", source="agent_panel", approved_by="tech lead", reason="x" * 700,
                              votes=[{"lens": "voice", "brand_fit": "off", "core_idea": "absent", "extra": 1}])
    assert lab["rubric"] == bl.BRAND_RUBRIC_VERSION and len(lab["reason"]) == bl.REASON_MAX
    assert lab["votes"] == [{"lens": "voice", "brand_fit": "off", "core_idea": "absent"}]
    for bad in ({"brand_fit": "great"}, {"core_idea": "maybe"}, {"source": "judge"}, {"status": "open"}):
        with pytest.raises(bl.BrandLabelError):
            bl.validate_brand_label({**lab, **bad})
    with pytest.raises(bl.BrandLabelError):
        bl.make_brand_label("fits", "present", source="agent_panel")          # a panel label needs approved_by
    with pytest.raises(bl.BrandLabelError):
        bl.validate_brand_label("fits")
    assert bl.label_passes(lab) is False and bl.label_passes(lab, lenient=True) is True


def test_the_pack_hides_the_pipeline_verdict_scores_and_gate_results():
    pack = bl.export_brand_pack(_fixture())
    assert pack["count"] == 3 and pack["rubric"]["version"] == bl.BRAND_RUBRIC_VERSION
    t2 = next(i for i in pack["items"] if i["sample_id"] == "t2-001")
    assert t2["brand_pack"]["core_idea"] == "slow water travel" and "system_prompt" not in t2["brand_pack"]
    t9 = next(i for i in pack["items"] if i["sample_id"] == "t9b-001")
    assert "gate_ledger" not in t9["output"] and "flags" not in t9["output"]
    text = json.dumps(pack)
    assert "pipeline_verdict" not in text and "9.9" not in text


def test_apply_writes_label_brand_bumps_the_version_and_keeps_the_content_label():
    panel = {"t2-001": {"brand_fit": "fits", "core_idea": "present", "reason": "voice and audience match"},
             "t2-002": {"brand_fit": "off", "core_idea": "absent", "status": "disputed"},
             "t9b-001": {"brand_fit": "sideways", "core_idea": "present"},
             "s1-001": {"brand_fit": "fits", "core_idea": "present"},
             "zz-404": {"brand_fit": "fits", "core_idea": "present"}}
    new, info = bl.apply_brand_labels(_fixture(), panel, approved_by="tech lead")
    assert info["applied"] == ["t2-001"] and new["fixture_version"] == "1.5.0"
    reasons = dict(info["skipped"])
    assert reasons["t2-002"] == "disputed" and reasons["s1-001"] == "not a brand item"
    assert reasons["zz-404"] == "unknown sample" and reasons["t9b-001"].startswith("invalid answer")
    s = next(x for x in new["samples"] if x["sample_id"] == "t2-001")
    assert s["label"]["verdict"] == "pass" and s["label"]["brand"]["brand_fit"] == "fits"
    assert new["label_history"][-1]["source"] == "agent_panel_brand" and info["brand_unlabelled_left"] == 2
    with pytest.raises(ValueError):
        bl.apply_brand_labels(_fixture(), panel, approved_by=" ")


def test_a_tech_lead_brand_label_is_never_overwritten_by_the_panel():
    fx, _ = bl.apply_brand_labels(_fixture(), {"t2-001": {"brand_fit": "off", "core_idea": "absent"}},
                                  approved_by="tech lead", source="tech_lead")
    again, info = bl.apply_brand_labels(fx, {"t2-001": {"brand_fit": "fits", "core_idea": "present"}},
                                        approved_by="tech lead")
    assert dict(info["skipped"])["t2-001"] == "has a tech-lead brand label"
    relabel, _ = bl.apply_brand_labels(fx, {"t2-001": {"brand_fit": "partly", "core_idea": "present"}},
                                       approved_by="tech lead", source="tech_lead")
    lab = next(x for x in relabel["samples"] if x["sample_id"] == "t2-001")["label"]["brand"]
    assert lab["brand_fit"] == "partly" and lab["previous"]["brand_fit"] == "off"


def test_the_panel_cli_round_trip(tmp_path):
    from scripts.aa729 import brand_labels as cli

    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps(_fixture()))
    assert cli.main(["export-pack", str(fx), "--out", str(tmp_path / "pack.json")]) == 0
    assert json.loads((tmp_path / "pack.json").read_text())["count"] == 3
    (tmp_path / "panel.json").write_text(json.dumps({"t2-002": {"brand_fit": "partly", "core_idea": "present"}}))
    assert cli.main(["apply", str(fx), str(tmp_path / "panel.json"), "--approved-by", "tech lead",
                     "--out", str(tmp_path / "fx2.json")]) == 0
    assert json.loads((tmp_path / "fx2.json").read_text())["fixture_version"] == "1.5.0"


# ── the overlap check ───────────────────────────────────────────────────────────────────────────────────────────

SRC = "day one arrive at lake orrin and meet the guide at the quay for a welcome dinner by the water"


def test_overlap_tells_shared_source_wording_from_brand_to_brand_copying():
    a = SRC + " then drift across the still lake as the lanterns come on along the old harbour wall"
    b = SRC + " then let the children race along the shore path and count the boats before bed"
    rec = bo.cross_brand_overlap({"velmora": a, "harrow": b}, SRC, warn_line=0.35)
    pair = rec["pairs"][0]
    assert pair["pair"] == "harrow~velmora" and pair["jaccard"] > 0.2
    assert pair["jaccard_beyond_source"] < 0.05 < pair["jaccard"]   # nearly all they share is the Master wording
    assert rec["copied_from_source"]["velmora"] > 0.4 and rec["verdict"] in ("distinct", "overlap")


def test_identical_texts_are_reported_not_warned_and_distinct_ones_pass():
    rec = bo.cross_brand_overlap({"a": SRC, "b": SRC, "c": "a completely different walk through the tamsel hills "
                                                            "with tea at a farm house and a slow ride home"})
    assert rec["identical_pairs"] == ["a~b"] and rec["verdict"] == "identical"
    assert bo.cross_brand_overlap({"a": SRC, "b": "nothing alike here at all in any of these many words"}
                                  )["verdict"] == "distinct"
    assert bo.cross_brand_overlap({"a": SRC, "b": ""})["empty"] == ["b"]
    assert bo.cross_brand_overlap({"a": SRC})["verdict"] == "n/a"


def test_overlap_warns_at_the_line():
    a = SRC + " with a boat"
    b = SRC + " with a train"
    rec = bo.cross_brand_overlap({"a": a, "b": b}, warn_line=0.35)
    assert rec["warn_pairs"] == ["a~b"] and rec["verdict"] == "overlap"


def test_tour_text_reads_every_string_but_status_keys():
    txt = bo.tour_text({"name": "Orrin", "status": "ai_generated", "highlights": ["quay", {"x": "lantern"}],
                        "days": 3})
    assert txt.split("\n") == ["Orrin", "quay", "lantern"]


def test_overlap_report_leaves_policy_hold_tours_out_of_the_means():
    rep = bo.overlap_report({"t1": {"a": SRC, "b": SRC}, "t2": {"a": SRC + " boat ride", "b": "other words " * 6}})
    assert rep["identical_tours"] == ["t1"] and rep["rewritten"]["tours"] == 1 and "a~b" in rep["per_pair_mean"]


def test_the_warn_line_comes_from_the_thresholds_file():
    from services.quality.registry import threshold

    assert bo.default_warn_line() == float(threshold("t2.cross_brand_overlap_warn")) == 0.35   # S279 L2: a setting


# ── agreement and the decision rule ─────────────────────────────────────────────────────────────────────────────

def test_agreement_counts_fail_as_the_positive_class():
    a = ba.agreement([(False, False), (False, True), (True, False), (True, True), (True, True)])
    assert (a["tp"], a["fp"], a["fn"], a["tn"]) == (1, 1, 1, 2)
    assert a["recall_fail"] == 0.5 and a["precision_fail"] == 0.5 and a["accuracy"] == 0.6
    assert ba.agreement([])["kappa"] is None
    assert ba.agreement([(True, True), (True, False)])["kappa"] == 0.0


def test_sweep_and_best_threshold_find_the_line_that_agrees():
    scored = [(9.0, True), (8.5, True), (6.0, False), (5.5, False), (7.5, True), (6.5, False)]
    rows = ba.sweep(scored)
    best = ba.best_threshold(rows, current=7.0)
    assert best["kappa"] == 1.0 and 6.5 < best["threshold"] <= 7.5
    assert ba.best_threshold([{"threshold": 7.0, "kappa": None}], 7.0) is None


def test_the_decision_rule_needs_enough_labels_and_kappa():
    best = {"threshold": 6.0, "kappa": 0.7}
    assert ba.decision(10, 5, 5, best, 7.0)["status"] == "insufficient_labels"
    assert ba.decision(50, 3, 47, best, 7.0)["status"] == "insufficient_labels"
    ok = ba.decision(50, 20, 30, best, 7.0)
    assert ok["status"] == "calibrated" and ok["proposed_threshold"] == 6.0 and ok["keep"] == "cut_allowed"
    assert ba.decision(50, 20, 30, {"threshold": 6.0, "kappa": 0.45}, 7.0)["status"] == "not_calibrated_moderate"
    assert ba.decision(50, 20, 30, {"threshold": 6.0, "kappa": 0.1}, 7.0)["keep"] == "rank_only"
    assert ba.decision(50, 20, 30, None, 7.0)["status"] == "no_signal"


def test_the_decision_rule_takes_a_pass_fail_gate_row_without_a_threshold():
    # T10 F9 is pass/fail: measure() passes its agreement row (no "threshold" key) as best (S278 KeyError).
    row = ba.agreement([(False, False)] * 10 + [(True, True)] * 30 + [(True, False)] * 2)
    assert "threshold" not in row
    d = ba.decision(row["n"], row["label_fail"], row["label_pass"], row, 7.0)
    assert d["status"] == "calibrated" and d["proposed_threshold"] is None and d["keep"] == "gate_allowed"
    weak = ba.agreement([(False, True)] * 6 + [(True, False)] * 6 + [(True, True)] * 30)
    w = ba.decision(weak["n"], weak["label_fail"], weak["label_pass"], weak, 7.0)
    assert w["status"].startswith("not_calibrated") and w["proposed_threshold"] is None
    assert "threshold None" not in w["why"]


# ── the measurement script ──────────────────────────────────────────────────────────────────────────────────────

def _labelled_items():
    from scripts.quality import measure_aa1079_brand_fit as m

    fx, _ = bl.apply_brand_labels(_fixture(), {
        "t2-001": {"brand_fit": "fits", "core_idea": "present"},
        "t2-002": {"brand_fit": "off", "core_idea": "absent"},
        "t9b-001": {"brand_fit": "fits", "core_idea": "unclear"}}, approved_by="tech lead")
    return m, m.items_from_fixture(fx)


def test_measure_pairs_each_instrument_with_the_labels():
    m, items = _labelled_items()
    versions = {items[0]["refs"]["version_id"]: {"quality_score": 9.9,
                                                 "judge_breakdown": {"brand_fit": 8.0, "mission_present": True}},
                items[1]["refs"]["version_id"]: {"quality_score": 9.8,
                                                 "judge_breakdown": json.dumps({"brand_fit": 5.0,
                                                                                "mission_present": True})}}
    judge = m.judge_values(items, versions)
    assert judge["t9b-001"]["f9_passed"] is True                       # the LAST F9 entry of the ledger
    out = m.measure(items, judge, current=7.0)
    assert out["coverage"]["labelled"] == 3 and out["coverage"]["by_writer"] == {"t2": 2, "t9_blog": 1}
    bf = out["instruments"]["t2_judge_brand_fit"]["strict"]
    assert bf["at_current"]["n"] == 2 and bf["at_current"]["kappa"] == 1.0
    stored = out["instruments"]["t2_stored_score"]["strict"]["at_current"]
    assert stored["judge_fail"] == 0 and stored["label_fail"] == 1             # the stored score passes everything
    assert out["instruments"]["t2_judge_core_idea"]["strict"]["at_current"]["fp"] == 0
    assert out["instruments"]["t2_judge_core_idea"]["strict"]["at_current"]["fn"] == 1
    assert bf["decision"]["status"] == "insufficient_labels"


def test_judge_scores_file_overrides_the_db_values():
    m, items = _labelled_items()
    judge = m.judge_values(items, {}, {"t2-001": {"brand_fit": 6.5, "ignored": 1}})
    assert judge["t2-001"]["brand_fit"] == 6.5 and "ignored" not in judge["t2-001"]
    assert judge["t2-002"]["brand_fit"] is None


def test_golden_overlap_groups_t2_items_by_tour_and_the_report_has_a_decision_section():
    m, items = _labelled_items()
    texts = {it["sample_id"]: bo.tour_text(it["output"]) for it in items if it["writer"] == "t2"}
    ov = m.golden_overlap(items, texts)
    assert list(ov["tours"]) == ["tour-a"] and ov["tours"]["tour-a"]["brands"] == ["harrow", "velmora"]
    judge = m.judge_values(items, {})
    md = m.report_markdown({"golden_set_version": "1.5.0", "measure": m.measure(items, judge, current=7.0),
                            "overlap": {"golden": ov}})
    assert "## Threshold decision (for the tech lead)" in md and "- [ ] A." in md and "golden:" in md


def test_measure_cli_refuses_no_db_without_a_fixture():
    from scripts.quality import measure_aa1079_brand_fit as m

    with pytest.raises(SystemExit):
        m.parse_args(["--no-db"])


def test_measure_runs_offline_on_a_fixture_file(tmp_path, monkeypatch):
    from scripts.quality import measure_aa1079_brand_fit as m

    fx, _ = bl.apply_brand_labels(_fixture(), {"t2-001": {"brand_fit": "fits", "core_idea": "present"}},
                                  approved_by="tech lead")
    path = tmp_path / "fx.json"
    path.write_text(json.dumps(fx))
    scores = tmp_path / "scores.json"
    scores.write_text(json.dumps({"t2-001": {"brand_fit": 8.0}}))
    rc = m.main(["--fixture", str(path), "--no-db", "--judge-scores", str(scores),
                 "--out", str(tmp_path / "m.json"), "--report", str(tmp_path / "m.md")])
    assert rc == 0
    out = json.loads((tmp_path / "m.json").read_text())
    assert out["cost_usd"] == 0 and out["golden_set_version"] == "1.5.0"
    assert (tmp_path / "m.md").read_text().startswith("# AA-1079 brand-fit calibration (1.5.0)")
