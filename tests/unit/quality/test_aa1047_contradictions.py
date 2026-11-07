"""AA-1047 (root ADR 0020, W1) — the contradiction suite.

Every example of services/quality/writing_policy.yaml (`examples`: synthetic fire / no-fire twins per topic) runs
through every fixer, gate and rule that reads its field (services/quality/policy_harness.py). The system's verdict is
the most severe detector verdict; it must be what the policy expects for 100% of the examples, each detector named in
the example's `all` must agree on its own, and every fixer's output must satisfy the policy.

The S271 audit listed 16 T9 contradictions (root docs/audits/2026-11-06-S271-writing-quality-audit, data/
blog-pipeline-audit.json plan.contradictions). CODE_VS_CODE maps each code-vs-code one to the examples that prove it
is gone; 2, 3 and 16 are rubric / fingerprint contradictions (tests below); the writer-vs-code ones (7, 11) and the
ones owned by other lanes or waves (13, 14, 15) are listed in the implementation notes AA-1047-policy-registry.md.

S272 (fail closed until W2): 1 (D1 / D13 background and rarity) and 8 (numbers: one trip frame) are NOT closed here.
The hand-written warn rules that closed them kept failing open on new inputs over three review rounds, so the gates
are main's again and the policy marks those topics `enforcement: block_until_w2_instrument`; the examples assert
main's (fail-closed) verdicts and the decided intent waits in tests/unit/quality/fixtures/
aa1047_w2_intent_examples.yaml (`w2_intent`, plus `main_gaps`: where main is laxer than the policy, and the one CTA
close ensure_cta leaves for check_cta to fail), kept for W2 and not asserted.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from services.quality import policy_harness as ph
from services.quality import writing_policy as wp

SOURCE = wp.load_source()
EXAMPLES = SOURCE["examples"]
SEEDS = SOURCE["example_seeds"]

# S271 T9 contradiction number -> the examples that show the code now agrees with itself and the policy.
CODE_VS_CODE = {
    4: ["tim-01", "tim-03", "tim-05", "tim-02", "tim-04"],                              # timing: repair vs F17
    5: ["tim-01", "faq-03", "faq-05", "scp-01"],                                        # severity of f7 / s5 / scope
    6: ["faq-02", "faq-03", "faq-04", "faq-06"],                                        # FAQ topics: filter vs dropper
    9: ["scp-01", "scp-02", "scp-03", "scp-05", "scp-07"],                              # scope
    10: ["cta-01", "cta-02", "cta-03", "cta-04", "cta-05"],                             # CTA literal line (cta-09: W2)
    12: ["meta-01", "meta-04", "meta-05", "meta-06", "meta-07", "meta-08", "meta-09", "meta-10", "meta-11", "meta-12",
         "meta-13", "meta-14", "meta-15", "meta-16",
         "tit-03", "tit-04",
         "slug-01", "slug-03"],                                                     # SEO bands, slug boundary
}
W2_FIXTURE = Path(__file__).parent / "fixtures" / "aa1047_w2_intent_examples.yaml"


def _outcome(ex: dict) -> ph.Outcome:
    return ph.run_example(ex, SEEDS)


@pytest.mark.parametrize("ex", EXAMPLES, ids=[e["id"] for e in EXAMPLES])
def test_example_is_decided_as_the_policy_expects(ex):
    o = _outcome(ex)
    assert o.combined == ex["expect"], (o.verdicts, o.notes)
    for detector in ex.get("all") or []:
        assert o.verdicts.get(detector) == ex["expect"], (detector, o.verdicts, o.notes)
    assert all(o.fixed.values()), o.fixed


BODY = [e for e in EXAMPLES if e.get("field", "body") == "body"]
BODY_DETECTORS = {"apply_rules", "F1_grounding", "F17_trip_fact", "F18_rarity", "F12_scope", "check_replacement"}


def test_every_body_example_gives_apply_rules_a_claim_row():
    """Without a row apply_rules reads every sentence as 'generic' and is never really exercised (review S272)."""
    assert [e["id"] for e in BODY if not e.get("claim")] == []


def test_every_body_fire_twin_names_its_owning_detectors():
    """`combined` takes the most severe verdict, so a lax detector hides behind a strict one; each detector that owns
    the example's rule must reach `expect` on its own (`all`, checked in test_example_is_decided_as_the_policy_expects).
    By design not an owner: apply_rules for a time of day or a rarity claim (F17 / F18 own them; the rule table reads
    only its social-proof lexicon), apply_rules for a digit (F1 owns digits, the rule table spelled-out figures), and
    check_replacement for a time, a rarity or scope (the admin sentence check runs F1 and the lexicon checks, and gives
    F12 as a warning, as on main). S272: the review-2..4 ownership (one trip frame for F1, the rule table and the admin
    check; F18's lexicon in the rule table) left with the code; the examples name the owners main has."""
    missing = [e["id"] for e in BODY if e["expect"] != "pass" and not e.get("all")]
    assert missing == []
    unknown = {d for e in BODY for d in e.get("all") or []} - BODY_DETECTORS
    assert unknown == set()


def test_check_replacement_blocks_every_f1_finding_as_on_main():
    """S272: F1 holds every number the SOURCE lacks (no background number warns until W2), and the admin sentence check
    refuses what F1 holds (each finding blocking, as on main)."""
    from services.acp_content_writing import sentence_repair as sr
    seed = ph._seed(SEEDS["monastery"])
    for text in ("Corran Monastery was founded in 1937 by monks from the coast.",
                 "Your group walks 30 km to the monastery."):
        checks = sr.check_replacement(text, ph.grounding_text(seed), {"brand_name": ph.BRAND})
        assert [(c["gate"], c["blocking"]) for c in checks] == [("F1_grounding", True)], text


def test_every_topic_has_fire_and_no_fire_twins():
    by_topic: dict[str, Counter] = {}
    for ex in EXAMPLES:
        by_topic.setdefault(ex["topic"], Counter())[ex["expect"] == "pass"] += 1
    required = {"background", "timing", "rarity", "cta", "faq", "numbers", "scope", "meta_band", "slug_boundary",
                "meals", "day_count"}
    assert required <= set(by_topic)
    for topic, counts in by_topic.items():
        assert counts[True] >= 1 and counts[False] >= 1, f"{topic} needs a fire and a no-fire example"


def test_the_code_vs_code_contradictions_are_proved_gone():
    ids = {e["id"]: e for e in EXAMPLES}
    for number, names in CODE_VS_CODE.items():
        for name in names:
            assert name in ids, (number, name)
            o = _outcome(ids[name])
            assert o.ok, (number, name, o.verdicts, o.notes)


def test_examples_are_synthetic():
    """Public repo: invented places only (CI golden-set text guard). The seeds name only the invented world."""
    invented = {"Corran Monastery", "Orvine", "Lake Orvine", "Velmora", "Brecca", "Tesselmoor", "Northpine Travel",
                "Dunmere Harbour"}
    for seed in SEEDS.values():
        for key in ("place", "country"):
            assert seed.get(key) is None or seed[key] in invented, seed
        card = seed.get("card") or {}
        assert not card or (card["display_name"] in invented and card["country"] in invented)


def test_policy_sha_ignores_the_fixtures():
    changed = {**SOURCE, "examples": EXAMPLES[:1]}
    assert wp.sha_of(changed) == wp.sha_of(SOURCE) == wp.policy_sha()


def test_contradiction_2_the_brief_is_never_a_source():
    """S271 T9 #2: the eval judge and the golden rubric counted the brief and brand pack as source (D2)."""
    from services.writing_memory import eval_judge as ej
    rules = " ".join(wp.label_rubric()["rules"])
    assert "instructions, never a source" in rules
    assert "instructions, never a source" in ej.system_prompt()
    assert "brief and brand pack are part of the source" not in ej.system_prompt()


def test_contradiction_3_reflection_is_voice_in_the_rubric_and_the_taxonomy():
    """S271 T9 #3: 'reflection is voice' sat next to fact.embellish 'editorial claim' (D3)."""
    from services.quality import registry as qr
    from services.writing_memory import eval_judge as ej
    assert any(r.startswith("Reflection that states no fact") for r in wp.label_rubric()["rules"])
    embellish = next(c for c in qr.taxonomy()["classes"] if c["id"] == "fact.embellish")
    assert "Reflection that states no fact about the trip is voice" in " ".join(embellish["definition"].split())
    assert "Reflection that states no fact about the trip is voice" in ej.system_prompt()


def test_contradiction_16_a_policy_change_moves_the_release_fingerprint():
    """S271 T9 #16 (in part): a rule change outside a prompt symbol never moved the fingerprint; the policy is now a
    component of it (the route-control and catalog parts stay with the change-control work)."""
    from services.writing_memory import config_fp
    assert "policy" in config_fp.COMPONENTS


def test_held_topics_record_their_w2_intent():
    """S272: a claim topic held at main's verdicts says so (`enforcement`) and keeps its decided intent
    (`w2_intent`)."""
    held = {t: c for t, c in SOURCE["claims"].items() if isinstance(c, dict) and "enforcement" in c}
    assert set(held) == {"background", "rarity", "numbers"}
    for topic, spec in held.items():
        assert spec["enforcement"] == "block_until_w2_instrument", topic
        assert spec["w2_intent"] and set(spec["w2_intent"]) < set(spec), topic
    assert SOURCE["claims"]["numbers"]["elsewhere"] == "enforce"
    assert SOURCE["claims"]["rarity"]["place"] == "enforce"
    assert SOURCE["claims"]["numbers"]["w2_intent"]["elsewhere"] == "warn"
    assert SOURCE["claims"]["rarity"]["w2_intent"]["place"] == "warn"


def test_w2_fixture_is_kept_apart_from_the_asserted_suite():
    """The W2 intent sentences parse, use the policy's seeds, and are never asserted here (their ids in `main_gaps`
    are not policy examples; `w2_intent` ids are, at main's verdict)."""
    data = yaml.safe_load(W2_FIXTURE.read_text(encoding="utf-8"))
    ids = {e["id"] for e in EXAMPLES}
    assert data["w2_intent"] and data["main_gaps"]
    for ex in data["w2_intent"]:
        assert ex["id"] in ids and ex.get("seed", "monastery") in SEEDS, ex["id"]
        assert ex["main"] == next(e["expect"] for e in EXAMPLES if e["id"] == ex["id"]), ex["id"]
    for ex in data["main_gaps"]:
        assert ex["id"] not in ids and ex.get("seed", "monastery") in SEEDS, ex["id"]
    # the withdrawn warn intents are recorded, not lost
    warn_intent = {e["id"] for e in data["w2_intent"] if e["expect"] == "warn"}
    assert {"bg-02", "bg-06", "rar-01", "rar-07", "num-05", "num-09", "num-38"} <= warn_intent
