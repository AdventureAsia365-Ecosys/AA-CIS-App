"""AA-1048 (Quality W2) — the sample-size table (sample_size.py, its doc), the deterministic detector harness
(detectors.py) and the mutation suites (mutations.py). Synthetic, invented texts only."""
import dataclasses
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
    # only the held-out side (claims.HELD_OUT_PCT of the objects) may pass: the total is the held-out need / 0.30
    assert ss.held_out_claims_needed(fire_rate=0.25, violation_rate=0.10, observed_precision=1.0,
                                     bar=deterministic) == 150
    assert ss.label_budget(fire_rate=0.25, violation_rate=0.10, observed_precision=1.0, bar=deterministic) == 500
    assert ss.label_budget(fire_rate=0.5, violation_rate=0.5, observed_precision=0.95, bar=deterministic) == 127
    assert ss.label_budget(fire_rate=0, violation_rate=0.5, observed_precision=1.0, bar=llm) is None
    rows = ss.budget_rows([
        {"detector": "a", "class": "fact.number", "fire_rate": 0.25, "violation_rate": 0.1, "precision": 1.0},
        {"detector": "b", "class": "fact.name", "kind": "llm", "fire_rate": 0.5, "violation_rate": 0.5,
         "observed_precision": 0.95},
        {"detector": "c", "class": "fact.name", "fire_rate": 0.5, "violation_rate": 0.5, "precision": None}],
        budget=300)
    assert [r["status"] for r in rows] == ["stays warn (over the label budget)", "reachable", "out of reach"]
    assert rows[1]["held_out_claims_needed"] == 66 and rows[1]["claims_to_label"] == 220
    assert rows[1]["bar"] == "llm_instrument" and rows[1]["held_out_pct"] == 0.30


def test_label_budget_leaves_enough_on_the_held_out_side():
    """Review S272 repro: fire_rate 0.1, violation_rate 0.1, precision 1.0, deterministic bar. The old budget (150)
    left about 45 held-out claims (4.5 fires, 4.5 violations against 12 and 15 needed)."""
    from services.ground_truth.claims import HELD_OUT_PCT

    _llm, deterministic = ss.BARS
    total = ss.label_budget(fire_rate=0.1, violation_rate=0.1, observed_precision=1.0, bar=deterministic)
    assert total == 500
    held = total * HELD_OUT_PCT
    assert held * 0.1 >= ss.min_flagged(1.0, deterministic) and held * 0.1 >= deterministic.n_pos
    with pytest.raises(ValueError):
        ss.label_budget(fire_rate=0.1, violation_rate=0.1, observed_precision=1.0, bar=deterministic, held_out_pct=0)
    (row,) = ss.budget_rows([{"detector": "x", "class": "fact.number", "fire_rate": 0.1, "violation_rate": 0.1,
                              "precision": 1.0}], budget=150)
    assert row["claims_to_label"] == 500 and row["status"].startswith("stays warn")


# ── detectors ──────────────────────────────────────────────────────────────────────────────────────────────────


def _claim(text, field="body", writer="t9_blog", scope=None, recorded=None, h=None, split="held_out"):
    return det.ClaimInput(h or f"h-{abs(hash((text, field)))}", writer, field, text, SOURCE, scope_name=scope,
                          recorded=recorded or {}, split=split)


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


AGREE = {"fact.number": 0.8, "fact.name": 0.8}


def test_measure_rows_precision_recall_mode_and_errors():
    claims = [_claim(f"claim {i}", h=f"c{i}") for i in range(40)]
    labels = {f"c{i}": ("fact.number", "violation" if i < 20 else "ok") for i in range(40)}
    labels["c39"] = ("fact.name", "ok")                                   # another class, never fired on
    good = _stub("good", {f"c{i}" for i in range(19)})                    # 19 tp, 0 fp
    noisy = _stub("noisy", {f"c{i}" for i in range(10)} | {f"c{i}" for i in range(20, 30)}, kind="tripwire")

    def boom(c):
        raise RuntimeError("x")
    broken = det.Detector("broken", ("fact.number",), "rule", boom)
    rows = {r["detector"]: r for r in det.measure((good, noisy, broken), claims, labels, label_set="CL-1@1.0.0",
                                                  agreement=AGREE)}
    g = rows["good"]
    assert (g["tp"], g["fp"], g["fn"], g["n_pos"], g["n_labelled"]) == (19, 0, 1, 20, 40)
    assert g["precision"] == 1.0 and g["recall"] == 0.95 and g["passes"] and g["mode"] == det.MODE_ENFORCE
    assert g["split"] == "held_out" and g["agreement"] == 0.8 and g["fire_rate"] == round(19 / 40, 4)
    n = rows["noisy"]
    assert n["precision"] == 0.5 and not n["passes"] and n["mode"] == det.MODE_ROUTE
    assert rows["broken"]["errors"] == 40 and rows["broken"]["n_labelled"] == 0
    per_writer = det.measure((good,), claims, labels, label_set="x", pooled=False, agreement=AGREE)
    assert per_writer[0]["writers"] == "t9_blog"
    th = det.thresholds_rows(list(rows.values()))
    entry = th["thresholds"]["det.good.fact.number"]
    assert entry["value"] == "enforce" and entry["calibration"]["calibrated"] is True
    assert entry["calibration"]["split"] == "held_out" and entry["calibration"]["agreement"] == 0.8
    assert th["thresholds"]["det.noisy.fact.number"]["calibration"]["calibrated"] is False
    with pytest.raises(ValueError, match="split"):
        det.measure((good,), claims, labels, label_set="x", split="dev")


def test_fires_on_claims_of_other_classes_count_against_precision():
    """A gate in enforce mode holds on ANY fire: a fire on an ok claim of another class, or on a violation of an
    unrelated class, is a false positive of the detector (review S272)."""
    claims = [_claim(f"claim {i}", h=f"c{i}") for i in range(30)]
    labels = {f"c{i}": ("fact.number", "violation") for i in range(20)}
    labels.update({f"c{i}": ("fact.inclusion", "ok") for i in range(20, 25)})
    labels.update({f"c{i}": ("fact.name", "violation") for i in range(25, 30)})
    d = _stub("d", {f"c{i}" for i in range(30)})
    (row,) = det.measure((d,), claims, labels, label_set="x", agreement=AGREE)
    assert (row["fired"], row["tp"], row["fp_ok"], row["fp_other_class"], row["fp"]) == (30, 20, 5, 5, 10)
    assert row["precision"] == round(20 / 30, 4) and row["recall"] == 1.0 and not row["passes"]
    assert row["n_labelled"] == 30 and row["fire_rate"] == 1.0 and row["violation_rate"] == round(20 / 30, 4)
    multi = det.Detector("m", ("fact.number", "fact.name"), "rule", lambda c: True)
    rows = {r["class"]: r for r in det.measure((multi,), claims, labels, label_set="x", agreement=AGREE)}
    assert rows["fact.name"]["tp"] == 25 and rows["fact.name"]["tp_class"] == 5 and rows["fact.name"]["n_pos"] == 5
    assert rows["fact.number"]["fp"] == 5                                    # only the ok claims are false here


def test_only_a_held_out_measurement_with_known_agreement_passes():
    claims = [_claim(f"claim {i}", h=f"c{i}", split="train" if i % 2 else "held_out") for i in range(80)]
    labels = {f"c{i}": ("fact.number", "violation" if i < 40 else "ok") for i in range(80)}
    d = _stub("d", {f"c{i}" for i in range(40)})
    held = det.measure((d,), claims, labels, label_set="x", agreement=AGREE)[0]
    assert held["n_labelled"] == 40 and held["passes"] and held["mode"] == det.MODE_ENFORCE
    for split in ("train", "all"):
        row = det.measure((d,), claims, labels, label_set="x", split=split, agreement=AGREE)[0]
        assert row["meets_bar"] and not row["passes"] and row["mode"] == det.MODE_WARN, split
    for agreement in (None, {"fact.number": 0.69}, {"fact.number": None}):
        row = det.measure((d,), claims, labels, label_set="x", agreement=agreement)[0]
        assert not row["meets_bar"] and not row["passes"] and row["mode"] == det.MODE_WARN, agreement
    _llm, deterministic = ss.BARS
    assert deterministic.agreement == 0.7
    assert ss.passes(20, 20, 20, deterministic) is False and ss.passes(20, 20, 20, deterministic, agreement=0.7)


def test_recorded_producers_are_measured_on_their_records_only():
    claims = [_claim("a", h="r1", recorded={"a3_place_card_check": True}),
              _claim("b", h="r2", recorded={"a3_place_card_check": False}), _claim("c", h="r3")]
    labels = {"r1": ("fact.name", "violation"), "r2": ("fact.name", "ok"), "r3": ("fact.name", "violation")}
    rec = det.recorded_detectors(claims)
    assert [d.name for d in rec] == ["recorded:a3_place_card_check"] and rec[0].classes == ()
    (row,) = det.measure(rec, claims, labels, label_set="CL-1")
    assert (row["class"], row["n_labelled"], row["tp"], row["fp"], row["mode"]) == ("fact.name", 2, 1, 0, "warn")
    assert row["classes_declared"] is False


def _recorded(h, fired, classes=("fact.number",)):
    return det.ClaimInput(h, "t9_blog", "body", f"claim {h}", SOURCE, recorded={"p": fired},
                          recorded_classes={"p": classes} if classes is not None else {}, split="held_out")


def test_recorded_producer_is_measured_against_its_declared_classes_only():
    """Review S272 repro: producer p fires on 15 fact.number and 20 scope.framing violations (and not on 5 fact.number
    ok claims). Measured against every class, both rows were enforce at precision 1.0; as a number checker its
    precision is 15 / 35."""
    labels = {}
    claims = []
    for i in range(15):
        claims.append(_recorded(f"n{i}", True))
        labels[f"n{i}"] = ("fact.number", "violation")
    for i in range(20):
        claims.append(_recorded(f"s{i}", True))
        labels[f"s{i}"] = ("scope.framing", "violation")
    for i in range(5):
        claims.append(_recorded(f"o{i}", False))
        labels[f"o{i}"] = ("fact.number", "ok")
    agree = {"fact.number": 0.9, "scope.framing": 0.9}
    rec = det.recorded_detectors(claims)
    assert rec[0].classes == ("fact.number",)
    (row,) = det.measure(rec, claims, labels, label_set="x", agreement=agree)
    assert (row["class"], row["fired"], row["tp"], row["fp_other_class"]) == ("fact.number", 35, 15, 20)
    assert row["precision"] == round(15 / 35, 4) and row["mode"] == det.MODE_WARN and not row["passes"]
    # no declared class: explored against every class, never enforce
    bare = [dataclasses.replace(c, recorded_classes={}) for c in claims]
    rows = det.measure(det.recorded_detectors(bare), bare, labels, label_set="x", agreement=agree)
    assert {r["class"] for r in rows} == {"fact.number", "scope.framing"}
    assert all(r["mode"] == det.MODE_WARN and not r["meets_bar"] and r["classes_declared"] is False for r in rows)


def test_a_class_the_detector_never_catches_does_not_pass():
    """Review S272 repro: classes (other.f6, other.c3); 20 held-out f6 violations all fired, 15 c3 violations none
    fired: the c3 row was enforce with tp_class 0."""
    claims = [_claim(f"c{i}", h=f"c{i}") for i in range(35)]
    labels = {f"c{i}": ("other.f6", "violation") for i in range(20)}
    labels.update({f"c{i}": ("other.c3", "violation") for i in range(20, 35)})
    d = det.Detector("x", ("other.f6", "other.c3"), "rule", lambda c: int(c.claim_hash[1:]) < 20)
    rows = {r["class"]: r for r in det.measure((d,), claims, labels, label_set="x",
                                                agreement={"other.f6": 0.9, "other.c3": 0.9})}
    assert rows["other.f6"]["passes"] and rows["other.f6"]["mode"] == det.MODE_ENFORCE
    c3 = rows["other.c3"]
    assert c3["tp_class"] == 0 and c3["recall"] == 0.0 and not c3["passes"] and c3["mode"] == det.MODE_WARN
    th = det.thresholds_rows(list(rows.values()))["thresholds"]
    assert th["det.x.other.c3"]["calibration"]["calibrated"] is False


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
