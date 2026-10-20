"""AA-800 — tenant signature vocabulary: the forbidden list split into three layers.

(1) platform truth: blocked for every tenant; (2) AA style: A1 unchanged, for T2/T9 stock superlatives
are capped at one per piece and positioning words are allowed when the tenant declares them; (3) the
tenant's own list: blocked. Plus the evidence rule: a service-level claim ("a luxury lodge") needs the
source. Mock-only: no AWS, no DB, no LLM.
"""
import json

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing.trip_facts import TripFactContext, find_trip_fact_issues
from services.acp_produce import tenant_pipeline as tp
from services.acp_shared import signature_vocabulary as sv
from services.acp_shared.brand_pack import pack_from_row
from services.content_generation import batch_prompt, prompts
from services.content_generation.forbidden_words import VALIDATE_FORBIDDEN, all_forbidden, state_forbidden
from services.content_generation.graph import _apply_forbidden_strip, validate_node

# Atlas & Hearth's live T0 profile (S234 fixtures): brand_type declares "luxury", nothing else.
AH = {"brand_name": "Atlas & Hearth", "brand_type": "Luxury cultural travel brand",
      "core_idea": "Private cultural journeys with depth, beauty, and meaning for discerning travellers",
      "voice": ["Elegant", "Discreet", "Cultured", "Calm", "Precise", "Atmospheric", "Intelligent"],
      "forbidden_words": ["bucket list"]}
TRAIL = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
         "core_idea": "Movement, energy, freedom, and social discovery.",
         "voice": ["Energetic", "Fresh", "Direct"], "forbidden_words": ["conquer"]}

_TOUR = {"name": "Bhutan Highlands", "country": "Bhutan", "duration": "4 Days",
         "summary": "A walk through the Paro valley with dzong visits and a stay in a farmhouse lodge.",
         "itineraries": "Day 1: Arrive in Paro.\nDay 2: Hike to Tiger's Nest.\nDay 3: Punakha Dzong.\n"
                        "Day 4: Depart."}


def _state(pack: dict, generated: dict, *, tenant: bool = True) -> dict:
    """A graph state as _rewrite_tour builds it (brand_* keys) for `pack`."""
    return {"tour": _TOUR, "generated": generated, "retry_count": 0, "is_tenant_rewrite": tenant,
            "brand_forbidden_words": pack.get("forbidden_words") or [], "brand_type": pack.get("brand_type", ""),
            "brand_core_idea": pack.get("core_idea", ""), "brand_voice_examples": pack.get("voice") or [],
            "brand_style_guide": pack.get("style_guide", ""), "brand_good_examples": pack.get("good_examples", ""),
            "brand_signature_words": pack.get("signature_words") or []}


def _gen(summary: str, **over) -> dict:
    g = {"name": "Bhutan: Paro and Punakha in 4 Days", "subtitle": "Paro valley walks and Punakha Dzong",
         "summary": summary, "highlights": ["Tiger's Nest hike", "Punakha Dzong", "Paro valley farmhouses"],
         "itineraries": "Day 1: Arrive in Paro.\nDay 2: Hike to Tiger's Nest.\nDay 3: Punakha Dzong.\nDay 4: Depart.",
         "seo_title": "Bhutan Paro and Punakha tour",
         "seo_meta": ("A four-day Bhutan walk through the Paro valley to Tiger's Nest and on to Punakha Dzong, "
                      "with farmhouse nights and quiet dzong courtyards on the way.")}
    g.update(over)
    return g


def _codes(state: dict) -> list[str]:
    return validate_node(state)["failure_codes"]


# ── the layers themselves ───────────────────────────────────────────────────────────────────────

def test_a1_validate_list_is_the_union_of_the_layers_byte_for_byte():
    assert VALIDATE_FORBIDDEN == [
        "curated", "pristine", "refined", "tailored", "bespoke",
        "stunning", "breathtaking", "magical", "paradise",
        "cheap", "deal", "book now", "instant booking", "discount",
    ]
    layers = set(sv.PLATFORM_TRUTH) | set(sv.STOCK_SUPERLATIVES) | set(sv.POSITIONING)
    assert set(VALIDATE_FORBIDDEN) == layers - sv.PROMPT_ONLY
    assert all_forbidden(["Glamping"]) == VALIDATE_FORBIDDEN + ["glamping"]  # A1 helper unchanged


def test_a1_state_keeps_the_full_list_and_rule_1():
    a1 = _state(AH, {}, tenant=False)
    assert state_forbidden(a1) == all_forbidden(["bucket list"])
    assert prompts._RULE_1_CORE_FORBIDDEN in prompts.SYSTEM_PROMPT
    assert "signature words" not in batch_prompt.build_s1_system_prompt({**a1, "seo": {}, "few_shots": []})


# ── layer 1: platform truth, every tenant ────────────────────────────────────────────────────────

@pytest.mark.parametrize("pack", [AH, TRAIL, {**AH, "signature_words": ["cheap", "luxury"]}, {}])
def test_platform_truth_is_blocked_for_every_tenant(pack):
    words = sv.tenant_blocked_words(pack)
    assert all(w in words for w in sv.PLATFORM_TRUTH)
    assert "FORBIDDEN_WORD" in _codes(_state(pack, _gen("A cheap walk through the Paro valley.")))


def test_t9_platform_truth_gate_blocks_and_exempts_the_source():
    res = qg.gate_platform_truth("Book now for the spring walk. The dzong is quiet.", "Punakha Dzong")
    assert not res["passed"] and res["blocking"] and res["repairable"]
    assert res["violations"] == ["uses 'book now', a price or booking promise the platform cannot back: "
                                 "'Book now for the spring walk.'"]
    # the tenant's own CTA / the seed holding the phrase is not a finding (same carve-out as F2)
    assert qg.gate_platform_truth("Book now for the spring walk.", "Book now for the spring walk")["passed"]


# ── layer 2a: stock superlatives, capped at one for a tenant ─────────────────────────────────────

def test_a1_still_blocks_a_single_superlative():
    assert "FORBIDDEN_WORD" in _codes(_state(TRAIL, _gen("A stunning walk through the Paro valley."), tenant=False))


def test_t2_one_superlative_is_a_warning_two_are_blocked():
    one = _codes(_state(TRAIL, _gen("A stunning walk through the Paro valley.")))
    assert "FORBIDDEN_WORD" not in one and "STOCK_SUPERLATIVE" in one
    two = _state(TRAIL, _gen("A stunning walk through the Paro valley to a magical dzong."))
    assert "FORBIDDEN_WORD" in _codes(two) and "STOCK_SUPERLATIVE" not in _codes(two)
    assert {"stunning", "magical"} <= set(state_forbidden(two, two["generated"]))


def test_t2_strip_removes_superlatives_only_over_the_cap():
    one = _state(TRAIL, _gen("A stunning walk through the Paro valley."))
    _apply_forbidden_strip(one["generated"], one)
    assert "stunning" in one["generated"]["summary"]
    two = _state(TRAIL, _gen("A stunning walk through the Paro valley to a magical dzong."))
    _apply_forbidden_strip(two["generated"], two)
    assert "stunning" not in two["generated"]["summary"] and "magical" not in two["generated"]["summary"]


def test_t9_superlatives_one_warns_two_block_and_never_hold():
    one = qg.gate_signature_words("The stunning dzong sits above the river.", "Punakha Dzong", TRAIL)
    assert one[0]["passed"] and not one[0]["blocking"]
    assert not one[1]["passed"] and not one[1]["blocking"]
    assert "one stock superlative ('stunning')" in one[1]["violations"][0]
    two = qg.gate_signature_words("The stunning dzong sits above the river. The valley is magical at dusk.",
                                  "Punakha Dzong", TRAIL)
    assert not two[0]["passed"] and two[0]["blocking"] and two[0]["repairable"]
    assert len(two[0]["violations"]) == 1 and two[0]["violations"][0].endswith("'The valley is magical at dusk.'")
    assert qg.hold_failure([two[0]]) is None  # style only: one repair, never a hold
    # a superlative in the source is not counted
    src = qg.gate_signature_words("The stunning dzong sits above the river. The valley is magical at dusk.",
                                  "the stunning dzong", TRAIL)
    assert src[0]["passed"]


def test_breathtaking_is_capped_not_banned_on_t9():
    assert qg.gate_banned_patterns("The breathtaking range faces the camp.", "")["passed"]
    assert qg.gate_signature_words("The breathtaking range faces the camp.", "", TRAIL)[0]["passed"]


# ── layer 2b: positioning words, allowed when declared ───────────────────────────────────────────

def test_signature_words_are_read_from_the_tenant_text():
    assert sv.signature_words(AH) == ["luxury"]
    assert sv.signature_words(TRAIL) == []
    assert sv.signature_words({"style_guide": "Refined, curated language. Itineraries feel tailor-made."}) == [
        "curated", "refined", "tailored"]
    assert sv.signature_words({"good_examples": "A bespoke evening in the courtyard."}) == ["bespoke"]


@pytest.mark.parametrize("text", [
    "Do not use bespoke or curated.", "Avoid luxury clichés and tailored talk.", "We value depth over luxury.",
    "Never say refined.", "No luxury language; keep it plain.",
])
def test_a_negated_mention_does_not_declare(text):
    assert sv.signature_words({"style_guide": text}) == []


def test_explicit_signature_words_from_custom_validators():
    assert sv.explicit_signature_words({"signature_words": ["Curated", "cheap", " "]}) == ["curated", "cheap"]
    assert sv.explicit_signature_words([{"kind": "x"}, {"signature_words": ["refined"]}]) == ["refined"]
    assert sv.explicit_signature_words(json.dumps({"signature_words": ["bespoke"]})) == ["bespoke"]
    assert sv.explicit_signature_words("not json") == [] and sv.explicit_signature_words(None) == []
    pack = pack_from_row({"brand_name": "X", "custom_validators": '{"signature_words": ["curated"]}'})
    assert pack["signature_words"] == ["curated"]
    # only the positioning words can be declared: "cheap" stays platform truth
    assert sv.signature_words({"signature_words": ["curated", "cheap"]}) == ["curated"]


def test_t2_declared_positioning_word_passes_validate_and_strip():
    ah = _state(AH, _gen("A luxury cultural journey through the Paro valley."))
    assert "FORBIDDEN_WORD" not in _codes(ah)
    refined = _state(AH, _gen("A refined walk through the Paro valley."))
    assert "FORBIDDEN_WORD" in _codes(refined)                  # undeclared for Atlas & Hearth
    declared = _state({**AH, "signature_words": ["refined"]}, _gen("A refined walk through the Paro valley."))
    assert "FORBIDDEN_WORD" not in _codes(declared)
    _apply_forbidden_strip(declared["generated"], declared)
    assert "refined" in declared["generated"]["summary"]        # a signature word is never stripped


# ── layer 3: the tenant's own list always wins ───────────────────────────────────────────────────

def test_tenant_forbidden_word_is_never_declared():
    pack = {**AH, "forbidden_words": ["luxury"], "signature_words": ["luxury"]}
    assert sv.signature_words(pack) == []
    assert "luxury" in sv.tenant_blocked_words(pack)
    assert "FORBIDDEN_WORD" in _codes(_state(pack, _gen("A luxury cultural journey through the Paro valley.")))
    assert not qg.gate_brand_forbidden_words("A luxury night by the lake.", "", ["luxury"])["passed"]


# ── writer prompts built per tenant ──────────────────────────────────────────────────────────────

def test_t2_rule_1_is_built_from_the_tenant_layers():
    ah = batch_prompt.build_s1_system_prompt({**_state(AH, {}), "seo": {}, "few_shots": []})
    assert ("NEVER use these words: curated, refined, tailored, bespoke, cheap, deal, book now, instant booking, "
            "discount\n") in ah
    assert "This brand's own signature words: luxury. Source fidelity (rule 6) comes first" in ah  # S245 nudge
    trail = batch_prompt.build_s1_system_prompt({**_state(TRAIL, {}), "seo": {}, "few_shots": []})
    assert "NEVER use these words: curated, refined, tailored, bespoke, luxury, cheap" in trail
    assert "signature words" not in trail
    assert prompts.T2_SYSTEM_PROMPT == prompts.build_t2_base_prompt({})


def test_t9_words_line_is_built_from_the_tenant_layers():
    ah = sv.t9_words_line(AH)
    assert "never write cheap, deal, book now, instant booking, discount." in ah
    assert "signature words, yours to use where the content seed fits: luxury." in ah
    assert "Avoid curated, refined, tailored, bespoke:" in ah
    assert "Avoid curated, refined, tailored, bespoke, luxury:" in sv.t9_words_line(TRAIL)


def test_t9_declared_word_is_not_warned_undeclared_is():
    ah = qg.gate_signature_words("A luxury evening in the courtyard.", "", AH)
    assert ah[1]["passed"]
    trail = qg.gate_signature_words("A luxury evening in the courtyard.", "", TRAIL)
    assert not trail[1]["passed"] and not trail[1]["blocking"]


# ── evidence rule: a service-level claim needs the source ────────────────────────────────────────

def test_service_level_claims():
    found = sv.service_level_claims("Nights are spent in a luxury tented camp by the lake.", "a ger camp by the lake")
    assert found == [{"word": "luxury", "phrase": "luxury tented camp"}]
    assert sv.service_level_claims("Nights in a luxury lodge.", "a deluxe lodge on the shore") == []
    assert sv.service_level_claims("A luxury cultural journey for discerning travellers.", "") == []  # brand level
    assert sv.service_level_claims("A tailored itinerary through Paro.", "")[0]["word"] == "tailored"
    assert sv.service_level_claims("A tailored itinerary through Paro.", "fully customised itinerary") == []


def test_t3_grounding_flags_an_unsupported_luxury_claim_for_a_declared_tenant():
    gen = _gen("Nights are spent in a luxury lodge in the Paro valley.")
    found = tp._t3_grounding_check(gen, [str(v) for v in _TOUR.values()], background=False)
    assert found == [{"field": "summary", "sentence": "Nights are spent in a luxury lodge in the Paro valley.",
                      "novel_numbers": [], "unsupported_claims": ["luxury lodge"]}]
    fb = tp.t3_repair_feedback([], found)
    assert "service claim luxury lodge" in fb and "must also be in the source" in fb
    assert tp._t3_unsupported("Nights are spent in a luxury lodge.", [str(v) for v in _TOUR.values()],
                              background=False)
    # the word itself is fine for Atlas & Hearth (validate passes); only the claim is a fidelity finding
    assert tp._t3_structural_issues(gen, _TOUR, {"forbidden_words": [], "brand_type": AH["brand_type"]}) == []


def test_t3_structural_reads_the_tenant_layers():
    gen = _gen("A refined walk through the Paro valley.")
    assert tp._t3_structural_issues(gen, _TOUR, {"brand_type": AH["brand_type"]}) == ["FORBIDDEN_WORD"]
    assert tp._t3_structural_issues(gen, _TOUR, {"brand_type": AH["brand_type"], "signature_words": ["refined"]}) == []


def test_t9_f17_flags_an_unsupported_luxury_claim():
    ctx = TripFactContext(seed="Sod Camp — overnight\nSource itinerary: Overnight at a ger camp by the lake.")
    found = find_trip_fact_issues("Your night is in a luxury ger camp by the lake.", ctx)["trip_fact"]
    assert found == ["states a service level the source does not ('luxury ger camp') — keep the fact, cut the "
                     "claim word: 'Your night is in a luxury ger camp by the lake.'"]
    ok = TripFactContext(seed="Sod Camp — overnight\nSource itinerary: Overnight at a luxury ger camp by the lake.")
    assert find_trip_fact_issues("Your night is in a luxury ger camp by the lake.", ok)["trip_fact"] == []


# ── admin T2 drawer agrees with T3 ───────────────────────────────────────────────────────────────

def test_admin_t2_flag_uses_the_layered_list():
    from api.routers import admin_content_quality as cq
    rc = {"summary": "A luxury cultural journey. A stunning walk to the dzong.", "highlights": ["A refined dinner"]}
    flagged = cq.t2_flag(cq.t2_sections(rc), _TOUR, AH)
    by = {(f["field"], f["index"]): [p["problem"] for p in f["problems"]] for f in flagged}
    # luxury is Atlas & Hearth's own, one superlative is allowed; "refined dinner" is undeclared and a claim
    assert list(by) == [("highlights", 0)]
    assert any("forbidden word(s): refined" in p for p in by[("highlights", 0)])
    assert any("service claim(s) ['refined dinner']" in p for p in by[("highlights", 0)])
    # the pre-AA-800 call shape (a word list) still works
    assert cq.t2_flag(cq.t2_sections({"summary": "A cheap walk."}), _TOUR, ["x"])
