"""AA-900 W0 (root ADR 0020 D1/D3/D4/D15) — the interim F9 policy (declared diff): F9_cta_fact holds only on a quoted,
unsupported trip activity / service / time / view with its why_not_in_source; every other F9 class warns.

Fire / no-fire twins per topic. Red on base: every one of these F9 entries blocked (the legacy mode below)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from services.acp_content_writing import f9_policy as fp
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair
from services.quality import registry as qr


def _f9(*violations, blocking=True):
    return {"gate": qr.F9_CTA_FACT, "passed": False, "blocking": blocking, "repairable": True,
            "violations": list(violations)}


def _claim(sentence, claim="x", why="the source is silent", status="manual_check", codes="FACT_CHECK_MANUAL_CHECK"):
    return f'audit {status}: {codes} — unsupported claim(s): "{sentence}" (claims: {claim}; source: {why})'


TWINS = [
    # (topic, fires: a trip promise, no fire: allowed / warn)
    ("background", "You will tour the old fort with a local guide.", "The old fort was built by a river king."),
    ("hypothetical", "Imagine yourself paddling across the lake at sunrise.", "Imagine how quiet the lake must be."),
    ("reflection", "You will spend two nights in a felt tent.", "There is a stillness here that stays with you."),
    ("scene", "Each morning your guide brings tea to the tent.",
     "As morning light spills over the ridge, monks chant."),
    ("view", "From the terrace you look out over the whole valley.", "The terrace faces the valley."),
    ("time", "We reach the pass by noon.", "Pilgrims walk the circuit at dawn."),
    ("service", "Lunch is included at a family home.", "The village is known for its noodle soup."),
]


@pytest.mark.parametrize("topic,fires,quiet", TWINS, ids=[t[0] for t in TWINS])
def test_trip_promise_twins(topic, fires, quiet):
    assert fp.trip_promise_kinds(fires), topic
    assert fp.trip_promise_kinds(quiet) == (), topic
    held = fp.apply_policy(_f9(_claim(fires)), mode=fp.INTERIM_MODE)
    warned = fp.apply_policy(_f9(_claim(quiet)), mode=fp.INTERIM_MODE)
    assert held["blocking"] is True and held["violations"] == [_claim(fires)]
    assert warned["blocking"] is False and warned["violations"] == [_claim(quiet)]  # the admin still sees it
    assert warned["f9_policy"]["classes"] == {fp.UNSUPPORTED_OTHER: 1}


def test_a_question_never_promises():
    assert fp.trip_promise_kinds("Is lunch included on the boat ride?") == ()


def test_a_known_place_name_is_not_a_trip_word():
    from services.acp_content_writing.hygiene import build_scope_lexicon
    lexicon = build_scope_lexicon("Location: Sunset Point, Lake Region\nSource itinerary: a walk to Sunset Point.")
    assert fp.trip_promise_kinds("Sunset Point overlooks the lake.") == ()
    assert fp.trip_promise_kinds("You will walk to Sunset Point.", lexicon) == ("activity",)


@pytest.mark.parametrize("violation,cls", [
    ("audit flagged: CTA_NOT_REFLECTED", fp.CTA_NOT_REFLECTED),
    ("TEXT_INTEGRITY: 'The the road ends.'", fp.TEXT_INTEGRITY),
    ('title unsupported claim: "Best tour" (claims: best; source: silent)', fp.SEO_FIELD_CLAIM),
    ('audit flagged: FACT_CHECK_MANUAL_CHECK — exact flagged phrase(s): "a phrase"', fp.FACT_CHECK_UNQUOTED),
    ("stored verdict", fp.OTHER),
])
def test_every_other_f9_class_warns(violation, cls):
    out = fp.apply_policy(_f9(violation), mode=fp.INTERIM_MODE)
    assert out["blocking"] is False and out["f9_policy"]["classes"] == {cls: 1}


def test_mixed_entry_keeps_only_the_promise_for_the_repair():
    """A CTA + claims violation took the full rewrite on base; now the repair gets the promise sentence only and the
    CTA part and the background claim are recorded as warned."""
    promise, background = "You will ride a camel at dawn.", "The dunes formed long ago."
    v = ('audit flagged: CTA_NOT_REFLECTED, FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
         f'"{promise}" (claims: camel ride; source: silent); "{background}" (claims: dune age; source: silent)')
    out = fp.apply_policy(_f9(v, "TEXT_INTEGRITY: 'Broken bit.'"), mode=fp.INTERIM_MODE)
    assert out["blocking"] is True
    assert out["violations"] == [_claim(promise, "camel ride", "silent", status="flagged")]
    assert out["f9_policy"]["classes"] == {fp.CTA_NOT_REFLECTED: 1, fp.TRIP_PROMISE: 1, fp.UNSUPPORTED_OTHER: 1,
                                           fp.TEXT_INTEGRITY: 1}
    assert any(background in w for w in out["f9_policy"]["warned"])
    assert "TEXT_INTEGRITY: 'Broken bit.'" in out["f9_policy"]["warned"]
    # the sentence repair can now parse it (no CTA in the violation): one item, the promise
    text = f"Opening line. {promise} {background} End."
    items = sentence_repair.plan(text, [out])
    assert [i["sentence"] for i in items] == [promise]


def test_claim_with_a_blank_why_never_holds():
    """The judge keeps only claims with a why_not_in_source; a stored claim whose source part is blank warns."""
    out = fp.apply_policy(_f9(_claim("You will ride a camel at dawn.", why=" ")), mode=fp.INTERIM_MODE)
    assert out["blocking"] is False and out["f9_policy"]["classes"] == {fp.UNSUPPORTED_OTHER: 1}


def test_non_blocking_entry_stays_non_blocking():
    title_only = _f9('meta unsupported claim: "You will ride a camel at dawn." (claims: c; source: s)', blocking=False)
    assert fp.apply_policy(title_only, mode=fp.INTERIM_MODE)["blocking"] is False


def test_legacy_mode_and_idempotence():
    entry = _f9(_claim("The old fort was built by a river king."))
    assert fp.apply_policy(entry, mode=fp.LEGACY_MODE) is entry
    once = fp.apply_policy(entry, mode=fp.INTERIM_MODE)
    assert fp.apply_policy(once, mode=fp.INTERIM_MODE) is once
    back = fp.apply_policy(once, mode=fp.LEGACY_MODE)
    assert back["blocking"] is True and back["violations"] == entry["violations"] and "f9_policy" not in back
    mixed = fp.apply_policy(_f9(_claim("You will ride a camel at dawn."), "TEXT_INTEGRITY: 'x y.'"),
                            mode=fp.INTERIM_MODE)
    assert fp.apply_policy(mixed, mode=fp.LEGACY_MODE)["violations"][-1] == "TEXT_INTEGRITY: 'x y.'"


def test_env_switch(monkeypatch):
    assert fp.policy_mode() == fp.INTERIM_MODE
    monkeypatch.setenv(fp.F9_POLICY_MODE_ENV, "legacy")
    assert fp.policy_mode() == fp.LEGACY_MODE
    monkeypatch.setenv(fp.F9_POLICY_MODE_ENV, "nonsense")
    assert fp.policy_mode() == fp.INTERIM_MODE


def test_constants_agree_with_quality_gates_and_sentence_repair():
    assert fp.NO_REASON_MARK == qg.F9_NO_REASON
    assert sentence_repair._F9_CLAIM_RE is fp.F9_CLAIM_RE
    assert fp.TEXT_INTEGRITY_PREFIX == sentence_repair.TEXT_INTEGRITY_PREFIX


# ---------------------------------------------------------------- through the gate stack (judge mocked, $0)

PIECE = ("The old fort stands above the river and has guarded the valley for centuries. "
         "You will ride a camel at dawn to the dunes. Ask us about the dates.")


def _verdict(*claims, codes=("FACT_CHECK_MANUAL_CHECK",)):
    return {"text": json.dumps({
        "status": "manual_check", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
        "failure_codes": list(codes), "flagged_phrases": [], "broken_sentences": [], "notes": "",
        "unsupported_claims": [{"sentence": s, "claim": c, "why_not_in_source": "the source is silent"}
                               for s, c in claims]})}


def _gates(raw):
    with patch.object(qg, "invoke_judge", return_value=raw), patch.object(qg, "_log_t10_judge_call"):
        out = qg.run_quality_gates(content_text=PIECE, atom_text="Location: Old Fort\nSource itinerary: fort visit.",
                                   cta="Ask us about the dates.", brand_rubric_text="r", channel="facebook")
    return out, next(g for g in out["gate_ledger"] if g["gate"] == qr.F9_CTA_FACT)


def test_gate_stack_holds_on_a_trip_promise():
    out, f9 = _gates(_verdict(("You will ride a camel at dawn to the dunes.", "a camel ride at dawn")))
    assert f9["blocking"] is True and not f9["passed"]
    assert out["first_failure"]["gate"] == qr.F9_CTA_FACT


def test_gate_stack_warns_on_place_background():
    out, f9 = _gates(_verdict(("The old fort stands above the river and has guarded the valley for centuries.",
                               "the fort's age")))
    assert f9["blocking"] is False and f9 in out["flags"]
    assert out["first_failure"] is None or out["first_failure"]["gate"] != qr.F9_CTA_FACT


def test_gate_stack_legacy_mode_blocks_on_background(monkeypatch):
    monkeypatch.setenv(fp.F9_POLICY_MODE_ENV, "legacy")
    _, f9 = _gates(_verdict(("The old fort stands above the river and has guarded the valley for centuries.",
                             "the fort's age")))
    assert f9["blocking"] is True


def test_gate_stack_cta_only_warns():
    _, f9 = _gates({"text": json.dumps({"status": "flagged", "brand_fit": "1", "cta_clear": "0", "human_read": "1",
                                        "failure_codes": ["CTA_NOT_REFLECTED"], "flagged_phrases": [],
                                        "broken_sentences": [], "notes": "", "unsupported_claims": []})})
    assert f9["blocking"] is False and f9["f9_policy"]["classes"] == {fp.CTA_NOT_REFLECTED: 1}
