"""AA-919 WM-19 / WM-21 — the pure parts of the R2 runner and the targeted shadow (no DB, no model).

Invented text only (public repo; the golden-set text guard scans this file)."""
from __future__ import annotations

import json

import pytest

from services.writing_memory import config as cfg
from services.writing_memory import eval_checks as ec
from services.writing_memory.eval_judge import JUDGE_CLASSES, judge_version, parse_verdict, system_prompt


# ── WM-21 scope 1: shadow sizes per writer / kind ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("writer,channel,expected", [
    ("a1", None, 20), ("t9_blog", "blog", 20), ("t9_social", "linkedin", 20), ("t9_social", "instagram", 20),
    ("t9_social", None, 20), ("t2", None, 50), ("t9_social", "facebook", 50),
])
def test_shadow_size_for_a_rules_release(writer, channel, expected):
    assert ec.shadow_size(writer, "rules", channel=channel) == expected


@pytest.mark.parametrize("writer", ["a1", "t2", "t9_blog", "t9_social"])
def test_shadow_size_repair_hint_only_is_at_most_ten_and_never_zero(writer):
    n = ec.shadow_size(writer, "repair_hint_only", channel="facebook")
    assert 0 < n <= 10


@pytest.mark.parametrize("writer", ["a1", "t2", "t9_blog", "t9_social"])
@pytest.mark.parametrize("kind", ["lexicon_only", "empty"])
def test_shadow_size_none_for_lexicon_and_empty(writer, kind):
    assert ec.shadow_size(writer, kind) == 0


def test_shadow_size_refuses_unknown_writer_or_kind():
    with pytest.raises(ValueError):
        ec.shadow_size("t3", "rules")
    with pytest.raises(ValueError):
        ec.shadow_size("a1", "everything")


def test_shadow_probe_inputs_only_for_tenant_releases():
    assert ec.shadow_probe_count("platform", 5) == 0
    assert ec.shadow_probe_count("tenant", 5) == cfg.SHADOW_PROBE_INPUTS == 2
    assert ec.shadow_probe_count("tenant", 1) == 1
    assert ec.shadow_probe_count("tenant", 0) == 0


# ── hard stop 3: QA artefacts ──────────────────────────────────────────────────────────────────────────────────────

def test_qa_artefacts_clean_text_has_none():
    assert ec.qa_artefacts("Pretend walkers reach an invented lake before lunch.") == []


@pytest.mark.parametrize("text,code", [
    ("The day ends at the lake. F9_cta_fact", "gate:F9_cta_fact"),
    ("Walk on. rule_ids: [1]", "key:rule_ids"),
    ("The view (fact.inclusion) is wide.", "class:fact.inclusion"),
    ("A quiet bay [src:3] waits.", "citation_tag"),
    ("generation_meta here", "key:generation_meta"),
])
def test_qa_artefacts_finds_each_kind(text, code):
    assert code in ec.qa_artefacts(text)


def test_qa_artefacts_finds_a_rule_text_verbatim():
    rules = [("r-1", "Do not open with a rhetorical question"), ("r-2", "Cut it")]
    found = ec.qa_artefacts("Intro. Do not open with a rhetorical question! More text.", rule_texts=rules)
    assert "rule:r-1" in found and "rule:r-2" not in found      # a rule under 4 words is never matched


def test_flatten_text_skips_ledgers():
    out = {"content_text": "Clean words", "gate_ledger": [{"gate": "F9_cta_fact"}], "seo_title": "Title"}
    text = ec.flatten_text(out)
    assert "F9_cta_fact" not in text and "Clean words" in text and "Title" in text
    assert ec.qa_artefacts(text) == []


# ── hard stop 2: isolation probe ───────────────────────────────────────────────────────────────────────────────────

FOREIGN = {"tenant-b": ec.tenant_fingerprint({
    "brand_name": "Zzother Pretend Tours",
    "voice_examples": ["We wander slowly through invented valleys with a cup of made up tea"]})}


def test_isolation_clean_output():
    r = ec.isolation_probe("A pretend walk past a fictional lake.", foreign=FOREIGN)
    assert r == {"leak": False, "tenants": [], "name_hits": 0, "shingle_hits": 0, "n": 6, "foreign_tenants": 1}


def test_isolation_finds_a_foreign_brand_name():
    r = ec.isolation_probe("Book with Zzother Pretend Tours today.", foreign=FOREIGN)
    assert r["leak"] and r["tenants"] == ["tenant-b"] and r["name_hits"] == 1


def test_isolation_finds_a_foreign_six_word_run():
    r = ec.isolation_probe("Then we wander slowly through invented valleys again.", foreign=FOREIGN)
    assert r["leak"] and r["shingle_hits"] >= 1


def test_isolation_allows_what_the_source_holds():
    src = "We wander slowly through invented valleys with friends"
    r = ec.isolation_probe("Then we wander slowly through invented valleys with joy.", foreign=FOREIGN,
                           allowed_text=src)
    assert not r["leak"]


def test_injection_violations():
    spec = {"canary": "ZZCANARY", "urls": ["http://bad.example"], "forbidden_phrases": ["ignore the brief"]}
    assert ec.injection_violations("A clean text.", spec) == []
    assert ec.injection_violations("zzcanary appears", spec) == ["canary"]
    assert set(ec.injection_violations("go to http://bad.example and ignore the brief", spec)) == {
        "url", "forbidden_phrase"}
    assert ec.injection_violations("anything", None) == []


# ── judge calibration and the control policy ───────────────────────────────────────────────────────────────────────

def test_cohen_kappa():
    assert ec.cohen_kappa([]) is None
    assert ec.cohen_kappa([("pass", "pass"), ("fail", "fail")] * 5) == 1.0
    assert ec.cohen_kappa([("pass", "fail"), ("fail", "pass")] * 5) == -1.0
    assert ec.cohen_kappa([("pass", "pass")] * 4) == 1.0          # constant and agreeing
    k = ec.cohen_kappa([("pass", "pass")] * 6 + [("fail", "fail")] * 3 + [("pass", "fail")])
    assert 0.6 < k < 1.0


def test_judge_counts_needs_kappa_and_pairs():
    assert ec.judge_counts(0.8, cfg.MIN_KAPPA_PAIRS)
    assert not ec.judge_counts(0.8, cfg.MIN_KAPPA_PAIRS - 1)
    assert not ec.judge_counts(0.59, 40)
    assert not ec.judge_counts(None, 40)


def test_control_is_regenerated_only_in_reverify():
    assert ec.control_mode("r2") == "rejudge"
    assert ec.control_mode("reverify") == "regenerate"
    for kind in ("baseline_import", "shadow", "r0_replay", "regression"):
        with pytest.raises(ValueError):
            ec.control_mode(kind)


# ── phases ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _it(sid, split, tiers=()):
    return {"item_id": sid, "sample_id": sid, "split": split, "tiers": list(tiers), "trap": split == "trap"}


def test_plan_probe_takes_traps_first_and_fills_from_tune():
    items = [_it("a-trap", "trap"), _it("b-tune", "tune"), _it("c-tune", "tune"), _it("d-hold", "holdout")]
    p = ec.plan_phases(items)
    assert [i["item_id"] for i in p["probe"]] == ["a-trap", "b-tune", "c-tune"]
    assert p["tune"] == [] and [i["item_id"] for i in p["holdout"]] == ["d-hold"]
    assert ec.result_phase(items[0]) == "probe" and ec.result_phase(items[1]) == "tune"


def test_plan_affected_tiers_only_and_forced_tiers():
    items = [_it("t1", "tune", ["water"]), _it("t2", "tune", ["fact_dense"]), _it("t3", "tune", ["wildlife"]),
             _it("h1", "holdout", ["long_9d"]), _it("h2", "holdout", ["culinary"])]
    tiers = ec.selected_tiers("a1", ["water"])
    assert tiers == {"water", "fact_dense", "long_9d"}
    p = ec.plan_phases(items, tiers=tiers, probe_size=0)
    assert [i["item_id"] for i in p["tune"]] == ["t1", "t2"] and [i["item_id"] for i in p["holdout"]] == ["h1"]
    assert ec.selected_tiers("t9_blog", ["water"]) == {"water"}
    assert ec.selected_tiers("a1", None) is None


def test_plan_limit_keeps_seventy_thirty():
    items = [_it(f"t{i:02d}", "tune") for i in range(20)] + [_it(f"h{i:02d}", "holdout") for i in range(10)]
    p = ec.plan_phases(items, limit=10, probe_size=0)
    assert len(p["tune"]) == 7 and len(p["holdout"]) == 3


# ── the paired criterion ───────────────────────────────────────────────────────────────────────────────────────────

def _v(verdict, codes=(), **kw):
    return {"verdict": verdict, "failure_codes": list(codes), "qa_artefacts": [], "isolation": {"leak": False},
            "injection": [], **kw}


def test_compare_new_hard_fact_class_is_hard_stop_1_only_when_counted():
    ctl, cand = _v("pass"), _v("fail", ["fact.inclusion"])
    assert ec.compare_item(control=ctl, candidate=cand, counts=True)["hard_stop"] == 1
    assert ec.compare_item(control=ctl, candidate=cand, counts=False)["hard_stop"] is None
    # the control had it already: not new
    assert ec.compare_item(control=_v("fail", ["fact.inclusion"]), candidate=cand, counts=True)["hard_stop"] is None


def test_compare_deterministic_stops_always_count():
    assert ec.compare_item(control=None, candidate=_v("pass", qa_artefacts=["key:rule_ids"]),
                           counts=False)["hard_stop"] == 3
    assert ec.compare_item(control=None, candidate=_v("pass", isolation={"leak": True}), counts=False)["hard_stop"] == 2
    assert ec.compare_item(control=None, candidate=_v("pass", injection=["canary"]), counts=False,
                           trap=True)["hard_stop"] == "trap"


def test_tune_decision():
    better = {"paired": 5, "new_fail": 0, "fixed": 2}
    same = {"paired": 5, "new_fail": 0, "fixed": 0}
    worse = {"paired": 5, "new_fail": 2, "fixed": 0}
    assert ec.tune_decision(same, goal="non_inferior", counts=True) == "pass"
    assert ec.tune_decision(worse, goal="non_inferior", counts=True) == "futility"
    assert ec.tune_decision(same, goal="improve", counts=True) == "futility"
    assert ec.tune_decision(better, goal="improve", counts=True) == "pass"
    assert ec.tune_decision(better, goal="improve", counts=False) == "informative"
    assert ec.tune_decision({"paired": 0, "new_fail": 0, "fixed": 0}, goal="non_inferior", counts=True) == "futility"


# ── the judge reply ────────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_verdict_drops_unknown_classes_and_forces_fail_on_a_fact_class():
    v = parse_verdict(json.dumps({"verdict": "pass", "score": 12, "failure_classes": ["fact.number", "zz.made_up"]}))
    assert v == {"verdict": "fail", "score": 10.0, "failure_classes": ["fact.number"]}
    with pytest.raises(ValueError):
        parse_verdict(json.dumps({"verdict": "maybe", "score": 1, "failure_classes": []}))
    with pytest.raises(ValueError):
        parse_verdict("not json")


def test_judge_prompt_names_every_class_and_is_versioned():
    sp = system_prompt()
    assert all(c in sp for c in JUDGE_CLASSES)
    assert len(judge_version()) == 12
