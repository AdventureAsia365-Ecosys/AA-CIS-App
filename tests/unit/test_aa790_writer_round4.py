"""AA-790 round 4 (S252) — prompt-side only. The 4-arm blind panel of round 3 (AI-tells 5.22, bar 5.5; Terra Family
brand voice 6.85) quoted shapes the writer itself still produced: staccato denials, 'not as … but as …', verdict and
aphorism lines, template closers, lists of three, label lines before the CTA, and family brands with the family
missing. No new deletion in the post-pass: the writer is told, with DON'T / WRITE INSTEAD pairs, a silent self-check,
and a FAMILY BRAND line for family packs (seed facts only).
"""
import re
import time

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import GOALS
from services.acp_content_writing import ai_tells, hygiene, prompts, quality_gates, service
from services.acp_content_writing.ai_tells import (FAMILY_RULE, PROMPT_RULES, SELF_CHECK_RULE, brand_voice_rule,
                                                   family_presence_rule)

GOAL = GOALS[0]
ANGLE = {"name": "The Craft School", "why_it_works": "w", "formula_fit": "f", "best_final_style": "s"}
TERRA = {"brand_name": "Terra Family Expeditions", "brand_type": "Premium family adventure travel brand",
         "voice": ["Warm", "Reassuring", "Clear", "Practical", "Family-aware", "Encouraging", "Trustworthy"]}
TRAIL = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
         "voice": ["Energetic", "Fresh", "Direct", "Friendly", "Modern", "Confident", "Lightly playful"]}
ATLAS = {"brand_name": "Atlas & Hearth", "brand_type": "Luxury cultural travel brand",
         "voice": ["Elegant", "Discreet", "Cultured", "Calm", "Precise", "Atmospheric", "Intelligent"]}
WILD = {"brand_name": "WildKind Travel", "brand_type": "Responsible nature and conservation travel brand",
        "voice": ["Thoughtful", "Grounded", "Respectful", "Nature-led", "Ethical", "Calm", "Informative"]}


def _flat(s: str) -> str:
    return " ".join(s.split())


def _prompt(channel: str, pack: dict, **kw) -> str:
    return prompts.build_user_prompt(content_seed="Zorig Chusum\nSource itinerary: A visit.", goal=GOAL,
                                     channel_style=get_channel_style(channel), brand_audience=pack, angle=ANGLE,
                                     cta="Message us.", **kw)


# ---------------------------------------------------------------- the AI-TELLS brief

def test_brief_names_the_round3_panel_frames():
    rules = _flat(PROMPT_RULES)
    for frame in ('"not as A but as B"', '"A rather than B"', '"That is the shape of ..."', '"X earns its place."',
                  '"That fact matters."', '"... is the point."', '"... is one that travels home with you"',
                  "No lists of three for rhythm", '"This is one moment of the ..."'):
        assert frame in rules, frame
    # a practical denial from the seed is still a fact, not a set-up
    assert 'A denial stays only when the denial itself is a practical fact from the content seed' in rules


def _pairs() -> list[tuple[str, str]]:
    block = PROMPT_RULES.split("DON'T / WRITE INSTEAD")[1]
    items = [_flat(i) for i in block.split("\n    - ")[1:]]
    return [(i.split(" WRITE", 1)[0], i.split(" WRITE", 1)[1]) for i in items]


def test_brief_has_six_to_ten_dont_write_pairs():
    pairs = _pairs()
    assert 6 <= len(pairs) <= 10
    for dont, write in pairs:
        assert dont.startswith("DON'T \"") and write, dont


def test_every_write_instead_takes_its_facts_from_the_seed():
    """A positive rewrite never carries a fact of its own: a <slot> filled from the content seed, or 'nothing' (the
    line goes), or the call to action alone."""
    for _dont, write in _pairs():
        assert "<" in write or write.lstrip(" :").startswith("nothing") or "call to action" in write, write


def test_dont_examples_are_short_paraphrased_phrases():
    """Short shapes, never a tenant's paragraph: every quoted DON'T example is at most 16 words."""
    for dont, _ in _pairs():
        for quote in re.findall(r'"([^"]+)"', dont):
            assert len(quote.split()) <= 16, quote


def test_self_check_is_the_last_rule_before_the_output_line():
    system = prompts.SYSTEM_PROMPT
    assert PROMPT_RULES in system and SELF_CHECK_RULE in system
    assert system.index(ai_tells.VISIT_DETAIL_RULE) < system.index(SELF_CHECK_RULE)
    after = system[system.index(SELF_CHECK_RULE) + len(SELF_CHECK_RULE):]
    assert after.lstrip().startswith("- Return ONLY the final content itself")
    check = _flat(SELF_CHECK_RULE)
    assert 'Make sure no sentence has the form "X is not A. It is B."' in check
    assert "This check is silent: never mention it in the piece." in check


def test_new_prompt_text_keeps_the_prompt_contracts():
    em = "—"
    for text in (PROMPT_RULES, SELF_CHECK_RULE, FAMILY_RULE):
        assert em not in text
        assert "premium" not in text.lower() and not re.search(r"\bAA\b", text)
    outside = prompts.SYSTEM_PROMPT.replace(PROMPT_RULES, "")
    assert "genuinely" not in outside and "precisely" not in outside
    assert "{" not in SELF_CHECK_RULE and "{" not in FAMILY_RULE  # appended raw, never .format()ed


def test_writer_prompt_version_moves_off_round_three():
    # round 3: 24363541; round 4 before review: 0463a554; round 4 shipped 2b510e08, round 5 pins its own:
    # test_aa790_writer_round5.test_writer_prompt_version_is_round_five
    assert service.WRITER_PROMPT_VERSION not in ("24363541", "0463a554")


# ---------------------------------------------------------------- FAMILY BRAND

@pytest.mark.parametrize("pack", [
    TERRA,
    {"voice": ["Warm", "Family-aware"]},
    {"brand_type": "Premium family adventure travel brand"},
    {"brand_type": "Adventure trips for families with children"},
    {"voice": ["Kid-friendly"], "brand_type": "Holidays for parents and kids"},
    {"voice": ["Kid-friendly"]},
    {"voice": ["Child-friendly", "Warm"]},
    {"voice": ["Family-friendly"]},
    # review: a negation never reaches past a comma, and 'by' is ownership only before a family word
    {"voice": ["Calm, never preachy, family-focused"]},
    {"brand_type": "Designed by parents, for parents"},
])
def test_family_rule_for_a_family_brand(pack):
    assert family_presence_rule(pack) == FAMILY_RULE


@pytest.mark.parametrize("pack", [
    TRAIL, ATLAS, WILD, {}, None,
    {"customer_segment": "Couples and families with children aged 7-16"},  # the audience alone never triggers it
    {"brand_type": "Not a family brand", "voice": ["Calm"]},
    {"voice": ["Never childish"]},
    {"core_idea": "Family time in nature."},
    # ownership, not audience: who runs the business never makes it a family brand
    {"voice": ["Family-run", "Calm"]},
    {"brand_type": "Boutique tours, family owned since 1990"},
    {"voice": ["Family-owned", "Family-operated", "Family-led", "Family-founded"]},
    {"brand_type": "A family business since 1990"},
    {"brand_type": "Small group tours, family run"},
    # review: a negation up to four words back, and 'by / same family / for N generations' ownership
    {"brand_type": "Adults-only luxury brand, not suitable for children"},
    {"brand_type": "Run by the same family for three generations"},
    {"brand_type": "Adults only, no kids"},
    {"brand_type": "Luxury retreats without children"},
    {"brand_type": "Boutique lodges owned by a local family"},
    {"brand_type": "Small group tours, run by one family for 30 years"},
])
def test_no_family_rule_otherwise(pack):
    assert family_presence_rule(pack) == ""


def test_family_rule_keeps_source_fidelity_first():
    rule = _flat(FAMILY_RULE)
    assert "SOURCE FIDELITY comes first: a family detail the seed does not give is left out, never guessed." in rule
    assert ('An age, an age band or "suitable for children" appears ONLY when the CONTENT SEED or FACTS state it.'
            in rule)
    assert "never this trip" in rule
    assert "Never invent what children feel, notice, learn, understand or remember" in rule
    assert '"works for every age"' in rule and "school-trip framing" in rule
    assert "without a made-up family line" in rule


def test_family_rule_bounds_who_does_what_by_the_seed():
    """Review fix: the writer never gives the children or the parents an activity the seed does not give the group
    (no per-fact attribution). Round 5 lets them do what the seed says the group does and drops the one-sentence
    cap (test_aa790_writer_round5)."""
    rule = _flat(FAMILY_RULE)
    assert "Never give them a moment, a view or an activity the seed does not give the group." in rule
    assert "With each fact" not in rule


def test_self_check_quotes_the_full_more_than_set_up_and_keeps_seed_quantities():
    check = _flat(SELF_CHECK_RULE)
    assert '"more than A"' not in check
    assert '"X is more than A. It is B." or "more than just A"' in check
    assert 'a quantity from the seed, such as "more than 300 monks", stays word for word' in check


def test_cta_pairs_do_not_contradict_the_worked_in_cta_rule():
    """Review fix: SYSTEM_PROMPT says the CTA is worked into the piece, not pasted on as a bare final sentence; the
    DON'T / WRITE pairs ban only the label line and never ask for the CTA 'alone' or 'then the call to action'."""
    assert "worked naturally into the piece, not pasted on as a bare" in _flat(prompts.SYSTEM_PROMPT)
    writes = [w for _, w in _pairs() if "call to action" in w]
    assert len(writes) == 2
    for w in writes:
        assert "worked into" in w and "alone" not in w and "then the call to action" not in w, w


@pytest.mark.parametrize("channel", ["linkedin", "blog", "facebook"])
def test_family_rule_reaches_every_channel_of_a_family_brand_only(channel):
    kw = {"atom_id": "a1"} if channel == "blog" else {}
    assert FAMILY_RULE in _prompt(channel, TERRA, **kw)
    for pack in (TRAIL, ATLAS, WILD):
        assert "FAMILY BRAND" not in _prompt(channel, pack, **kw)


def test_family_rule_comes_after_the_energy_line_and_before_the_blog_format():
    p = _prompt("blog", {**TERRA, "voice": [*TERRA["voice"], "Calm"]}, atom_id="a1")
    assert p.index(brand_voice_rule({"voice": ["Calm"]})) < p.index(FAMILY_RULE) < p.index("BLOG-SPECIFIC FORMAT")


def test_energy_lines_are_unchanged_and_family_is_separate():
    """Round 4 must not move PACE / RESTRAINT, and brand_voice_rule stays '' for a family pack with neither."""
    assert brand_voice_rule(TERRA) == ""
    assert "PACE FOR THIS BRAND (its voice: Energetic, Lightly playful)" in brand_voice_rule(TRAIL)
    assert "RESTRAINT FOR THIS BRAND (its voice: Thoughtful, Grounded, Respectful, Calm)" in brand_voice_rule(WILD)


# ---------------------------------------------------------------- placeholder slots (review fix)

_SLOT_RX = hygiene.AI_TELL_LINE_RES[-1]


def test_every_prompt_slot_is_caught():
    """Review fix: each <slot> in the DON'T / WRITE pairs, copied into a piece, is residue."""
    slots = re.findall(r"<[^<>]+>", _flat(PROMPT_RULES))
    assert len(slots) >= 10
    for slot in slots:
        assert _SLOT_RX.search(f"Lunch is at {slot} by the river."), slot


@pytest.mark.parametrize("sentence, slot", [
    ("After the long ride north, lunch is at <place> by the river.", "<place>"),
    ("<Day or place> starts with a climb to the monastery gate.", "<Day or place>"),
    ("The abbot usually <does what the content seed says> before the morning prayers.",
     "<does what the content seed says>"),
    ("The weavers train for <seed slot> in the village workshop.", "<seed slot>"),
    ("The night is spent at <night_stop> above the lake shore.", "<night_stop>"),
    ("This is one moment of the <trip> along the northern loop.", "<trip>"),
])
def test_a_leftover_slot_is_residue_and_goes_to_the_f15_repair(sentence, slot):
    text = f"## Day 2\n\nThe road climbs north from the town.\n\n{sentence}\n"
    assert (slot, sentence) in ai_tells.residue(text)
    voice = quality_gates.gate_voice(text, spec=None)
    assert any(f"stock line '{slot}'" in v and sentence in v for v in voice["violations"]), voice
    # flagged, never cut by the deterministic pass
    assert slot in ai_tells.rewrite(text, ai_tells.TellContext())[0]


@pytest.mark.parametrize("sentence", [
    "Lunch is at the <strong>riverside</strong> café, with <em>noodles</em> on the menu.",
    "<p>The road climbs north from the town.</p>",
    "The camp has <br> hot showers <br/> and a <a href=\"https://example.com\">map</a> at the gate.",
    "<h2>Day 2</h2> The road climbs north <span>from</span> the town.",
    "Groups of 3 < 5 people and 7 > 2 guides walk the ridge together.",
    "The ridge sits at <1,200 m> above the valley floor.",
    "Lunch is at the riverside café, with noodles on the menu.",
])
def test_html_tags_and_plain_text_are_not_slots(sentence):
    assert not _SLOT_RX.search(sentence)
    assert ai_tells.residue(f"## Day 2\n\n{sentence}\n") == []


def _slot_time(s: str) -> float:
    best = float("inf")
    for _ in range(3):
        t = time.perf_counter()
        _SLOT_RX.search(s)
        list(_SLOT_RX.finditer(s))
        best = min(best, time.perf_counter() - t)
    return best


@pytest.mark.parametrize("unit", ["<", "<a", "<place ", "<" + "a" * 59, "<p", "x <...", "< > "])
def test_the_slot_check_is_linear(unit):
    """8000 characters of near-misses (an unclosed '<', a slot that runs past 60 characters): growth ratio, not a
    wall-clock budget (CI runners are 2-3x slower). 4x the input must cost < 8x the time (+20 ms timer noise)."""
    small = unit * (8000 // len(unit))
    big = small * 4
    t_small, t_big = _slot_time(small), _slot_time(big)
    assert t_big < 8 * t_small + 0.02, f"{unit!r}: {t_small * 1000:.1f} ms -> {t_big * 1000:.1f} ms at 4x"
    assert t_big < 0.5
