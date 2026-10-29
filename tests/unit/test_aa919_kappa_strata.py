"""AA-919 S257 — judge calibration per stratum of the golden set (channel, tenant, tier, split) and overall.

Pure ($0, no DB): `eval_checks.item_strata`, `stratum_stats`, `kappa_by_stratum`, `strata_below`,
`strata_label_counts`, `kappa_max`; the R2 runner stores the rows in `details.kappa_strata` and keeps the gate on the
overall kappa; the Releases tab summary passes the rows through, shape-checked.
"""
from __future__ import annotations

import pytest

from services.writing_memory import config as cfg
from services.writing_memory import eval_checks as ec
from services.writing_memory import r2

TEN_A = "00000000-0000-4000-8000-00000000000a"
TEN_B = "00000000-0000-4000-8000-00000000000b"


def _rec(label, judge, *, channel="linkedin", tenant=TEN_A, tiers=(), split="tune"):
    item = {"channel": channel, "tenant_id": tenant, "tiers": list(tiers), "split": split}
    return {"label": label, "judge": judge, "strata": ec.item_strata(item)}


def _row(rows, dim, value):
    return next(r for r in rows if r["dim"] == dim and r["value"] == value)


# ── strata of an item ──────────────────────────────────────────────────────────────────────────────────────────────

def test_item_strata_reads_the_golden_item_fields():
    s = ec.item_strata({"channel": "linkedin", "tenant_id": TEN_A, "tiers": ["long_9d", "fact_dense", "post_edit"],
                        "split": "holdout"})
    assert s == [("channel", "linkedin"), ("tenant", TEN_A), ("tier", "fact_dense"), ("tier", "long_9d"),
                 ("split", "holdout")]


def test_item_strata_platform_item_without_channel_or_tier():
    assert ec.item_strata({"channel": None, "tenant_id": None, "tiers": [], "split": "trap"}) == [
        ("tenant", "platform"), ("tier", "untiered")]


# ── numbers ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_overall_row_equals_cohen_kappa_of_all_pairs():
    # the S256 cc0f75cf matrix: pass->pass 3, pass->fail 5, fail->fail 5 (kappa 0.316)
    recs = [_rec("pass", "pass")] * 3 + [_rec("pass", "fail")] * 5 + [_rec("fail", "fail")] * 5
    rows = ec.kappa_by_stratum(recs)
    overall = rows[0]
    assert (overall["dim"], overall["value"]) == ("all", "all")
    assert overall["kappa"] == ec.cohen_kappa([(r["label"], r["judge"]) for r in recs]) == 0.316
    assert overall["n"] == 13 and overall["labels"] == {"pass": 8, "fail": 5} and overall["agree"] == 8
    assert overall["pass_recall"] == 0.375 and overall["fail_recall"] == 1.0
    assert overall["kappa_max"] == 0.316          # the judge passes too few texts: kappa_max is the ceiling
    assert overall["measurable"] and overall["below"]


def test_a_stratum_can_disagree_while_the_overall_kappa_passes():
    # tenant A: perfect agreement (12 pairs); tenant B: the judge fails every pass label (10 pairs)
    a = [_rec("pass", "pass", tenant=TEN_A)] * 6 + [_rec("fail", "fail", tenant=TEN_A)] * 6
    b = [_rec("pass", "fail", tenant=TEN_B)] * 4 + [_rec("pass", "pass", tenant=TEN_B)] * 3 \
        + [_rec("fail", "fail", tenant=TEN_B)] * 3
    rows = ec.kappa_by_stratum(a + b)
    assert rows[0]["kappa"] >= cfg.MIN_KAPPA and not rows[0]["below"]
    ra, rb = _row(rows, "tenant", TEN_A), _row(rows, "tenant", TEN_B)
    assert ra["kappa"] == 1.0 and not ra["below"]
    assert rb["measurable"] and rb["kappa"] < cfg.MIN_KAPPA and rb["below"]
    assert ec.strata_below(rows) == [f"tenant:{TEN_B}"]


def test_small_or_one_class_strata_are_not_measurable_and_one_class_has_no_kappa():
    recs = [_rec("pass", "pass", tiers=("long_9d",))] * 4 + [_rec("fail", "pass")] * 2 + [_rec("pass", "pass")] * 6
    rows = ec.kappa_by_stratum(recs)
    long9 = _row(rows, "tier", "long_9d")
    assert long9["n"] == 4 and long9["kappa"] is None and long9["kappa_max"] is None
    assert long9["pass_recall"] == 1.0 and long9["fail_recall"] is None
    assert not long9["measurable"] and not long9["below"]
    untiered = _row(rows, "tier", "untiered")
    assert untiered["n"] == 8 and not untiered["measurable"] and not untiered["below"]
    assert ec.strata_below(rows) == []


def test_rows_are_ordered_by_dimension_then_value_and_capped():
    recs = [_rec("pass", "pass", tenant=f"t-{i:02d}", tiers=(f"tier{i:02d}",)) for i in range(30)]
    rows = ec.kappa_by_stratum(recs)
    assert len(rows) == ec.STRATA_MAX
    dims = [r["dim"] for r in rows]
    assert dims[0] == "all" and dims[1] == "channel"
    assert dims.index("tier") > max(i for i, d in enumerate(dims) if d == "tenant")


def test_errors_and_unknown_verdicts_are_not_pairs():
    rows = ec.kappa_by_stratum([_rec("pass", "error"), _rec("pass", None), _rec("pass", "pass")])
    assert rows[0]["n"] == 1


def test_kappa_max_matches_hand_computation():
    assert ec.kappa_max([]) is None
    assert ec.kappa_max([("pass", "pass"), ("pass", "pass")]) is None       # pe == 1
    # 8 / 5 labels, judge passes 3: pomax = 3/13 + 5/13
    pairs = [("pass", "pass")] * 3 + [("pass", "fail")] * 5 + [("fail", "fail")] * 5
    la, lb = 8 / 13, 3 / 13
    pe = la * lb + (1 - la) * (1 - lb)
    assert ec.kappa_max(pairs) == round((8 / 13 - pe) / (1 - pe), 3)


def test_strata_label_counts_show_what_the_golden_set_can_measure():
    items = ([{"channel": "linkedin", "tenant_id": TEN_A, "tiers": [], "split": "tune", "label_verdict": "pass"}] * 7
             + [{"channel": "linkedin", "tenant_id": TEN_B, "tiers": [], "split": "holdout",
                 "label_verdict": "fail"}] * 4)
    rows = ec.strata_label_counts(items)
    assert rows[0] == {"dim": "all", "value": "all", "n": 11, "labels": {"pass": 7, "fail": 4},
                       "measurable_possible": True}
    assert _row(rows, "tenant", TEN_A)["measurable_possible"] is False


def test_the_gate_threshold_is_unchanged():
    assert cfg.MIN_KAPPA == 0.6 and cfg.MIN_KAPPA_PAIRS == 10 and cfg.MIN_KAPPA_PER_LABEL == 3


# ── the R2 runner stores them ──────────────────────────────────────────────────────────────────────────────────────

def test_r2_run_stores_kappa_per_stratum_and_keeps_the_overall_gate():
    from tests.unit.test_aa919_r2_runner import FakeStore, golden, run

    items = golden(labels_fail=0)
    for it in items:
        if it["split"] == "holdout":
            it["tiers"] = ["long_9d"]
    store = FakeStore(items)
    out, _, _ = run(store, r2.R2Config(writer="a1"))
    det = store.runs[out.run_id]["details"]
    rows = det["kappa_strata"]
    assert rows[0]["dim"] == "all" and rows[0]["kappa"] == out.judge_kappa and rows[0]["n"] == det["kappa_pairs"]
    assert {(r["dim"], r["value"]) for r in rows} >= {("tenant", "platform"), ("tier", "long_9d"),
                                                       ("tier", "untiered"), ("split", "tune"), ("split", "holdout")}
    assert det["kappa_strata_below"] == [] and out.kappa_strata_below == []
    assert out.judge_counts


def test_r2_names_a_measurable_stratum_under_the_threshold_without_changing_the_gate():
    from tests.unit.test_aa919_r2_runner import FakeStore, golden, make_judge, run

    items = golden(n_tune=12, n_hold=10, labels_fail=4, bad_holdout=4)
    # every tune item is wrongly judged except the fail-labelled ones: the tune stratum disagrees
    wrong = {it["item_id"] for it in items if it["split"] == "tune" and it["label_verdict"] == "pass"}
    store = FakeStore(items)
    out, _, _ = run(store, r2.R2Config(writer="a1"), judge=make_judge(wrong_on=wrong))
    det = store.runs[out.run_id]["details"]
    tune = _row(det["kappa_strata"], "split", "tune")
    assert tune["measurable"] and tune["below"]
    assert "split:tune" in det["kappa_strata_below"]
    # the gate is still the overall one (judge_counts from the overall kappa only)
    assert out.judge_counts == ec.judge_counts(out.judge_kappa, det["kappa_pairs"], det["kappa_labels"])


@pytest.mark.parametrize("bad", [None, "x", [1, 2], [{"dim": "tier"}]])
def test_release_summary_drops_malformed_strata(bad):
    from services.writing_memory import releases as rel

    out = rel.run_summary({"kappa_strata": bad, "kappa_pairs": 13})
    assert out["kappa_pairs"] == 13 and not out.get("kappa_strata")


def test_release_summary_passes_kappa_numbers_and_strata_through():
    from services.writing_memory import releases as rel

    rows = ec.kappa_by_stratum([_rec("pass", "pass")] * 6 + [_rec("fail", "fail")] * 4 + [_rec("pass", "fail")])
    out = rel.run_summary({"kappa_pairs": 11, "kappa_labels": {"pass": 7, "fail": 4}, "kappa_strata": rows,
                           "kappa_strata_below": ["tier:x"], "judge_claims": "never shown"})
    assert out["kappa_pairs"] == 11 and out["kappa_labels"] == {"pass": 7, "fail": 4}
    assert out["kappa_strata"][0] == {k: rows[0][k] for k in rel.STRATUM_KEYS}
    assert out["kappa_strata_below"] == ["tier:x"] and "judge_claims" not in out
