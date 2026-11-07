"""AA-1048 (Quality W2) — the sample-size table (sample_size.py, its doc), the deterministic detector harness
(detectors.py) and the mutation suites (mutations.py). Synthetic, invented texts only."""
import os

import pytest

from services.ground_truth import detectors as det
from services.ground_truth import mutations as mut
from services.ground_truth import sample_size as ss

DOC = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "calibration", "aa1048-sample-size.md")
SOURCE = ("Day 1: Arrive in Velmora and transfer to the Harbour Lodge. Dinner included.\n"
          "Day 2: Walk 12 km to Ostrin Valley. Optional: kayak on Brannoch Lake.\n"
          "Day 3: Drive to Kalvey Pass and depart.")


# ── sample size ────────────────────────────────────────────────────────────────────────────────────────────────


def test_wilson_lower_bound_known_values():
    assert ss.wilson_lower(0, 0) is None
    assert ss.wilson_lower(10, 10) == pytest.approx(0.7225, abs=1e-4)
    assert ss.wilson_lower(43, 47) == pytest.approx(0.8007, abs=1e-3)
    assert ss.wilson_lower(0, 5) == 0.0
    with pytest.raises(ValueError):
        ss.wilson_lower(6, 5)


def test_min_flagged_is_conservative_and_monotone():
    llm, deterministic = ss.BARS
    assert ss.min_flagged(0.89, llm) is None
    assert ss.min_flagged(0.92, llm) == 47            # the audit's "about 47 flagged"
    assert ss.min_flagged(0.90, llm) == 70
    assert ss.min_flagged(1.0, deterministic) == 12
    needs = [ss.min_flagged(p, llm) for p in ss.OBSERVED]
    assert needs == sorted(needs, reverse=True)
    assert ss.passes(45, 47, 20, llm) is True and ss.passes(45, 47, 19, llm) is False
    assert ss.passes(0, 0, 30, llm) is False


def test_the_doc_holds_the_generated_table():
    with open(DOC, encoding="utf-8") as fh:
        doc = fh.read()
    assert ss.markdown(ss.table()) in doc


def test_label_budget_and_budget_rows():
    llm, deterministic = ss.BARS
    assert ss.label_budget(fire_rate=0.25, violation_rate=0.10, observed_precision=1.0, bar=deterministic) == 150
    assert ss.label_budget(fire_rate=0.5, violation_rate=0.5, observed_precision=0.95, bar=deterministic) == 38
    assert ss.label_budget(fire_rate=0, violation_rate=0.5, observed_precision=1.0, bar=llm) is None
    rows = ss.budget_rows([
        {"detector": "a", "class": "fact.number", "fire_rate": 0.25, "violation_rate": 0.1, "precision": 1.0},
        {"detector": "b", "class": "fact.name", "kind": "llm", "fire_rate": 0.5, "violation_rate": 0.5,
         "observed_precision": 0.95},
        {"detector": "c", "class": "fact.name", "fire_rate": 0.5, "violation_rate": 0.5, "precision": None}],
        budget=100)
    assert [r["status"] for r in rows] == ["stays warn (over the label budget)", "reachable", "out of reach"]
    assert rows[1]["claims_to_label"] == 66 and rows[1]["bar"] == "llm_instrument"


# ── detectors ──────────────────────────────────────────────────────────────────────────────────────────────────


def _claim(text, field="body", writer="t9_blog", scope=None, recorded=None, h=None):
    return det.ClaimInput(h or f"h-{abs(hash((text, field)))}", writer, field, text, SOURCE, scope_name=scope,
                          recorded=recorded or {})


@pytest.mark.parametrize("name,positive,negative", [
    ("novel_numbers", "You walk 40 km to Ostrin Valley.", "You walk 12 km to Ostrin Valley."),
    ("meal_clock_regex", "Breakfast is included at the lodge.", "You walk to Ostrin Valley."),
    ("day_codes", "Day 6 ends at Kalvey Pass.", "Day 2 ends in Ostrin Valley."),
    ("elephant_riding", "Ride an elephant through the Velmora forest.", "Watch birds in the Velmora forest."),
    ("t3_lexicon", "You stay in a luxury lodge in Velmora.", "You stay at the Harbour Lodge."),
    ("F1_grounding", "You walk 40 km to Ostrin Valley.", "You walk 12 km to Ostrin Valley."),
    ("s1_names", "Then walk on to Dravenmoor Castle.", "Then walk on to Ostrin Valley."),
    ("CONCRETE_TRIP_RE", "Dinner is included.", "The valley is quiet."),
])
def test_production_detectors_fire_on_positives_only(name, positive, negative):
    (d,) = det.by_name([name])
    assert d.fn(_claim(positive)) is True
    assert d.fn(_claim(negative)) is False


def test_meta_band_and_scope_detectors():
    (meta,) = det.by_name(["meta_band"])
    assert meta.fn(_claim("Too short.", field="meta_description")) is True
    assert meta.fn(_claim("x" * 150, field="meta_description")) is False
    assert meta.fn(_claim("Too short.", field="body")) is False
    (f12,) = det.by_name([det.qr.F12_SCOPE])
    assert f12.fn(_claim("A walk to Ostrin Valley.")) is False           # no scope: nothing to compare
    with pytest.raises(ValueError, match="unknown detector"):
        det.by_name(["nope"])


def _stub(name, fired, kind="rule", classes=("fact.number",)):
    return det.Detector(name, classes, kind, lambda c: c.claim_hash in fired)


def test_measure_rows_precision_recall_mode_and_errors():
    claims = [_claim(f"claim {i}", h=f"c{i}") for i in range(40)]
    labels = {f"c{i}": ("fact.number", "violation" if i < 20 else "ok") for i in range(40)}
    labels["c39"] = ("fact.name", "ok")                                   # another class: not counted
    good = _stub("good", {f"c{i}" for i in range(19)})                    # 19 tp, 0 fp
    noisy = _stub("noisy", {f"c{i}" for i in range(10)} | {f"c{i}" for i in range(20, 30)}, kind="tripwire")

    def boom(c):
        raise RuntimeError("x")
    broken = det.Detector("broken", ("fact.number",), "rule", boom)
    rows = {r["detector"]: r for r in det.measure((good, noisy, broken), claims, labels, label_set="CL-1@1.0.0")}
    g = rows["good"]
    assert (g["tp"], g["fp"], g["fn"], g["n_pos"], g["n_labelled"]) == (19, 0, 1, 20, 39)
    assert g["precision"] == 1.0 and g["recall"] == 0.95 and g["passes"] and g["mode"] == det.MODE_ENFORCE
    n = rows["noisy"]
    assert n["precision"] == 0.5 and not n["passes"] and n["mode"] == det.MODE_ROUTE
    assert rows["broken"]["errors"] == 40 and rows["broken"]["n_labelled"] == 0
    per_writer = det.measure((good,), claims, labels, label_set="x", pooled=False)
    assert per_writer[0]["writers"] == "t9_blog"
    th = det.thresholds_rows(list(rows.values()))
    entry = th["thresholds"]["det.good.fact.number"]
    assert entry["value"] == "enforce" and entry["calibration"]["calibrated"] is True
    assert th["thresholds"]["det.noisy.fact.number"]["calibration"]["calibrated"] is False


def test_recorded_producers_are_measured_on_their_records_only():
    claims = [_claim("a", h="r1", recorded={"a3_place_card_check": True}),
              _claim("b", h="r2", recorded={"a3_place_card_check": False}), _claim("c", h="r3")]
    labels = {"r1": ("fact.name", "violation"), "r2": ("fact.name", "ok"), "r3": ("fact.name", "violation")}
    rec = det.recorded_detectors(claims)
    assert [d.name for d in rec] == ["recorded:a3_place_card_check"]
    (row,) = det.measure(rec, claims, labels, label_set="CL-1")
    assert (row["class"], row["n_labelled"], row["tp"], row["fp"], row["mode"]) == ("fact.name", 2, 1, 0, "warn")


# ── mutation suites ────────────────────────────────────────────────────────────────────────────────────────────


def test_suite_is_deterministic_and_complete():
    a = mut.build_suite(n_texts=3, seed=7)
    assert a == mut.build_suite(n_texts=3, seed=7)
    assert len(a) == 4 * 3 * (4 + len(mut.CLASSES))
    kinds = {c.kind for c in a}
    assert kinds == {"clean", *mut.CLASSES}
    assert all("Day 2" in c.source for c in a if c.writer == "a1")
    assert all(" — " in c.source for c in a if c.writer.startswith("t9"))
    assert set(mut.POLICY_CLASS) == set(mut.RECALL_BARS)


def test_run_suite_with_stub_detectors():
    cases = mut.build_suite(writers=("t2",), n_texts=2, seed=3)
    catch_numbers = det.Detector("n", (), "rule", lambda c: " km " in c.text and "second day" in c.text and any(
        ch.isdigit() for ch in c.text) and not c.text.endswith("Ostrin Valley."))
    trip = det.Detector("t", (), "tripwire", lambda c: True)
    rep = mut.run_suite(cases, (catch_numbers, trip))
    assert rep["false_findings"]["t2"]["clean"] == 8
    assert rep["tripwires_on_clean"] == {"t": 8}
    assert rep["recall"]["t2"]["meal"]["recall"] == 0.0 and not rep["recall"]["t2"]["meal"]["passes"]
    assert any("t2:" in ln for ln in mut.summary_lines(rep))


def test_real_detectors_hold_their_measured_floor_on_the_suite():
    """A regression guard on the measured numbers (S272): the deterministic detectors catch every number, day,
    meal and clock mutant and flag no clean sentence. Inclusion of services and S1 optional-as-fixed have no
    deterministic detector today (0 recall): they stay with the claim instrument (implementation notes)."""
    rep = mut.run_suite(mut.build_suite(n_texts=6, seed=1048))
    for writer in mut.WRITERS:
        rec = rep["recall"][writer]
        for cls in ("number", "structural", "meal", "clock", "name"):
            assert rec[cls]["passes"], (writer, cls, rec[cls])
        assert rep["false_findings"][writer]["passes"], rep["false_findings"][writer]
    assert rep["recall"]["t9_blog"]["optional"]["passes"]
