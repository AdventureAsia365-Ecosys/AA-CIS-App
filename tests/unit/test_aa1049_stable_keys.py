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
# The stable-key guard is opt-in (T9_REGRESSION_GUARD_MODE=stable_keys), off until the tech lead turns it on; the
# default path is main's gate-name rule. The review's cases below pin the default decision to main's, and the opt-in
# decision on them (the two S272 review findings, fixed S275: docs/implementation-notes/AA-1049-l3-stable-keys.md).

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
    ("f15x2_to_f1x1", [_f15(S1, S2)], [F1_NEW], True, True),
    # review S272 round 3, finding 3: LinkedIn under-floor F14 + F15 s1 -> far-over F14 + F15 s2 (same gates)
    ("f14_floor_to_far_over", [_f14(UNDER), _f15(S1)], [_f14(FAR_OVER), _f15(S2)], False, False),
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
    """Opt-in only: the stable-key decision on the same cases. S275: both review findings now decide like main (the
    truth-clean attempt ships; the far-over post is kept, held, never reverted into the waived short one)."""
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


# ------------------------------------------------------------------- S275: the two S272 review findings, fixed

def _f2(*sentences: str) -> dict:
    return {"gate": qr.F2_SUPERLATIVES, "passed": False, "blocking": True,
            "violations": [f"second stock superlative: '{q}'" for q in sentences]}


def _f1(sentence: str) -> dict:
    return {"gate": qr.F1_GROUNDING, "passed": False, "blocking": True,
            "violations": [f"not present in the content seed: '{sentence}'"]}


HASHTAGS = "6 hashtags, more than 3: '#a #b #c #d #e #f'"
OVER = "post is 270 words, over the 120–250-word band for this brand on LinkedIn"
N1 = "The ride covers 42 km in four hours."
N2 = "Each boat carries 18 guests to the Velmora reef."


def test_f14_keys_by_violation_class_not_by_count():
    """Finding 3: F14 was one whole-gate key, so an under-floor and a far-over block looked the same."""
    under = sk.blocking_keys([_f14(UNDER)])
    far = sk.blocking_keys([_f14(FAR_OVER)])
    assert under == {(qr.F14_SOCIAL_SHAPE, sk.F14_UNDER_FLOOR)} and far == {(qr.F14_SOCIAL_SHAPE, sk.F14_FAR_OVER)}
    shorter = UNDER.replace("60 words", "41 words")
    assert sk.blocking_keys([_f14(shorter)]) == under
    both = {**_f14(FAR_OVER), "violations": [FAR_OVER, HASHTAGS]}
    assert sk.blocking_keys([both]) == {(qr.F14_SOCIAL_SHAPE, sk.F14_FAR_OVER), (qr.F14_SOCIAL_SHAPE, sk.F14_HASHTAGS)}
    assert sk.f14_class(OVER) == sk.F14_OVER and sk.f14_class("something else") == sk.WHOLE_GATE


def test_hold_keys_leave_out_style_and_the_floor_block():
    ledger = [_f15(S1), _f2(S2), _f14(UNDER), _f9((A, "a"))]
    assert sk.hold_keys(ledger) == sk.blocking_keys([_f9((A, "a"))])
    far = {**_f14(FAR_OVER), "violations": [FAR_OVER, HASHTAGS]}
    assert sk.hold_keys([far]) == {(qr.F14_SOCIAL_SHAPE, sk.F14_FAR_OVER)}  # the hashtag residue never holds
    assert sk.has_floor_block([_f14(UNDER)]) and not sk.has_floor_block([far])


def test_style_traded_for_a_truth_failure_is_reverted_even_when_main_keeps():
    """Finding 2, the case main itself keeps: two style GATES cleared for one new F1 number (1 gate < 2 gates)."""
    prev, cur = [_f15(S1), _f2(S2)], [_f1(N1)]
    assert not _main_rule(prev, cur)
    c = sk.compare(prev, cur)
    assert c["revert"] and c["rule"] == sk.RULE_HOLD and c["new_hold"] == {(qr.F1_GROUNDING, _norm(N1))}


def _norm(text: str) -> str:
    return next(iter(sk.blocking_keys([_f1(text)])))[1]


def test_a_rewrite_that_clears_more_truth_findings_than_it_adds_is_still_kept():
    """D-guard-2 stays: three F9 claims cleared for one new F1 number (hold-capable 3 -> 1)."""
    c = sk.compare([_f9((A, "a"), (B, "b"), (C, "c"))], [_f1(N1)])
    assert c["new_hold"] and c["cur_hold_keys"] < c["prev_hold_keys"] and not c["revert"]
    assert c["rule"] == sk.RULE_KEEP


def test_a_truth_swap_inside_one_gate_is_reverted_even_with_style_cleared():
    """F9 A + F15 s -> F9 B: the all-key rule keeps (1 key < 2); the hold rule reverts (a new claim, 1 >= 1)."""
    c = sk.compare([_f9((A, "a")), _f15(S1)], [_f9((B, "b"))])
    assert c["revert"] and c["rule"] == sk.RULE_HOLD


def test_an_under_floor_attempt_is_reverted_to_only_when_main_reverts():
    """Finding 3: from an attempt with the AA-780 under-floor block the stable-key mode takes main's decision, so it
    never reverts into the short post (the call site would waive its block and ship it approved) where main keeps."""
    prev = [_f14(UNDER), _f15(S1)]
    for cur in ([_f14(FAR_OVER), _f15(S2)], [_f15(S2)], [_f14(FAR_OVER)], [_f15(S1), _f2(S2)]):
        c = sk.compare(prev, cur)
        assert c["rule"] == sk.RULE_MAIN_FLOOR and c["revert"] is _main_rule(prev, cur)
    c = sk.compare(prev, [_f15(S2), _f1(N1)])  # a new F1 gate, 2 >= 2 gates: main reverts, so does the opt-in mode
    assert c["revert"] and _main_rule(prev, [_f15(S2), _f1(N1)])


def test_a_new_under_floor_block_is_never_masked_by_an_earlier_f14_key():
    """Finding 3, other direction: far-over -> under-floor was the same (F14, *) key, so no new key and kept (the
    under-floor block never holds: the short post would ship). Now a new key in the same gate: reverted to the held
    far-over attempt."""
    c = sk.compare([_f14(FAR_OVER)], [_f14(UNDER)])
    assert c["new"] == {(qr.F14_SOCIAL_SHAPE, sk.F14_UNDER_FLOOR)} and c["revert"] and c["rule"] == sk.RULE_KEYS


_POOL = {
    "f15": _f15(S1), "f15b": _f15(S2), "f2": _f2(S2), "f1": _f1(N1), "f1b": _f1(N2), "f9": _f9((A, "a")),
    "f9b": _f9((B, "b"), (C, "c")), "under": _f14(UNDER), "far": _f14(FAR_OVER),
    "far_tags": {**_f14(FAR_OVER), "violations": [FAR_OVER, HASHTAGS]},
}


def _ledgers():
    import itertools
    names = sorted(_POOL)
    for n in (1, 2):
        for combo in itertools.combinations(names, n):
            if sum(c.startswith(("under", "far")) for c in combo) > 1:  # one F14_social_shape result per attempt
                continue
            yield combo, [_POOL[c] for c in combo]


def test_fail_closed_over_every_pool_pair():
    """Over every pair of 1-2-result attempts from the pool: the opt-in mode never keeps a rewrite that holds over a
    previous attempt that does not (unless it has the under-floor block: then it is main's decision), and never
    reverts into an under-floor attempt where main keeps."""
    from services.acp_content_writing.quality_gates import hold_failure
    pairs = 0
    for _pn, prev in _ledgers():
        for _cn, cur in _ledgers():
            pairs += 1
            c = sk.compare(prev, cur)
            if sk.has_floor_block(prev):
                assert c["revert"] is _main_rule(prev, cur), (_pn, _cn)
                continue
            if hold_failure(prev) is None and hold_failure(cur) is not None:
                assert c["revert"], (_pn, _cn)
    assert pairs > 1000
