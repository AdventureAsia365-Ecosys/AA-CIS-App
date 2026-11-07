"""AA-1047 (root ADR 0020, W1) — the contradiction suite.

Every example of services/quality/writing_policy.yaml (`examples`: synthetic fire / no-fire twins per topic) runs
through every fixer, gate and rule that reads its field (services/quality/policy_harness.py). The system's verdict is
the most severe detector verdict; it must be what the policy expects for 100% of the examples, each detector named in
the example's `all` must agree on its own, and every fixer's output must satisfy the policy.

The S271 audit listed 16 T9 contradictions (root docs/audits/2026-11-06-S271-writing-quality-audit, data/
blog-pipeline-audit.json plan.contradictions). CODE_VS_CODE maps each code-vs-code one to the examples that prove it
is gone; 2, 3 and 16 are rubric / fingerprint contradictions (tests below); the writer-vs-code ones (7, 11) and the
ones owned by other lanes or waves (13, 14, 15) are listed in the implementation notes AA-1047-policy-registry.md.
"""
from __future__ import annotations

from collections import Counter

import pytest

from services.quality import policy_harness as ph
from services.quality import writing_policy as wp

SOURCE = wp.load_source()
EXAMPLES = SOURCE["examples"]
SEEDS = SOURCE["example_seeds"]

# S271 T9 contradiction number -> the examples that show the code now agrees with itself and the policy.
CODE_VS_CODE = {
    1: ["bg-01", "bg-02", "bg-03", "bg-05", "bg-07", "bg-08", "bg-11", "bg-12", "bg-13", "rar-01", "rar-04", "rar-07",
        "rar-09", "rar-10", "rar-11", "rar-12"],                                        # D1, D13
    4: ["tim-01", "tim-03", "tim-05", "tim-02", "tim-04"],                              # timing: repair vs F17
    5: ["tim-01", "faq-03", "faq-05", "scp-01"],                                        # severity of f7 / s5 / scope
    6: ["faq-02", "faq-03", "faq-04", "faq-06"],                                        # FAQ topics: filter vs dropper
    8: ["num-01", "num-02", "num-05", "num-06", "num-07", "num-08", "num-09", "num-10", "num-11", "num-12", "num-13",
        "num-14", "num-15", "num-16", "num-17", "num-18", "num-19", "num-20", "num-21"],  # numbers: one trip frame
    9: ["scp-01", "scp-02", "scp-03", "scp-05", "scp-07"],                              # scope
    10: ["cta-01", "cta-02", "cta-03", "cta-04", "cta-05", "cta-09"],                   # CTA literal line
    12: ["meta-01", "meta-04", "meta-05", "meta-06", "meta-07", "meta-08", "meta-09", "meta-10", "meta-11", "meta-12",
         "tit-03", "tit-04",
         "slug-01", "slug-03"],                                                     # SEO bands, slug boundary
}


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
    By design not an owner: apply_rules for a time of day (F17 owns it), for a rarity claim about a place (D1
    background passes it, F18 warns), and check_replacement for scope (an admin's sentence gets F12 as a warning until
    the replay shows no false positives). S272 review 2: a number inside the one trip frame
    (claim_rules.trip_number_frame) is owned by F1, apply_rules AND check_replacement; check_replacement also owns the
    sentence-level F17 / F18 findings (it runs them), so every timing and rarity fire twin lists it."""
    missing = [e["id"] for e in BODY if e["expect"] != "pass" and not e.get("all")]
    assert missing == []
    unknown = {d for e in BODY for d in e.get("all") or []} - BODY_DETECTORS
    assert unknown == set()


def test_check_replacement_warns_where_the_pipeline_warns():
    """S272 review: F1 warns on a background number (F6) but the admin sentence check refused it."""
    from services.acp_content_writing import sentence_repair as sr
    seed = ph._seed(SEEDS["monastery"])
    checks = sr.check_replacement("Corran Monastery was founded in 1937 by monks from the coast.",
                                  ph.grounding_text(seed), {"brand_name": ph.BRAND})
    assert [(c["gate"], c["blocking"]) for c in checks] == [("F1_grounding", False)]
    held = sr.check_replacement("Your group walks 30 km to the monastery.", ph.grounding_text(seed),
                                {"brand_name": ph.BRAND})
    assert [(c["gate"], c["blocking"]) for c in held] == [("F1_grounding", True)]


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
    invented = {"Corran Monastery", "Orvine", "Lake Orvine", "Velmora", "Brecca", "Tesselmoor", "Northpine Travel"}
    for seed in SEEDS.values():
        assert seed["place"] in invented and seed["country"] in invented


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


def test_timing_and_rarity_fire_twins_list_the_admin_check():
    """S272 review 2: check_replacement ran neither F17 nor F18, so the admin path saved 'You cross Lake Orvine at
    dawn.' and 'Most travellers never find their way to Brecca.'; every blocking F17 / F18 twin now lists it as an
    owner."""
    owned = [e["id"] for e in BODY if e["expect"] == "block"
             and {"F17_trip_fact", "F18_rarity"} & set(e.get("all") or [])
             and "check_replacement" not in (e.get("all") or [])]
    assert owned == []
