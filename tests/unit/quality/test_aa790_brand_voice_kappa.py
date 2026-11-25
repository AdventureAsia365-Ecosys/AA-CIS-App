"""AA-790 (S294 D5) — the voice rubric's agreement with the golden-set brand labels (services/quality/
brand_voice_kappa.py). Pure: no DB, no model. Invented brands, places and texts only.
"""
from __future__ import annotations

import ast
from pathlib import Path

from services.quality import brand_agreement as ba
from services.quality import brand_voice_kappa as bvk


def _brand(fit, votes=None, source="agent_panel"):
    return {"rubric": "aa1079-brand-v1", "brand_fit": fit, "core_idea": "present", "reason": "", "source": source,
            "status": "agreed", "approved_by": "tech lead", "labelled_at": "2026-11-25T00:00:00+00:00",
            "votes": votes if votes is not None else [{"lens": lens, "brand_fit": fit, "core_idea": "present"}
                                                      for lens in ("voice", "audience", "skeptic")]}


def _t9(sid, fit=None, channel="linkedin", text="You'll walk the Orvalen ridge. Book with us."):
    stratum = "t9_blog" if channel == "blog" else "t9_linkedin"
    s = {"sample_id": sid, "kind": "t9", "stratum": stratum, "channel": channel,
         "input": {"context": {"channel": channel, "cta": "Book with us.",
                               "brand_audience": {"brand_name": "Kestrel Trails", "voice": ["Energetic"]}}},
         "output": {"content_text": text}, "label": {"verdict": "pass"}}
    if fit:
        s["label"]["brand"] = _brand(fit)
    return s


def _t2(sid, fit=None):
    s = {"sample_id": sid, "kind": "t2", "stratum": "t2", "channel": None, "input": {"brand_rules_ref": "kestrel"},
         "output": {"name": "Ridge Days", "seo_title": "x", "summary": "A calm walk above Lake Varnel.",
                    "highlights": ["Quiet huts"], "itineraries": [{"day": 1, "description": "Walk to Varro hut."}]},
         "label": {"verdict": "pass"}}
    if fit:
        s["label"]["brand"] = _brand(fit)
    return s


def _fixture(samples):
    return {"fixture_version": "9.9.0", "shared_inputs": {"brand_rules": {"kestrel": {"brand_type": "luxury"}}},
            "samples": samples}


def test_coverage_counts_what_exists_and_what_is_missing():
    samples = [_t9(f"t9l-{i:03d}", "fits" if i % 2 else "partly") for i in range(10)]
    samples += [_t9("t9l-100"), _t2("t2-001", "off"), _t2("t2-002")]
    samples.append({**_t9("inj-1", "fits"), "kind": "injection_t9", "stratum": "injection"})
    res = bvk.measure(_fixture(samples), scorer=lambda inp: (True, "energetic"))
    soc, t2 = res["coverage"]["t9_social"], res["coverage"]["t2"]
    assert soc["labelled"] == 10 and soc["unlabelled_ids"] == ["t9l-100"] and soc["human_labelled"] == 0
    assert soc["pairs_missing"] == ba.GATE_MIN_PAIRS - 10
    assert soc["pairs_missing_after_labelling_fixture"] == ba.GATE_MIN_PAIRS - 11
    assert soc["classes"]["strict"] == {"fail": 5, "pass": 5, "fail_missing": 0, "pass_missing": 0}
    assert soc["classes"]["lenient"]["fail"] == 0 and soc["classes"]["lenient"]["fail_missing"] == ba.MIN_PER_CLASS
    assert t2["labelled"] == 1 and t2["unlabelled"] == 1 and t2["brand_fit"] == {"off": 1}
    assert res["coverage"]["t9_blog"]["brand_items"] == 0


def test_perfect_agreement_on_enough_pairs_is_calibrated():
    samples = [_t9(f"t9l-{i:03d}", "fits" if i < 25 else "off") for i in range(45)]
    scorer = lambda inp: (inp["text"] != "OFF", "energetic")  # noqa: E731
    for s in samples[25:]:
        s["output"]["content_text"] = "OFF"
    res = bvk.measure(_fixture(samples), scorer=scorer)
    strict = res["writers"]["t9_social"]["strict"]
    assert strict["agreement"]["kappa"] == 1.0 and strict["decision"]["status"] == "calibrated"
    assert strict["decision"]["keep"] == "repair_only_gate_allowed" and strict["disagreements"] == []
    assert strict["lens_reliability"]["mean_kappa"] == 1.0


def test_too_few_pairs_or_one_class_stays_report_only():
    res = bvk.measure(_fixture([_t9(f"t9l-{i:03d}", "fits") for i in range(45)]),
                      scorer=lambda inp: (True, "calm"))
    d = res["writers"]["t9_social"]["strict"]["decision"]
    assert d["status"] == "insufficient_labels" and d["keep"] == "report_only"
    assert bvk.decide({"n": 50, "label_fail": 10, "label_pass": 40, "kappa": 0.45})["status"] == \
        "not_calibrated_moderate"
    assert bvk.decide({"n": 50, "label_fail": 10, "label_pass": 40, "kappa": 0.1})["status"] == "not_calibrated_weak"


def test_lens_reliability_reads_votes_pairwise():
    labs = [_brand("fits", votes=[{"lens": "voice", "brand_fit": "fits"}, {"lens": "audience", "brand_fit": "off"}]),
            _brand("off", votes=[{"lens": "voice", "brand_fit": "off"}, {"lens": "audience", "brand_fit": "fits"}])]
    rel = bvk.lens_reliability(labs, lenient=False)
    assert rel["pairs"]["audience~voice"]["n"] == 2 and rel["pairs"]["audience~voice"]["kappa"] == -1.0


def test_scoring_errors_are_listed_not_fatal():
    def boom(inp):
        raise ValueError("bad pack")
    res = bvk.measure(_fixture([_t9("t9l-001", "fits")]), scorer=boom)
    assert res["writers"]["t9_social"]["errors"] == [{"sample_id": "t9l-001", "error": "ValueError"}]


def test_real_rubric_scores_t9_and_t2_inputs():
    fx = _fixture([_t9("t9l-001", "fits"), _t2("t2-001", "partly")])
    res = bvk.measure(fx)
    assert res["writers"]["t9_social"]["scored"] == 1 and res["writers"]["t2"]["scored"] == 1
    assert res["writers"]["t2"]["profiles"] == {"calm": 1}       # brand_type luxury
    inp = bvk.scoring_input(fx, fx["samples"][1], "t2", None)
    assert "Lake Varnel" in inp["text"] and "Ridge Days" not in inp["text"] and inp["channel"] == "tour"
    assert any(line.startswith("| t9_social |") for line in bvk.report_lines(res))


def test_no_model_db_or_network_import():
    tree = ast.parse(Path(bvk.__file__).read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and any(x in m for x in ("llm_client", "asyncpg", "boto3", "anthropic", "openai"))
                   for m in mods)


def test_addendum_plan_reaches_the_bar_per_writer():
    """docs/calibration/data/aa790-brand-label-addendum.json: after the panel step and the new samples every
    writer has >= GATE_MIN_PAIRS pairs; the ids are golden sample ids of the right writer."""
    import json
    import re

    root = Path(__file__).resolve().parents[3]
    path = root / "docs" / "calibration" / "data" / "aa790-brand-label-addendum.json"
    plan = json.loads(path.read_text("utf-8"))
    assert plan["rubric"] == "aa1079-brand-v1" and plan["cost_usd"] == 0
    prefix = {"t2": r"t2-\d{3}", "t9_blog": r"t9b-\d{3}", "t9_social": r"t9[lf]-\d{3}"}
    for w, measured in plan["measured"]["writers"].items():
        ids = plan["step_1_panel_on_existing_items"][w]
        assert all(re.fullmatch(prefix[w], i) for i in ids) and len(set(ids)) == len(ids)
        total = measured["labelled"] + len(ids) + plan["step_2_new_samples"][w]
        assert total >= ba.GATE_MIN_PAIRS, w
