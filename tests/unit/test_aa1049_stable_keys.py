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


@pytest.mark.parametrize("raw, mode", [(None, "gate_names"), ("", "gate_names"), ("gate_names", "gate_names"),
                                       ("STABLE_KEYS", "stable_keys"), ("stable_keys", "stable_keys"),
                                       ("nonsense", "gate_names")])
def test_guard_mode_switch(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv(service.REGRESSION_GUARD_ENV, raising=False)
    else:
        monkeypatch.setenv(service.REGRESSION_GUARD_ENV, raw)
    assert service.regression_guard_mode() == mode


def test_the_guard_mode_is_classified_as_an_output_switch():
    from services.writing_memory import config_fp as cf
    for writer in ("t9_blog", "t9_social"):
        assert service.REGRESSION_GUARD_ENV in cf.MODE_FLAGS[writer]


# ------------------------------------------------------------------- S272 orchestrator decision: default = main
# The stable-key guard is opt-in (T9_REGRESSION_GUARD_MODE=stable_keys) until W2 data; the default path is main's
# gate-name rule. The review's cases below pin the default decision to main's, and show what the opt-in mode does on
# them (two open review findings, docs/implementation-notes/AA-1049-l13-t9.md "Known limits").

def _f15(*sentences: str) -> dict:
    return {"gate": qr.F15_VOICE, "passed": False, "blocking": True,
            "violations": [f"stock phrase: '{q}'" for q in sentences]}


def _f14(violation: str) -> dict:
    return {"gate": "F14_social_shape", "passed": False, "blocking": True, "violations": [violation]}


S1, S2 = "Hidden gems await around every corner of Dunmere.", "An unforgettable journey awaits you in Brecca."
F1_NEW = {"gate": qr.F1_GROUNDING, "passed": False, "blocking": True,
          "violations": ["not present in the content seed: 'The ride covers 42 km in four hours.'"]}
UNDER = "post is 60 words, under the band 120–250 words for this brand on LinkedIn — rewrite it longer"
FAR_OVER = "post is 320 words, far over the 120–250-word band for this brand on LinkedIn — cut it"
REVIEW_CASES = [
    # review S272 round 3, finding 2: two style findings traded for a new product-truth (F1) finding
    ("f15x2_to_f1x1", [_f15(S1, S2)], [F1_NEW], True, False),
    # review S272 round 3, finding 3: LinkedIn under-floor F14 + F15 s1 -> far-over F14 + F15 s2 (same gates)
    ("f14_floor_to_far_over", [_f14(UNDER), _f15(S1)], [_f14(FAR_OVER), _f15(S2)], False, True),
    # the S271 cases of this file
    ("f9_swap", [_f9((A, "a"))], [_f9((B, "b"))], False, True),
    ("f9x3_to_f17", [_f9((A, "a"), (B, "b"), (C, "c"))],
     [_quoted(qr.F17_TRIP_FACT, "Boats leave the Dunmere slip at six sharp each day.")], True, False),
]


def _main_rule(prev, cur) -> bool:
    """Main's AA-528 decision, written out from main's call site: a new failing gate and not fewer failing gates."""
    pb, cb = service._regression_failures(prev), service._regression_failures(cur)
    return bool(cb - pb) and len(cb) >= len(pb)


@pytest.mark.parametrize("name, prev, cur, main_reverts, keys_revert", REVIEW_CASES, ids=[c[0] for c in REVIEW_CASES])
def test_the_default_guard_decision_is_mains(monkeypatch, name, prev, cur, main_reverts, keys_revert):
    monkeypatch.delenv(service.REGRESSION_GUARD_ENV, raising=False)
    new, revert, _, _ = service.guard_decision(prev, cur)
    assert bool(new and revert) is main_reverts is _main_rule(prev, cur)
    monkeypatch.setenv(service.REGRESSION_GUARD_ENV, service.GUARD_GATE_NAMES)
    assert service.guard_decision(prev, cur)[:2] == (new, revert)


@pytest.mark.parametrize("name, prev, cur, main_reverts, keys_revert", REVIEW_CASES, ids=[c[0] for c in REVIEW_CASES])
def test_the_opt_in_mode_still_differs_on_the_review_cases(monkeypatch, name, prev, cur, main_reverts, keys_revert):
    """Opt-in only: the stable-key decision on the same cases (the two review findings stay open until W2)."""
    monkeypatch.setenv(service.REGRESSION_GUARD_ENV, service.GUARD_STABLE_KEYS)
    new, revert, _, _ = service.guard_decision(prev, cur)
    assert bool(new and revert) is keys_revert


def test_default_review_cases_by_name(monkeypatch):
    """F15x2 -> F1x1 reverts (the truth-clean attempt ships); the F14 floor/far-over swap keeps cur (main: no new gate),
    so the AA-780 waiver never turns the far-over post into an approved short one."""
    monkeypatch.delenv(service.REGRESSION_GUARD_ENV, raising=False)
    new, revert, prev_g, cur_g = service.guard_decision([_f15(S1, S2)], [F1_NEW])
    assert new == {qr.F1_GROUNDING} and revert and prev_g == {qr.F15_VOICE}
    new, revert, _, _ = service.guard_decision([_f14(UNDER), _f15(S1)], [_f14(FAR_OVER), _f15(S2)])
    assert new == set()
