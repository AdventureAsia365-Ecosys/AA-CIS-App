"""AA-790 round 5 (S252) — prompt-side only, every round-4 AI-tell change kept. The 5-arm blind panel of round 4
(AI-tells 5.05 → 5.50, bar 5.5) found Terra Family brand voice 7.0 → 5.5 and LinkedIn 6.92 → 5.08: "children never
appear concretely (only 'your family')", "families appear only as an abstraction", "dry and institutional rather
than warm", "no pace or comfort cues". Round 4's FAMILY_RULE caused it (speak to "your family", never give an
activity to the children, at most one family sentence per section). Round 5: the children and the parents do what
the seed says the group does, warmth from the seed's pace / effort / rest cues, no hard cap, three DON'T / WRITE
INSTEAD pairs, one concrete family moment in a short post. Non-family brands are byte-identical to round 4.
"""
import hashlib
import re

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import GOALS
from services.acp_content_writing import ai_tells, hygiene, prompts, quality_gates, service
from services.acp_content_writing.ai_tells import (FAMILY_RULE, FAMILY_SHORT_POST_CHANNELS, FAMILY_SHORT_POST_RULE,
                                                   PROMPT_RULES, SELF_CHECK_RULE, VISIT_DETAIL_RULE, brand_voice_rule,
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
FAMRUN = {"brand_name": "Lodge Co", "brand_type": "Boutique lodges, family run", "voice": ["Calm"]}
CHANNELS = ("blog", "linkedin", "facebook", "instagram", "tiktok", "ads", "email", "landing_page")


def _flat(s: str) -> str:
    return " ".join(s.split())


def _prompt(channel: str, pack: dict) -> str:
    kw = {"atom_id": "a1"} if channel == "blog" else {}
    return prompts.build_user_prompt(content_seed="Zorig Chusum\nSource itinerary: A visit.", goal=GOAL,
                                     channel_style=get_channel_style(channel), brand_audience=pack, angle=ANGLE,
                                     cta="Message us.", **kw)


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- versions

def test_writer_prompt_version_is_round_five():
    """VISIT DETAIL (SYSTEM_PROMPT) gains the FAMILY BRAND bridge sentence, so the SYSTEM_PROMPT hash moves."""
    assert service.WRITER_PROMPT_VERSION == "79cd046a"  # round 4: 2b510e08; round 3: 24363541
    assert ai_tells.AI_TELLS_VERSION == "ai_tells.v3"  # the pass makes no new edit


def test_round_four_ai_tell_rules_are_unchanged():
    """Round 5 keeps every round-4 AI-tell change: PROMPT_RULES (ten pairs) + SELF_CHECK_RULE, byte for byte."""
    assert _sha16(PROMPT_RULES + SELF_CHECK_RULE) == "9ea12897753804d2"
    assert SELF_CHECK_RULE in prompts.SYSTEM_PROMPT and PROMPT_RULES in prompts.SYSTEM_PROMPT


def test_visit_detail_bridge_is_scoped_to_a_family_brand_brief():
    rule = _flat(VISIT_DETAIL_RULE)
    assert ("When the brief has a FAMILY BRAND line, naming the children or the parents as the ones who see or do "
            "what the content seed says the group sees or does is not a new fact; the moment itself still comes "
            "only from the content seed.") in rule
    # the round-2 VISIT DETAIL contract is still there, word for word
    assert rule.startswith("- VISIT DETAIL: never say what a visit shows, includes or lets the reader watch, see or do")
    assert VISIT_DETAIL_RULE in prompts.SYSTEM_PROMPT


# ---------------------------------------------------------------- the rule text

def test_children_and_parents_do_what_the_seed_says_the_group_does():
    rule = _flat(FAMILY_RULE)
    assert "The children and the parents are part of the travelling group." in rule
    assert ("When the CONTENT SEED or FACTS say the group sees, does, tastes, walks, rides or sleeps somewhere, name "
            "the children or the parents as the ones who do it") in rule
    assert "that is the seed's own moment, not a new fact." in rule
    assert "Never give them a moment, a view or an activity the seed does not give the group." in rule
    # an optional / demanding / limited moment never becomes a suitability claim for the children
    assert ("When the seed marks a moment optional, demanding or limited to some of the group, keep the seed's own "
            "words about who it is for and speak to the family as a whole there.") in rule


def test_round_four_abstraction_and_cap_are_gone():
    rule = _flat(FAMILY_RULE)
    assert "never assign an activity to the children or to the parents" not in rule
    assert "At most one family sentence per section" not in rule
    assert 'Otherwise speak to the family as a whole ("your family")' not in rule
    assert 'do not let "your family" be the only sign of a family' in rule
    assert "Bring the family into most sections, never as a formula" in rule


def test_still_forbidden_inventions():
    rule = _flat(FAMILY_RULE)
    assert "SOURCE FIDELITY comes first: a family detail the seed does not give is left out, never guessed." in rule
    assert "No child-specific service, price, facility or activity the seed does not give" in rule
    assert ('An age, an age band or "suitable for children" appears ONLY when the CONTENT SEED or FACTS state it.'
            in rule)
    assert "The AUDIENCE line's ages describe the brand's customers, never this trip." in rule
    assert "Never invent what children feel, notice, learn, understand or remember" in rule
    assert '"works for every age"' in rule and "school-trip framing" in rule
    assert "without a made-up family line" in rule


def test_warmth_comes_from_the_seeds_own_pace_cues():
    rule = _flat(FAMILY_RULE)
    assert "Warmth comes from the pace, effort and rest cues the CONTENT SEED gives" in rule
    for cue in ("the timing", "the walking or driving time", "a meal or rest stop", "what is optional",
                "where the night is"):
        assert cue in rule, cue
    assert "never invent reassurance or comfort" in rule
    for invented in ('"no rush"', '"easy for little legs"', '"nothing to worry about"', '"at their own pace"'):
        assert invented in rule, invented  # named as examples of what NOT to invent


def test_no_formula_openers():
    rule = _flat(FAMILY_RULE)
    assert 'Do not open section after section with "For children, ..." or "For families, ..."' in rule
    assert 'never write "tends to land differently"' in rule
    assert "The AI TELLS rules apply here too." in rule


# ---------------------------------------------------------------- DON'T / WRITE INSTEAD pairs

def _family_pairs() -> list[tuple[str, str]]:
    block = _flat(FAMILY_RULE.split("DON'T / WRITE INSTEAD", 1)[1])
    items = block.split(" - DON'T ")[1:]
    return [(i.split(" WRITE ", 1)[0], i.split(" WRITE ", 1)[1]) for i in items]


def test_two_to_three_family_pairs_from_the_panel():
    pairs = _family_pairs()
    assert 2 <= len(pairs) <= 3
    donts = " ".join(d for d, _ in pairs)
    assert '"For families, this is a stop with a clear frame."' in donts  # the panel's own abstraction quote
    assert '"Children can watch a craft in progress."' in donts  # r3's invented visit format (fidelity lens)
    assert "the seed never says the group watches anyone work" in donts


def test_each_write_half_is_a_seed_slot_never_a_fact():
    for dont, write in _family_pairs():
        quoted = re.findall(r'"([^"]+)"', write)
        assert quoted, write
        for q in quoted:
            assert re.search(r"<[^<>]+>", q), q  # the fact comes from a slot filled from the seed
        assert len(re.findall(r"\w+", re.findall(r'"([^"]+)"', dont)[0])) <= 16, dont


def test_every_family_slot_is_caught_if_copied():
    """A copied <slot> is residue (round-4 review fix): F15_voice stock-line repair, never shipped silently."""
    rx = hygiene.AI_TELL_LINE_RES[-1]
    slots = re.findall(r"<[^<>]+>", _flat(FAMILY_RULE))
    assert len(slots) >= 5
    for slot in slots:
        assert "\n" not in slot and len(slot) <= 62, slot
        assert rx.search(f"Lunch is at {slot} by the river."), slot
    sentence = "At <place>, the children ride along the lake shore before lunch."
    text = f"## Day 2\n\nThe road climbs north from the town.\n\n{sentence}\n"
    assert ("<place>", sentence) in ai_tells.residue(text)
    assert any("stock line '<place>'" in v for v in quality_gates.gate_voice(text, spec=None)["violations"])


def test_new_text_keeps_the_prompt_contracts():
    em = "—"
    for text in (FAMILY_RULE, FAMILY_SHORT_POST_RULE, VISIT_DETAIL_RULE):
        assert em not in text
        assert "premium" not in text.lower() and not re.search(r"\bAA\b", text)
        assert "{" not in text and "}" not in text  # appended raw, never .format()ed
        assert "genuinely" not in text and "precisely" not in text


# ---------------------------------------------------------------- trigger

@pytest.mark.parametrize("pack", [
    TERRA,
    {"voice": ["Family-aware"]},
    {"brand_type": "Premium family adventure travel brand"},
    {"voice": ["Kid-friendly"]},
    {"brand_type": "Designed by parents, for parents"},
    {"voice": ["Calm, never preachy, family-focused"]},
])
def test_family_brands_get_the_rule(pack):
    assert family_presence_rule(pack) == FAMILY_RULE
    assert family_presence_rule(pack, "blog") == FAMILY_RULE


@pytest.mark.parametrize("pack", [
    TRAIL, ATLAS, WILD, FAMRUN, None, {},
    {"brand_type": "Small group tours", "audience": "couples and families"},
    {"brand_type": "Not a family brand", "voice": ["Calm"]},
    {"brand_type": "Adults-only luxury brand, not suitable for children"},
    {"brand_type": "Boutique tours, family owned since 1990"},
    {"brand_type": "A family business since 1990"},
    {"brand_type": "Run by the same family for three generations"},
    {"brand_type": "Boutique lodges owned by a local family"},
])
@pytest.mark.parametrize("channel", [None, *CHANNELS])
def test_no_rule_for_other_brands_or_ownership_wording(pack, channel):
    assert family_presence_rule(pack, channel) == ""


# ---------------------------------------------------------------- channels

@pytest.mark.parametrize("channel", sorted(FAMILY_SHORT_POST_CHANNELS))
def test_short_post_gets_one_concrete_family_moment(channel):
    assert FAMILY_SHORT_POST_CHANNELS == prompts.PLAIN_TEXT_CHANNELS
    assert family_presence_rule(TERRA, channel) == FAMILY_RULE + FAMILY_SHORT_POST_RULE
    p = _prompt(channel, TERRA)
    assert FAMILY_RULE + FAMILY_SHORT_POST_RULE in p
    assert p.index(FAMILY_SHORT_POST_RULE) < p.index("FORMAT FOR THIS CHANNEL")
    rule = _flat(FAMILY_SHORT_POST_RULE)
    assert "one concrete family moment from the CONTENT SEED is enough" in rule and "Keep the post short." in rule


@pytest.mark.parametrize("channel", ["blog", "email", "landing_page"])
def test_long_channels_get_the_rule_without_the_short_post_line(channel):
    p = _prompt(channel, TERRA)
    assert FAMILY_RULE in p and FAMILY_SHORT_POST_RULE not in p


def test_family_rule_comes_after_the_energy_line_and_before_the_blog_format():
    p = _prompt("blog", {**TERRA, "voice": [*TERRA["voice"], "Calm"]})
    assert p.index(brand_voice_rule({"voice": ["Calm"]})) < p.index(FAMILY_RULE) < p.index("BLOG-SPECIFIC FORMAT")


# ---------------------------------------------------------------- non-family brands are unchanged

# sha256[:16] of build_user_prompt on origin/main at round 4 (d7f485cd), same seed / angle / CTA as _prompt.
_ROUND4_USER_PROMPTS = {
    ("TRAIL", "blog"): "a201860e29de2247", ("TRAIL", "linkedin"): "2ac41c9e446733a5",
    ("TRAIL", "facebook"): "b31303ad89dfd7d5", ("TRAIL", "email"): "52c6f4e418902eab",
    ("TRAIL", "landing_page"): "e654dce6386a5fed",
    ("ATLAS", "blog"): "fc657c5b7e3f79a6", ("ATLAS", "linkedin"): "e4f9e1f5611970bc",
    ("ATLAS", "facebook"): "434029ee0e3420d3", ("ATLAS", "email"): "5c79419b8d525a08",
    ("ATLAS", "landing_page"): "851661ba4b1f169c",
    ("WILD", "blog"): "835ff069818008fc", ("WILD", "linkedin"): "8ee646671384091c",
    ("WILD", "facebook"): "5f531681c430d86d", ("WILD", "email"): "5be5484b9c015d45",
    ("WILD", "landing_page"): "9307615b65505944",
    ("FAMRUN", "blog"): "85b7199331786d81", ("FAMRUN", "linkedin"): "da1baebe741ad2df",
    ("FAMRUN", "facebook"): "79ebb8425a45ac25", ("FAMRUN", "email"): "0767af1bfde71d41",
    ("FAMRUN", "landing_page"): "9bc1ac9c65cfa560",
}
_PACKS = {"TRAIL": TRAIL, "ATLAS": ATLAS, "WILD": WILD, "FAMRUN": FAMRUN}


@pytest.mark.parametrize("name, channel", sorted(_ROUND4_USER_PROMPTS))
def test_non_family_user_prompts_are_byte_identical_to_round_four(name, channel):
    p = _prompt(channel, _PACKS[name])
    assert "FAMILY BRAND" not in p and FAMILY_SHORT_POST_RULE not in p
    assert _sha16(p) == _ROUND4_USER_PROMPTS[(name, channel)]


def test_energy_lines_are_unchanged():
    assert brand_voice_rule(TERRA) == ""
    assert "PACE FOR THIS BRAND (its voice: Energetic, Lightly playful)" in brand_voice_rule(TRAIL)
    assert "RESTRAINT FOR THIS BRAND (its voice: Thoughtful, Grounded, Respectful, Calm)" in brand_voice_rule(WILD)
