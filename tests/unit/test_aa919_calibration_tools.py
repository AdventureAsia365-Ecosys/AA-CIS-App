"""AA-919 S257 — the calibration report helpers, the kappa diag fix, the judge-model comparison harness and the
calibration candidate picker. Pure ($0): no DB, no model call; invented ids and texts only.
"""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.aa919 import calibration_candidates as cc
from scripts.aa919 import judge_compare as jc
from scripts.aa919 import kappa_diag as kd
from services.writing_memory import calibration as cal
from services.writing_memory import eval_checks as ec

SOURCE = "SEED:\nZz invented fortress at the meeting of two pretend rivers\nSCOPE:\nZz one moment only"


def _row(**kw):
    base = {"item_id": "i-1", "verdict": "fail", "score": Decimal("6"), "failure_codes": ["fact.embellish"],
            "details": json.dumps({"calibration": "only", "judge_claims": [{"class": "fact.embellish",
                                                                             "claim": "zz", "why": "zz"}]}),
            "sample_id": "zz-001", "channel": "linkedin", "split": "tune", "tiers": [], "label_verdict": "pass",
            "label_source": "agent_panel", "label": json.dumps({"grade": "light", "reason": "zz reason"}),
            "input": json.dumps({"context": {"scope": "Zz one moment only", "goal": {"description": "Zz goal text"},
                                             "brand_audience": {"core_idea": "Zz core idea of a pretend brand"}}}),
            "control_output": json.dumps({"content_text": "Zz output"}), "tenant_id": "t-a", "set_id": "s-1",
            "cal_kind": "only"}
    base.update(kw)
    return base


# ── calibration_verdict: the shapes r2.py stores ──────────────────────────────────────────────────────────────────

def test_calibration_verdict_reads_same_row_only_and_dict():
    assert cal.calibration_verdict(_row())["score"] == 6.0
    same = cal.calibration_verdict(_row(details={"calibration": "same_row"}, verdict="pass", score=8))
    assert same == {"verdict": "pass", "score": 8.0, "codes": ["fact.embellish"], "claims": []}
    d = cal.calibration_verdict(_row(details={"calibration": {"verdict": "pass", "score": "7", "failure_codes": [],
                                                              "judge_claims": [{"class": "x"}, "junk"]}}))
    assert d == {"verdict": "pass", "score": 7.0, "codes": [], "claims": [{"class": "x"}]}


def test_calibration_verdict_none_for_unlabelled_controls_and_bad_verdicts():
    assert cal.calibration_verdict(_row(details={"source_mode": "score_only"})) is None
    assert cal.calibration_verdict(_row(details=None)) is None
    assert cal.calibration_verdict(_row(verdict="error")) is None


def test_a_non_numeric_score_never_raises():
    assert cal.calibration_verdict(_row(score="SEED: not a number"))["score"] is None


# ── the 39096169 crash ─────────────────────────────────────────────────────────────────────────────────────────────

def test_diag_pair_record_keeps_the_score_when_the_judge_source_is_long_text():
    """S256 run 39096169: the diag reused the score variable for the judge SOURCE and float()ed the source."""
    app = (lambda writer, inp, shared: SOURCE, lambda writer, out: "Zz output", lambda: "v", [])
    x = kd.pair_record(_row(), "t9_social", {}, app, {})
    assert x["score"] == 6.0 and x["judge"] == "fail" and x["label"] == "pass"
    assert x["src_len"] == len(SOURCE) and x["out_len"] == len("Zz output")
    # scope is in the source; goal and core idea are not
    assert set(x["hidden"]) == {"goal", "core_idea"}
    assert ("channel", "linkedin") in x["strata"] and ("tenant", "t-a") in x["strata"]
    assert x["claims"][0]["class"] == "fact.embellish"


def test_diag_pair_record_without_app_modules_and_for_a_control_without_calibration():
    x = kd.pair_record(_row(), "t9_social", {}, None, {})
    assert x["score"] == 6.0 and x["src_len"] is None and x["hidden"] == {}
    assert kd.pair_record(_row(details={}), "t9_social", {}, None, {}) is None


def test_diag_pair_record_survives_a_source_builder_error():
    def boom(*a):
        raise KeyError("x")
    x = kd.pair_record(_row(), "t9_social", {}, (boom, boom, lambda: "v", []), {})
    assert x["src_len"] == "err KeyError" and x["score"] == 6.0


def test_hidden_from_judge_is_empty_with_source_parity():
    ctx = {"scope": "Zz one moment only"}
    assert cal.hidden_from_judge(ctx, SOURCE) == {}
    assert cal.hidden_from_judge("not a dict", SOURCE) == {}


# ── report ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _recs():
    item = {"channel": "linkedin", "tenant_id": "t-a", "tiers": [], "split": "tune"}
    rows = [("pass", "pass", 8.0)] * 3 + [("pass", "fail", 6.0)] * 5 + [("fail", "fail", 4.0)] * 5
    return [{"sample": f"zz-{i:03d}", "label": a, "judge": b, "score": s, "codes": ["fact.embellish"] if b == "fail"
             else [], "strata": ec.item_strata(item)} for i, (a, b, s) in enumerate(rows)]


def test_report_numbers():
    r = cal.report(_recs())
    assert r["kappa"] == 0.316 and r["pairs"] == 13 and not r["counts"]
    assert r["confusion"] == {"pass_pass": 3, "pass_fail": 5, "fail_fail": 5, "fail_pass": 0}
    assert r["pass_recall"] == 0.375 and r["fail_recall"] == 1.0
    assert r["score_auc"] == 1.0                      # the score separates the labels even though the verdict fails
    assert r["score_threshold_kappa"][5] == 1.0
    assert len(r["disagreements"]) == 5 and r["codes_on_pass_labels"] == {"fact.embellish": 5}
    lo, hi = r["loo_kappa"]
    assert lo <= r["kappa"] <= hi
    assert r["strata"][0]["dim"] == "all"
    assert r["strata_below"] == ["channel:linkedin", "tenant:t-a", "tier:untiered", "split:tune"]
    assert any("BELOW" in line for line in cal.strata_lines(r["strata"]))


def test_score_auc_edges():
    assert cal.score_auc([], [1.0]) is None
    assert cal.score_auc([5.0], [5.0]) == 0.5


# ── judge comparison harness ───────────────────────────────────────────────────────────────────────────────────────

def _model(key, *, vendor="openai", provider="bedrock", api_style="converse", enabled=True, acct=("acc3",),
           pin=0.2, pout=1.2):
    profiles = {a: f"zz.{key}" for a in acct}
    return SimpleNamespace(model_key=key, vendor=vendor, provider=provider, api_style=api_style, enabled=enabled,
                           blocked_reason=None, price_in_per_mtok=pin, price_out_per_mtok=pout,
                           account_for=lambda pref, p=profiles: pref if pref in p else (next(iter(p), None)))


CATALOG = {"luna": _model("luna"), "luna6": _model("luna6"), "astra": _model("astra", pin=1.0, pout=4.0),
           "luna-openai": _model("luna-openai", provider="openai", api_style="openai_chat", acct=()),
           "sonnet": _model("sonnet", vendor="anthropic"), "off": _model("off", enabled=False),
           "acc2only": _model("acc2only", acct=("acc2",)), "noprice": _model("noprice", pin=None)}


def test_choose_models_acc3_only():
    chosen, refused = jc.choose_models(CATALOG, ["luna", "luna-openai", "luna6", "sonnet", "off", "acc2only",
                                                 "noprice", "ghost", "luna"])
    assert chosen == ["luna", "luna6"]
    assert "leave AWS" in refused["luna-openai"] and "ADR 0010" in refused["sonnet"]
    assert "disabled" in refused["off"] and refused["acc2only"] == "not served on acc3"
    assert refused["noprice"] == "no price in the catalog" and "not in" in refused["ghost"]


def test_choose_models_caps_at_three_and_anthropic_needs_the_flag():
    chosen, refused = jc.choose_models(CATALOG, ["luna", "luna6", "astra", "sonnet"], allow_anthropic=True)
    assert chosen == ["luna", "luna6", "astra"] and "limit" in refused["sonnet"]


def test_estimate_and_max_usd_guard():
    est = jc.estimate(CATALOG, ["luna", "astra"], [30_000] * 13, 1200)
    one_luna_max = (10_000 * 0.2 + 1200 * 1.2) / 1e6
    assert est["per_model"]["luna"]["calls"] == 13
    assert est["per_model"]["luna"]["max_usd"] == round(13 * one_luna_max, 4)
    assert est["per_model"]["luna"]["expected_usd"] < est["per_model"]["luna"]["max_usd"]
    assert est["total_max_usd"] == round(est["per_model"]["luna"]["max_usd"] + est["per_model"]["astra"]["max_usd"], 4)
    assert jc.check_max_usd(None, 0.1) == "--apply needs --max-usd"
    assert "must be in" in jc.check_max_usd(jc.HARD_MAX_USD + 1, 0.1)
    assert "over --max-usd" in jc.check_max_usd(0.05, 0.1)
    assert jc.check_max_usd(0.5, 0.1) is None


def test_budget_stops_calls_that_would_pass_the_cap():
    b = jc.Budget(0.10)
    assert b.try_reserve(0.04) and b.try_reserve(0.04)
    assert not b.try_reserve(0.04) and b.skipped == 1     # 0.08 in flight + 0.04 > 0.10
    b.settle(0.04, 0.01)
    b.settle(0.04, None)                                   # unknown cost (a failed billed call): the worst case
    assert b.spent == pytest.approx(0.05) and b.reserved == 0.0
    assert b.try_reserve(0.04) and not b.try_reserve(0.02)


def test_model_agreement():
    out = jc.agreement({"a": {"s1": "pass", "s2": "fail", "s3": "pass"}, "b": {"s1": "pass", "s2": "pass"}})
    assert out == {"a|b": {"n": 2, "agree": 1, "kappa": ec.cohen_kappa([("pass", "pass"), ("fail", "pass")])}}


def test_harness_logs_under_the_adhoc_stage_as_judge():
    from services.writing_memory import config as cfg
    assert jc.STAGE == "adhoc_aa919" and cfg.EVAL_ROLE == "judge"


def test_harness_passes_model_and_stage_to_the_judge(monkeypatch):
    """judge_output with model_key + stage: the request carries the override and the log row the adhoc stage."""
    import asyncio

    from services.writing_memory import eval_judge as ej

    seen, logged = {}, {}

    class FakeClient:
        def generate(self, req):
            seen.update(stage=req.stage, model_tier=req.model_tier, fields=req.model_fields_set)
            return SimpleNamespace(content=json.dumps({"verdict": "pass", "score": 8, "failure_classes": [],
                                                       "unsupported_claims": []}),
                                   model_used="satellite-luna", cost_usd=0.001, input_tokens=10, output_tokens=5)

    async def fake_log(pool, **kw):
        logged.update(kw)

    monkeypatch.setattr(ej, "LLMClient", FakeClient)
    monkeypatch.setattr(ej, "record_call_with_pool", fake_log)
    v = asyncio.run(ej.judge_output(None, writer="t9_social", source_text="zz", output_text="zz", run_id="c-1",
                                    item_id="i-1", arm="calibration", model_key="luna", stage=jc.STAGE,
                                    signal_extra={"model_key": "luna"}))
    assert v.verdict == "pass" and seen["stage"] == "adhoc_aa919" and seen["model_tier"] == "luna"
    assert logged["stage"] == "adhoc_aa919" and logged["role"] == "judge"
    assert logged["quality_signal"]["model_key"] == "luna" and logged["quality_signal"]["run_id"] == "c-1"
    # R2's default call is unchanged: the route (no override)
    asyncio.run(ej.judge_output(None, writer="t9_social", source_text="zz", output_text="zz", run_id="r",
                                item_id="i", arm="control"))
    assert seen["stage"] == "writing_memory_eval" and seen["model_tier"] is None
    assert "model_tier" not in seen["fields"]


# ── calibration candidate picker ───────────────────────────────────────────────────────────────────────────────────

def _item(label, *, channel="linkedin", tenant="t-a", tiers=()):
    return {"channel": channel, "tenant_id": tenant, "tiers": list(tiers), "split": "tune", "label_verdict": label}


def test_gaps_overall_per_stratum_and_missing_channels():
    items = [_item("pass")] * 6 + [_item("fail", tenant="t-b")] * 2
    g = cc.gaps("t9_social", items, target_per_label=5, stratum_target=3, extra_channels=("facebook", "linkedin"))
    keys = {(x["dim"], x["value"], x["label"]): x["need"] for x in g}
    assert keys[("all", "all", "fail")] == 3 and ("all", "all", "pass") not in keys
    assert keys[("tenant", "t-a", "fail")] == 3 and keys[("tenant", "t-b", "pass")] == 3
    assert keys[("tenant", "t-b", "fail")] == 1
    assert keys[("channel", "facebook", "pass")] == 3 and keys[("channel", "facebook", "fail")] == 3
    assert ("channel", "linkedin", "pass") not in keys and not any(x["value"] == "untiered" for x in g)


def test_lean_is_evidence_only_never_a_label():
    assert cc.lean({"human_label": "pass", "held": True}) == "pass"
    assert cc.lean({"review": "fail"}) == "fail"
    assert cc.lean({"held": True}) == "fail" and cc.lean({"edited": True}) == "fail"
    assert cc.lean({"approved": True, "published": True}) == "pass"
    assert cc.lean({"approved": True}) is None


def test_pick_prefers_the_missing_label_in_the_stratum_and_never_repeats():
    cands = [{"id": f"p-{i}", "tenant_id": "t-b", "channel": "facebook", "days": None,
              "signals": {"held": i % 2 == 0}} for i in range(10)]
    cands += [{"id": "p-other", "tenant_id": "t-a", "channel": "linkedin", "days": 12, "signals": {"held": True}}]
    gap_rows = [{"writer": "t9_social", "dim": "channel", "value": "facebook", "label": "fail", "have": 0, "need": 2},
                {"writer": "t9_social", "dim": "tenant", "value": "t-b", "label": "fail", "have": 0, "need": 1}]
    picks = cc.pick(gap_rows, cands, seed=919, oversample=2.0)
    first = [p for p in picks if p["fills"] == "channel:facebook:fail"]
    assert len(first) == 4 and all(p["lean"] == "fail" for p in first)
    assert len({p["id"] for p in picks}) == len(picks) and "p-other" not in {p["id"] for p in picks}
    assert picks == cc.pick(gap_rows, cands, seed=919, oversample=2.0)             # deterministic
    assert ("tier", "long_9d") in cc.candidate_strata(cands[-1])


def test_golden_refs_excludes_every_known_id():
    items = [{"refs": {"piece_id": "p-1", "tenant_id": "t-a"}}, {"refs": json.dumps({"tour_id": "r-1"})},
             {"refs": {}}]
    assert cc.golden_refs(items) == {"p-1", "r-1"}
