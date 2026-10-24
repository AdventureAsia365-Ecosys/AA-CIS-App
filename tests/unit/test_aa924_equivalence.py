"""AA-924 PR1b — folding the gates changes no routing decision on the AA-791 fault set.

`tests/unit/fixtures/aa924_pre_fold_t10.json` freezes the AA-926-era T10 outcomes (26 distinct results) taken at the
PR1a commit, before any gate was merged. The stack now stores 24 (F2_lexicon, F15_voice); this test requires the
legacy view of every new ledger (`expand_ledger`) to be the old ledger, and passed / first_failure / hold_failure /
the sentence-repair route / the style-only stage / the rewrite feedback to be the same, after the rename map.

The one intended difference: a word on the tenant's own forbidden list that is also a platform-truth word ('cheap')
is judged once now, by the lexicon gate (case f2b-2) — "no word judged twice" (AA-924 scope 3).
"""
import json
from pathlib import Path

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair
from services.acp_content_writing import service
from services.quality import registry as qr
from tests.unit import aa791_t9_fault_set as fs

FROZEN = json.loads((Path(__file__).parent / "fixtures" / "aa924_pre_fold_t10.json").read_text(encoding="utf-8"))
FOLDED = {qr.F2_BANNED_PATTERNS, qr.F2_BRAND_FORBIDDEN, qr.F15_VOICE_WARN}
# case → the legacy gate whose result changed on purpose (the word it judged is now the lexicon's only)
INTENDED = {"f2b-2": qr.F2_PLATFORM_TRUTH}
CASES = {c.case_id: c for c in fs.FAULT_CASES + fs.CLEAN_CASES}


def _route(plan):
    return "rewrite" if plan is None else "none" if plan == [] else "repair"


def _run(case_id: str) -> dict:
    with fs.judges_pass():
        return fs.run_case(CASES[case_id])


def test_frozen_fixture_covers_the_40_cases():
    assert set(FROZEN["cases"]) == set(CASES) and len(CASES) == 40


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_legacy_view_equals_the_pre_fold_ledger(case_id):
    old = FROZEN["cases"][case_id]
    out = _run(case_id)
    legacy = qg.expand_ledger(out["gate_ledger"])
    if case_id in INTENDED:
        gate = INTENDED[case_id]
        before = next(g for g in old["ledger"] if g["gate"] == gate)
        after = next(g for g in legacy if g["gate"] == gate)
        assert not before["passed"] and after["passed"]
        assert [g for g in legacy if g["gate"] != gate] == [g for g in old["ledger"] if g["gate"] != gate]
    else:
        assert legacy == old["ledger"]
    assert not any(g["gate"] in FOLDED for g in out["gate_ledger"])
    assert out["passed"] == old["passed"]
    assert (out["first_failure"] or {}).get("gate") == old["first_failure"]
    assert [g["gate"] for g in qg.expand_ledger(out["flags"])] == old["flags"]
    hold = qg.hold_failure(out["gate_ledger"])
    assert (hold or {}).get("gate") == old["hold_failure"]
    plan = sentence_repair.plan(CASES[case_id].content_text, out["gate_ledger"])
    assert _route(plan) == _route(old["repair_plan"])
    if case_id not in INTENDED:
        assert plan == old["repair_plan"]
    assert service._style_only_repair(out["gate_ledger"]) == old["style_only_repair"]
    for ch in ("blog", "facebook"):
        feedback = service._repair_feedback(out["gate_ledger"], ch)
        expected = FROZEN["repair_feedback"][f"{case_id}|{ch}"]
        if case_id in INTENDED:
            expected = [f for f in expected if not f.startswith(INTENDED[case_id] + ":")]
        assert feedback == expected


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_held_reason_names_the_stored_entry(case_id):
    out = _run(case_id)
    status, reason, _ = service._hold_or_ship(out["gate_ledger"], out["flags"])
    hold = qg.hold_failure(out["gate_ledger"])
    assert (status == "held") == (hold is not None)
    if hold is not None:
        stored = qg.ledger_gate(hold)
        assert reason.startswith(f"{stored}: ")
        assert stored in {g["gate"] for g in out["gate_ledger"]}
        assert stored not in FOLDED


def test_channel_gate_order_is_the_old_order_with_the_fold():
    clean = next(c for c in fs.CLEAN_CASES if c.channel == "facebook")
    with fs.judges_pass():
        for ch, old in FROZEN["channel_gate_order"].items():
            r = qg.run_quality_gates(content_text=clean.content_text, atom_text=clean.atom_text, cta=clean.cta,
                                     brand_rubric_text="rubric", channel=ch, brand_pack=clean.brand_pack)
            expected = [qr.F2_LEXICON if g == qr.F2_BANNED_PATTERNS else g for g in old if g not in FOLDED
                        or g == qr.F2_BANNED_PATTERNS]
            assert [g["gate"] for g in r["gate_ledger"]] == expected, ch
            assert len(expected) == len(old) - 2
            assert [g["gate"] for g in qg.expand_ledger(r["gate_ledger"])] == old, ch
