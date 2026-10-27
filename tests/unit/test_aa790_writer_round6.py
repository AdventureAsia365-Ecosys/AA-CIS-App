"""AA-790 round 6 (S252) — prompt-side only, every round-4/5 AI-tell rule and FAMILY_RULE kept byte for byte. The
6-arm blind panel of round 5 (AI-tells 4.88 → 5.58 against round 3, fidelity 7.35 → 7.60) found non-family brand
voice below round 3: Trail Pulse 6.83 → 6.0 ("long middle stretches read like an encyclopedia", "the FAQ is formal
brochure copy"), Atlas & Hearth 8.2 → 7.4 ("reference prose with little atmosphere"), WildKind 7.5 → 7.0
("encyclopedia more than observant nature writing; brand voice in one bolted-on line"). Rounds 4/5 removed the
verdict / denial / aphorism lines that carried those voices; round 6 adds a BRAND TEXTURE block read from the pack:
MOMENTUM (energetic), ATMOSPHERE (luxury / cultural), OBSERVATION (nature / respectful), never for a family brand.
"""
import hashlib
import re

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import GOALS
from services.acp_content_writing import ai_tells, hygiene, prompts, quality_gates, service
from services.acp_content_writing.ai_tells import (BLOG_HEADING_RULE, BRAND_TEXTURE_ATMOSPHERE,
                                                   BRAND_TEXTURE_ATMOSPHERE_FAQ, BRAND_TEXTURE_COMMON,
                                                   BRAND_TEXTURE_FAQ, BRAND_TEXTURE_MOMENTUM,
                                                   BRAND_TEXTURE_MOMENTUM_FAQ, BRAND_TEXTURE_OBSERVATION,
                                                   BRAND_TEXTURE_OBSERVATION_FAQ, FAMILY_RULE,
                                                   FAMILY_SHORT_POST_RULE, PROMPT_RULES, SELF_CHECK_RULE,
                                                   SOCIAL_AUDIENCE_RULE, VISIT_DETAIL_RULE, TellContext,
                                                   brand_texture_rule, brand_textures, brand_voice_rule,
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
PLAIN = {"brand_name": "Plain Co", "brand_type": "Small group tours", "voice": ["Clear", "Friendly"]}
CHANNELS = ("blog", "linkedin", "facebook", "instagram", "tiktok", "ads", "email", "landing_page")
TEXTURE_TEXTS = (BRAND_TEXTURE_COMMON, BRAND_TEXTURE_FAQ, BRAND_TEXTURE_MOMENTUM, BRAND_TEXTURE_MOMENTUM_FAQ,
                 BRAND_TEXTURE_ATMOSPHERE, BRAND_TEXTURE_ATMOSPHERE_FAQ, BRAND_TEXTURE_OBSERVATION,
                 BRAND_TEXTURE_OBSERVATION_FAQ)
BLOCKS = {"momentum": BRAND_TEXTURE_MOMENTUM, "atmosphere": BRAND_TEXTURE_ATMOSPHERE,
          "observation": BRAND_TEXTURE_OBSERVATION}


def _flat(s: str) -> str:
    return " ".join(s.split())


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _prompt(channel: str, pack) -> str:
    kw = {"atom_id": "a1"} if channel == "blog" else {}
    return prompts.build_user_prompt(content_seed="Zorig Chusum\nSource itinerary: A visit.", goal=GOAL,
                                     channel_style=get_channel_style(channel), brand_audience=pack, angle=ANGLE,
                                     cta="Message us.", **kw)


# ---------------------------------------------------------------- versions and the rules round 6 must not touch

_BRIDGE = ("A BRAND TEXTURE block, when the brief has one, belongs to the style guide: it sets sentence shape and "
           "where the reader stands, never content.")


# round 7's own bridge sentence (test_aa790_writer_round7), taken out to compare with round 5
_ROUND7_BRIDGE = (" A BRAND FOCUS block, when the\n  brief has one, is the style guide's own points: it says which of "
                  "them to cover where the content seed has a\n  fact for it, never content of its own.")


def test_writer_prompt_version_is_round_six():
    """SYSTEM_PROMPT's style-guide rule gains the BRAND TEXTURE bridge, so the SYSTEM_PROMPT hash moves."""
    # round 6 shipped b5978839, round 7 pins its own: test_aa790_writer_round7.test_writer_prompt_version_is_round_seven
    assert service.WRITER_PROMPT_VERSION not in ("79cd046a", "2b510e08", "24363541")
    assert ai_tells.AI_TELLS_VERSION == "ai_tells.v3"  # the pass makes no new edit


def test_system_prompt_is_round_five_plus_the_bridge():
    assert _BRIDGE in _flat(prompts.SYSTEM_PROMPT)
    before = prompts.SYSTEM_PROMPT.replace(_ROUND7_BRIDGE, "").replace(
        " A BRAND TEXTURE block, when the brief has one, belongs to the\n  style guide: it sets sentence shape and "
        "where the reader stands, never content.", "")
    assert hashlib.sha256(before.encode()).hexdigest() == (
        "79cd046aaa1f5585e78cd1804f27a0388dacf754908bf50e910610d1e9122905")


def test_round_four_and_five_rules_are_byte_identical():
    """sha256[:16] on origin/main at round 5 (33c09fc0)."""
    assert _sha16(PROMPT_RULES + SELF_CHECK_RULE) == "9ea12897753804d2"
    assert _sha16(FAMILY_RULE) == "682a32216b48e2ae"
    assert _sha16(FAMILY_SHORT_POST_RULE) == "4648e5c991b1e038"
    assert _sha16(VISIT_DETAIL_RULE) == "90f0442da4629567"
    assert _sha16(BLOG_HEADING_RULE) == "a2a8be6f07191274"
    assert _sha16(SOCIAL_AUDIENCE_RULE) == "86c478b0645ddd48"
    for rule in (PROMPT_RULES, SELF_CHECK_RULE, VISIT_DETAIL_RULE):
        assert rule in prompts.SYSTEM_PROMPT


def test_energy_lines_are_unchanged():
    assert brand_voice_rule(TERRA) == ""
    assert "PACE FOR THIS BRAND (its voice: Energetic, Lightly playful)" in brand_voice_rule(TRAIL)
    assert "RESTRAINT FOR THIS BRAND (its voice: Elegant, Discreet, Calm)" in brand_voice_rule(ATLAS)
    assert "RESTRAINT FOR THIS BRAND (its voice: Thoughtful, Grounded, Respectful, Calm)" in brand_voice_rule(WILD)


# ---------------------------------------------------------------- detection from the pack

def test_the_three_panel_brands():
    assert brand_textures(TRAIL) == {"momentum": ["Energetic", "Lightly playful"]}
    assert brand_textures(ATLAS) == {"atmosphere": ["Elegant", "Cultured", "Atmospheric",
                                                    "Luxury cultural travel brand"]}
    assert brand_textures(WILD) == {"observation": ["Respectful", "Nature-led", "Ethical",
                                                    "Responsible nature and conservation travel brand"]}


@pytest.mark.parametrize("voice", [["Energetic"], ["Lightly playful"], ["Bold, never loud"], ["Calm", "Upbeat"],
                                   ["Sporty"], ["Not calm", "dynamic"], ["Never playful"], ["Calm"], []])
def test_momentum_reuses_the_pace_lines_test(voice):
    """MOMENTUM fires exactly when brand_voice_rule writes the PACE line, from the same pack words."""
    pace = brand_voice_rule({"voice": voice})
    found = brand_textures({"voice": voice}).get("momentum", [])
    assert bool(found) == ("PACE FOR THIS BRAND" in pace)
    if found:
        assert f"PACE FOR THIS BRAND (its voice: {', '.join(found)})" in pace


@pytest.mark.parametrize("pack, texture", [
    ({"voice": ["Elegant"]}, "atmosphere"),
    ({"voice": ["Refined", "Calm"]}, "atmosphere"),
    ({"voice": ["Atmospheric"]}, "atmosphere"),
    ({"brand_type": "Luxury river cruises"}, "atmosphere"),
    ({"brand_type": "Cultural journeys in Asia"}, "atmosphere"),
    ({"brand_type": "High-end heritage tours"}, "atmosphere"),
    ({"voice": ["Nature-led"]}, "observation"),
    ({"voice": ["Respectful"]}, "observation"),
    ({"brand_type": "Wildlife safaris"}, "observation"),
    ({"brand_type": "Eco lodges and conservation trips"}, "observation"),
    ({"voice": ["Energetic"]}, "momentum"),
])
def test_each_signal_is_read_from_voice_or_brand_type(pack, texture):
    assert texture in brand_textures(pack)
    assert BLOCKS[texture] in brand_texture_rule(pack)


@pytest.mark.parametrize("pack", [
    None, {}, PLAIN, FAMRUN,
    {"voice": ["Calm"]},  # calm alone is RESTRAINT, not a texture
    {"voice": ["Thoughtful", "Grounded", "Informative"]},
    {"brand_type": "Boutique tours"},
    {"brand_type": "Not a luxury brand", "voice": ["Clear"]},
    {"brand_type": "Small group tours, never luxury"},
    {"voice": ["Never elegant", "Not respectful of hype"]},
    {"brand_type": "City breaks, no wildlife"},
])
@pytest.mark.parametrize("channel", [None, *CHANNELS])
def test_no_texture_without_a_signal(pack, channel):
    assert brand_textures(pack) == {}
    assert brand_texture_rule(pack, channel) == ""


@pytest.mark.parametrize("pack", [
    TERRA,
    {**TERRA, "voice": [*TERRA["voice"], "Energetic", "Elegant"]},
    {"brand_type": "Luxury family safaris", "voice": ["Nature-led"]},
    {"voice": ["Family-aware", "Playful"]},
])
@pytest.mark.parametrize("channel", [None, *CHANNELS])
def test_a_family_brand_gets_no_texture(pack, channel):
    """FAMILY_RULE carries a family brand's voice (round 5 fixed Terra: 6.75 > round 3's 6.65)."""
    assert family_presence_rule(pack)
    assert brand_textures(pack) == {}
    assert brand_texture_rule(pack, channel) == ""


def test_no_tenant_name_is_hardcoded():
    src = open(ai_tells.__file__, encoding="utf-8").read()
    start = src.index("_ATMOSPHERE_VOICE = ")
    code = src[start:src.index("def brand_texture_rule")]
    for name in ("trail", "pulse", "atlas", "hearth", "wildkind", "terra"):
        assert name not in code.lower(), name
    renamed = {**ATLAS, "brand_name": "Trail Pulse"}
    assert brand_textures(renamed) == brand_textures(ATLAS)


# ---------------------------------------------------------------- the rule text

def test_common_lines():
    common = _flat(BRAND_TEXTURE_COMMON)
    assert ("Every factual paragraph comes back to the traveller within two sentences: after at most two sentences "
            "about what a place is") in common
    assert "the next sentence puts the reader there, in a moment the CONTENT SEED gives" in common
    assert "A stretch that reads like a reference work or an encyclopedia entry is not finished." in common
    assert "When the seed gives no moment for a place, keep its facts to two sentences and move on." in common
    assert "SOURCE FIDELITY, VISIT DETAIL and the AI TELLS rules apply to every line" in common
    assert ("No verdict line, no aphorism, no praise, no denial set-up and no list of three to make the voice."
            in common)
    faq = _flat(BRAND_TEXTURE_FAQ)
    assert "The FAQ answers are written in this voice too, never as formal brochure copy" in faq
    assert "one fact from the CONTENT SEED or FACTS" in faq


def test_momentum_text():
    rule = _flat(BRAND_TEXTURE_MOMENTUM)
    assert "short, concrete action sentences built on what the CONTENT SEED says the reader does" in rule
    assert "(riding, walking, swimming, climbing, paddling)" in rule and 'written to "you"' in rule
    assert "never a comment on the trip" in rule
    for verdict in ('"Four days in."', '"That\'s the pace this route sets."',
                    '"This is the day the route announces itself."'):
        assert verdict in rule, verdict  # named as what NOT to write
    assert '"you", short sentences, the fact first' in _flat(BRAND_TEXTURE_MOMENTUM_FAQ)


def test_atmosphere_text():
    rule = _flat(BRAND_TEXTURE_ATMOSPHERE)
    assert "concrete sensory or material detail the CONTENT SEED or FACTS give" in rule
    # round 7: "told in unhurried, well-built sentences (still within the AI TELLS length limits)" lengthened the
    # round-6 sentences (Atlas & Hearth 21.4 → 24.4 words); test_aa790_writer_round7 pins the new wording
    assert "calm, plainly built sentences of ordinary length" in rule
    assert ("Only detail the seed gives: SOURCE FIDELITY forbids inventing what a place looks, sounds or feels like."
            in rule)
    assert "aphorism" in rule and '"The architecture is the devotion made structural."' in rule
    assert '("one of the most layered destinations in ...")' in rule
    assert "a line about what a place asks of or gives the reader" in rule
    assert "When the seed gives no sensory detail, the calm comes from the sentence itself" in rule
    assert '"is renowned for"' in BRAND_TEXTURE_ATMOSPHERE_FAQ and "brochure phrasing" in BRAND_TEXTURE_ATMOSPHERE_FAQ


def test_observation_text():
    rule = _flat(BRAND_TEXTURE_OBSERVATION)
    assert "an observant, specific voice: name what the CONTENT SEED says is there" in rule
    assert 'No drama words ("dramatic", "spectacular", "stunning")' in rule
    assert "no verdict about what a place means or drives" in rule
    assert "never a sighting or an encounter the seed does not give" in rule
    assert "observant words as the body" in _flat(BRAND_TEXTURE_OBSERVATION_FAQ)


@pytest.mark.parametrize("pack, key", [(TRAIL, "momentum"), (ATLAS, "atmosphere"), (WILD, "observation")])
def test_rule_for_each_brand_type(pack, key):
    blog = brand_texture_rule(pack, "blog")
    post = brand_texture_rule(pack, "linkedin")
    words = ", ".join(brand_textures(pack)[key])
    head = (f"\nBRAND TEXTURE FOR THIS BRAND (from its pack: {words}): the brand's voice comes from how each "
            "sentence is built and where the reader stands, never from a line that comments on the trip.")
    faq = {"momentum": BRAND_TEXTURE_MOMENTUM_FAQ, "atmosphere": BRAND_TEXTURE_ATMOSPHERE_FAQ,
           "observation": BRAND_TEXTURE_OBSERVATION_FAQ}[key]
    assert blog == head + BRAND_TEXTURE_COMMON + BRAND_TEXTURE_FAQ + BLOCKS[key] + faq
    assert post == head + BRAND_TEXTURE_COMMON + BLOCKS[key]  # no FAQ off the blog
    for other, block in BLOCKS.items():
        assert (block in blog) == (other == key)


def test_several_textures_render_in_a_fixed_order_with_one_common_part():
    pack = {"voice": ["Respectful", "Energetic"], "brand_type": "Luxury expeditions"}
    rule = brand_texture_rule(pack, "blog")
    assert rule.count(BRAND_TEXTURE_COMMON) == 1 and rule.count(BRAND_TEXTURE_FAQ) == 1
    assert (rule.index(BRAND_TEXTURE_MOMENTUM) < rule.index(BRAND_TEXTURE_ATMOSPHERE)
            < rule.index(BRAND_TEXTURE_OBSERVATION))
    assert "(from its pack: Energetic, Luxury expeditions, Respectful)" in rule


# ---------------------------------------------------------------- DON'T / WRITE INSTEAD pairs

def _pairs(block: str) -> list[tuple[str, str]]:
    items = _flat(block).split(" - DON'T ")[1:]
    return [(i.split(" WRITE ", 1)[0], i.split(" WRITE ", 1)[1]) for i in items]


@pytest.mark.parametrize("key", sorted(BLOCKS))
def test_one_or_two_pairs_per_brand_type_each_write_a_seed_slot(key):
    pairs = _pairs(BLOCKS[key])
    assert 1 <= len(pairs) <= 2
    for dont, write in pairs:
        quoted = re.findall(r'"([^"]+)"', write)
        assert quoted, write
        for q in quoted:
            assert re.search(r"<[^<>]+>", q), q  # the fact comes from a slot filled from the seed
        assert not re.search(r"<[^<>]+>", re.findall(r'"([^"]+)"', dont)[0]), dont


def test_dont_halves_are_the_panel_shapes():
    donts = " ".join(d for b in BLOCKS.values() for d, _ in _pairs(b))
    assert "multi-tiered limestone system" in donts  # Trail Pulse r5: "read like an encyclopedia"
    assert "that matters." in donts  # Trail Pulse r3 voice beat the ai_tells lens docked
    assert "one of the most layered destinations" in donts  # Atlas & Hearth r5 closing praise
    assert "also known by its local name, flows through the valley" in donts  # Atlas r5 "flat fact-listing"
    assert "The geology here drives everything visible above it." in donts  # WildKind r5 verdict
    assert "gives the route its northernmost cultural depth" in donts  # WildKind r5 verdict


def test_every_texture_slot_is_caught_if_copied():
    """A copied <slot> is residue: F15_voice stock-line repair, never shipped silently."""
    rx = hygiene.AI_TELL_LINE_RES[-1]
    slots = re.findall(r"<[^<>]+>", "".join(_flat(b) for b in BLOCKS.values()))
    assert len(slots) >= 10
    for slot in slots:
        assert "\n" not in slot and len(slot) <= 62, slot
        assert rx.search(f"Lunch is at {slot} by the river."), slot
    for block in BLOCKS.values():  # never split over two lines inside the rule either
        assert not re.search(r"<[^<>]*\n[^<>]*>", block)
    sentence = "You ride <the distance or road the content seed gives> to the falls, then swim."
    text = f"## Day 2\n\nThe road climbs north from the town.\n\n{sentence}\n"
    assert any(p.startswith("<the distance") for p, _ in ai_tells.residue(text))
    assert any("stock line" in v for v in quality_gates.gate_voice(text, spec=None)["violations"])


# Each WRITE half with its slots filled from a made-up seed: none is a shape the AI-tell rules forbid.
_FILLED = [
    "You ride 30 kilometres on the river road to the falls, then swim in the lower pools.",
    "After the morning ride from the old town, you eat lunch at the village of Ban Na.",
    "You reach the fortress in the late afternoon, when the light falls on the two rivers.",
    "You stay at the lodge in the high valley, beside the Chhu river.",
    "On the way to the river town, you pass the limestone forest.",
    "In Ban Hat, the Akha families grow tea on the slopes, and you share lunch in a family home.",
]


def test_the_filled_write_halves_match_the_pairs():
    writes = [w for b in BLOCKS.values() for _, w in _pairs(b)]
    assert len(writes) == len(_FILLED)
    for write, filled in zip(writes, _FILLED):
        template = re.findall(r'"([^"]+)"', write)[0]
        pattern = re.escape(template)
        pattern = re.sub(r"<[^<>]+>", ".+", pattern.replace(r"\<", "<").replace(r"\>", ">"))
        assert re.fullmatch(pattern, filled), (template, filled)


@pytest.mark.parametrize("sentence", _FILLED)
def test_no_write_half_is_an_ai_tell(sentence):
    lead = "The old capital sits where two rivers meet."
    text = "\n\n".join([lead, "## The Route", "The monks gather at dawn. " + sentence,
                        "The day ends at the riverside camp."])
    out, log = ai_tells.rewrite(text, TellContext(cta="Message us."))
    assert out == text and log == []
    assert ai_tells.residue(text) == [] and hygiene.template_lines(text) == []
    flat = sentence.lower()
    for shape in (" not ", "rather than", "more than", "earns", "matters", "worth ", "the kind of", "asks "):
        assert shape not in f" {flat} ", shape
    assert flat.count(",") <= 2  # no rhythmic list of three


def test_new_text_keeps_the_prompt_contracts():
    for text in TEXTURE_TEXTS:
        assert "—" not in text
        assert "{" not in text and "}" not in text  # appended raw, never .format()ed
        for word in ("genuinely", "precisely", "premium", "luxury"):
            assert word not in text.lower(), word
        assert not re.search(r"\bAA\b", text)
        for name in ("Trail Pulse", "Atlas", "Hearth", "WildKind", "Terra", "Kuang Si", "Bhutan", "Paro"):
            assert name not in text, name


# ---------------------------------------------------------------- the user prompt

@pytest.mark.parametrize("pack, key", [(TRAIL, "momentum"), (ATLAS, "atmosphere"), (WILD, "observation")])
@pytest.mark.parametrize("channel", CHANNELS)
def test_texture_sits_after_the_energy_line_and_before_the_format(pack, key, channel):
    p = _prompt(channel, pack)
    rule = brand_texture_rule(pack, channel)
    assert p.count(rule) == 1 and p.count("BRAND TEXTURE FOR THIS BRAND") == 1
    assert p.index(brand_voice_rule(pack)) + len(brand_voice_rule(pack)) == p.index(rule)
    tail = "BLOG-SPECIFIC FORMAT" if channel == "blog" else (
        "FORMAT FOR THIS CHANNEL" if channel in prompts.PLAIN_TEXT_CHANNELS else None)
    if tail:
        assert p.index(rule) < p.index(tail)
    assert ("The FAQ answers are written in this voice too" in p) == (channel == "blog")


# sha256[:16] of build_user_prompt on origin/main at round 5 (33c09fc0), same seed / angle / CTA as _prompt.
_ROUND5_USER_PROMPTS = {
    ("TERRA", "blog"): "37d161a964077a8e", ("TERRA", "linkedin"): "be87bf0d4c424e93",
    ("TERRA", "facebook"): "9e06337e0de5142c", ("TERRA", "email"): "9b7a6cf54d7b7fc3",
    ("TERRA", "landing_page"): "be44f5d728fffeb3",
    ("FAMRUN", "blog"): "85b7199331786d81", ("FAMRUN", "linkedin"): "da1baebe741ad2df",
    ("FAMRUN", "facebook"): "79ebb8425a45ac25", ("FAMRUN", "email"): "0767af1bfde71d41",
    ("FAMRUN", "landing_page"): "9bc1ac9c65cfa560",
    ("PLAIN", "blog"): "1a975e3d87f4ef19", ("PLAIN", "linkedin"): "43243411a25f13ca",
    ("PLAIN", "facebook"): "6fe46d54fe1a295c", ("PLAIN", "email"): "f92dfe2d7a3e338a",
    ("PLAIN", "landing_page"): "91764d537a457e08",
    ("EMPTY", "blog"): "c58f2612c2633c58", ("EMPTY", "linkedin"): "3ed2c2a12330946f",
    ("EMPTY", "facebook"): "a3e8dd44f72f305c", ("EMPTY", "email"): "eb2c108162d07e6a",
    ("EMPTY", "landing_page"): "e249b242815d858b",
}
_PACKS = {"TERRA": TERRA, "FAMRUN": FAMRUN, "PLAIN": PLAIN, "EMPTY": {}}


@pytest.mark.parametrize("name, channel", sorted(_ROUND5_USER_PROMPTS))
def test_family_and_no_signal_user_prompts_are_byte_identical_to_round_five(name, channel):
    """Round 7 (S254) adds the ANGLE BRIEF line to every prompt; without it these are the round-5 bytes (no pack
    here has a style guide, so none gets a BRAND FOCUS block)."""
    p = _prompt(channel, _PACKS[name])
    assert p.count(ai_tells.ANGLE_BRIEF_RULE) == 1 and "BRAND FOCUS" not in p
    p = p.replace("\n" + ai_tells.ANGLE_BRIEF_RULE, "")
    assert "BRAND TEXTURE" not in p
    assert _sha16(p) == _ROUND5_USER_PROMPTS[(name, channel)]
    assert (FAMILY_RULE in p) == (name == "TERRA")
