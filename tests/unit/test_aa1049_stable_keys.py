"""AA-1049 W3 (ADR 0020 S5) — stable keys of the blocking findings, and the regression guard that reads them.
Invented places and brands only."""
from __future__ import annotations

import pytest

from services.acp_content_writing import service
from services.acp_content_writing import stable_keys as sk
from services.quality import registry as qr


def _f9(*claims: str, cta: bool = False) -> dict:
    body = "; ".join(f'"{q}" (claims: {c}; source: not stated)' for q, c in claims)
    v = ("audit flagged: CTA_NOT_REFLECTED, " if cta else "audit flagged: ") + \
        f"FACT_CHECK_MANUAL_CHECK — unsupported claim(s): {body}"
    return {"gate": qr.F9_CTA_FACT, "passed": False, "blocking": True, "violations": [v]}


def _quoted(gate: str, sentence: str, problem: str = "states a time the source lacks") -> dict:
    return {"gate": gate, "passed": False, "blocking": True, "violations": [f"{problem}: '{sentence}'"]}


A = "The ferry to Brecca leaves at dawn every morning of the trip."
B = "Every guest rides the old harbour tram along the Dunmere sea wall."
C = "Lunch is always served on the quay of Velmora harbour."


def test_keys_name_the_quoted_sentence_not_the_judge_wording():
    one = sk.blocking_keys([_f9((A, "a dawn ferry"))])
    two = sk.blocking_keys([{**_f9((A, "a daily departure time"))}])
    assert one == two and len(one) == 1
    gate, key = next(iter(one))
    assert gate == qr.F9_CTA_FACT and key.startswith("the ferry to brecca")


def test_tags_case_and_edge_punctuation_do_not_change_a_key():
    plain = sk.blocking_keys([_quoted(qr.F17_TRIP_FACT, A)])
    tagged = sk.blocking_keys([_quoted(qr.F17_TRIP_FACT, A.replace("dawn", "dawn [F:f3]").upper())])
    assert plain == tagged


def test_whole_gate_failures_key_on_the_gate_only():
    short = {"gate": qr.F4_EXTREME_LENGTH, "passed": False, "blocking": True, "violations": ["blog is 812 chars"]}
    shorter = {**short, "violations": ["blog is 640 chars"]}
    assert sk.blocking_keys([short]) == sk.blocking_keys([shorter]) == {(qr.F4_EXTREME_LENGTH, sk.WHOLE_GATE)}


def test_cta_failure_and_passed_or_warn_results():
    assert (qr.F9_CTA_FACT, sk.CTA_KEY) in sk.blocking_keys([_f9((A, "x"), cta=True)])
    warn = {**_quoted(qr.F12_SCOPE, A), "blocking": False}
    passed = {"gate": qr.F1_GROUNDING, "passed": True, "violations": []}
    assert sk.blocking_keys([warn, passed]) == frozenset()


def test_swapping_one_claim_for_another_is_a_regression():
    """Declared diff D-guard-1: the S271 gate-name guard saw 'no new gate' and kept the swap."""
    new, revert = sk.regressed([_f9((A, "a"))], [_f9((B, "b"))])
    assert revert and {g for g, _ in new} == {qr.F9_CTA_FACT}


def test_fixing_three_claims_while_adding_one_finding_is_kept():
    """Declared diff D-guard-2: the S271 guard reverted (a new gate F17, 1 gate >= 1 gate)."""
    prev = [_f9((A, "a"), (B, "b"), (C, "c"))]
    cur = [_quoted(qr.F17_TRIP_FACT, "Boats leave the Dunmere slip at six sharp each day.")]
    new, revert = sk.regressed(prev, cur)
    assert new and not revert


def test_a_rewrite_that_clears_whole_gates_is_never_reverted():
    """Main's gate-count rule stays a condition: F1 + F9 cleared for three F13_repeat findings is kept (reverting it
    would bring back the two product-truth failures), even though the key count grew 2 -> 3."""
    prev = [_quoted(qr.F1_GROUNDING, A, "not present in the content seed"), _f9((B, "b"))]
    cur = [{"gate": qr.F13_REPEAT, "passed": False, "blocking": True,
            "violations": [f"repeats an earlier line: '{q}'" for q in (A, B, C)]}]
    c = sk.compare(prev, cur)
    assert c["new"] and c["cur_keys"] == 3 and c["prev_keys"] == 2
    assert c["cur_gates"] == {qr.F13_REPEAT} and c["prev_gates"] == {qr.F1_GROUNDING, qr.F9_CTA_FACT}
    assert c["revert"] is False and sk.regressed(prev, cur)[1] is False


def test_same_gates_more_keys_is_reverted():
    new, revert = sk.regressed([_f9((A, "a"))], [_f9((B, "b"), (C, "c"))])
    assert revert and len(new) == 2


def test_shrank_is_a_proper_subset():
    assert sk.shrank([_f9((A, "a"), (B, "b"))], [_f9((A, "a"))])
    assert not sk.shrank([_f9((A, "a"))], [_f9((A, "a"))])
    assert not sk.shrank([_f9((A, "a"))], [_f9((B, "b"))])


@pytest.mark.parametrize("raw, mode", [(None, "stable_keys"), ("", "stable_keys"), ("gate_names", "gate_names"),
                                       ("STABLE_KEYS", "stable_keys"), ("nonsense", "stable_keys")])
def test_guard_mode_switch(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv(service.REGRESSION_GUARD_ENV, raising=False)
    else:
        monkeypatch.setenv(service.REGRESSION_GUARD_ENV, raw)
    assert service.regression_guard_mode() == mode
