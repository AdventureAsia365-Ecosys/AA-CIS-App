"""AA-790 round 7 (S254) — prompt-side, plus one residue shape; the deterministic pass is unchanged. The S252 7-arm
blind panel put brand voice at round 3 7.17 → round 6 6.75. Offline diagnosis ($0, stored panel + re-panel texts):

* the T8 angle brief is copied as wording: 20 of 42 Terra LinkedIn posts of rounds 1-6 end on "… is a structured,
  accessible stop", the brief's own words, for a stop whose seed has no practical fact (judges: "institutional");
* the brand's own style-guide points (pace, rest, comfort; guiding, privacy; social; habitats) are not covered, and
  nothing in the brief maps them to seed facts;
* family brands never got the round-6 "back to the traveller" line;
* the ATMOSPHERE texture's "unhurried, well-built sentences" lengthened round-6 sentences.

Round 7: ANGLE_BRIEF_RULE after the angle lines, the BRAND FOCUS block from the style guide (met only from the seed),
ordinary sentence length in ATMOSPHERE, and the "a ADJ, ADJ stop" verdict as F15 residue.
"""
import hashlib
import re

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import GOALS
from services.acp_content_writing import ai_tells, hygiene, prompts, quality_gates, service
from services.acp_content_writing.ai_tells import (ANGLE_BRIEF_RULE, BLOG_HEADING_RULE, BRAND_FOCUS_HEAD,
                                                   BRAND_FOCUS_TRAVELLER_HEAD, BRAND_FOCUS_USE,
                                                   BRAND_TEXTURE_ATMOSPHERE, BRAND_TEXTURE_COMMON,
                                                   BRAND_TEXTURE_FAQ, BRAND_TEXTURE_MOMENTUM,
                                                   BRAND_TEXTURE_OBSERVATION, FAMILY_RULE, FAMILY_SHORT_POST_RULE,
                                                   FOCUS_MAX_POINTS, PROMPT_RULES, SELF_CHECK_RULE,
                                                   SOCIAL_AUDIENCE_RULE, TRAVELLER_RETURN_LINE, VISIT_DETAIL_RULE,
                                                   TellContext, brand_focus_points, brand_focus_rule,
                                                   brand_texture_rule, brand_textures, family_presence_rule)

GOAL = GOALS[0]
ANGLE = {"name": "The Craft School", "why_it_works": "w", "formula_fit": "f",
         "best_final_style": "Close on it being an accessible, structured stop."}
CHANNELS = ("blog", "linkedin", "facebook", "instagram", "tiktok", "ads", "email", "landing_page")

# The four test brands' style guides (invented test tenants), as their packs hold them.
TERRA_GUIDE = """Explain the pace clearly.
Mention comfort, safety, learning, and variety.
Show how the trip works for both adults and children.
Use simple but polished language.
Avoid childish language.

Write like this:
“Each day balances active discovery with time to rest, making the journey engaging for children.”

Avoid:
Do not make the tour sound like a school trip."""
ATLAS_GUIDE = """Use slower, more polished sentences.
Focus on place, heritage, atmosphere, guiding, and privacy.
Avoid too much operational detail unless it supports comfort or exclusivity.
Make the journey feel layered and personal.

Write like this:
“This journey moves at a considered pace, balancing historic cities, quiet landscapes, and private moments.”

Avoid:
Do not use backpacker or group-tour language."""
TRAIL_GUIDE = """Use active verbs.
Keep sentences short and dynamic.
Make the trip feel social, mobile, and alive.
Focus on what the traveller does, feels, and discovers.
Use natural modern language, not formal travel-brochure language.

Write like this:
“Start the morning on a forest trail, spend the afternoon cycling between villages.”

Avoid:
Do not sound too premium or formal.
Second person, active verbs, contractions are fine, no consultancy maxims."""
WILD_GUIDE = """Use careful nature language.
Be specific about landscapes and habitats.
Be cautious with wildlife claims.
Show respect for communities, guides, and ecosystems.
Avoid exaggerated sustainability claims unless verified.
Use calm, observant writing.

Write like this:
“Travellers spend time in forests, wetlands, and village areas.”

Avoid:
Do not use exploitative wildlife language like “get up close”."""

TERRA = {"brand_name": "Terra Family Expeditions", "brand_type": "Premium family adventure travel brand",
         "voice": ["Warm", "Reassuring", "Clear", "Practical", "Family-aware", "Encouraging", "Trustworthy"],
         "style_guide": TERRA_GUIDE}
TRAIL = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
         "voice": ["Energetic", "Fresh", "Direct", "Friendly", "Modern", "Confident", "Lightly playful"],
         "style_guide": TRAIL_GUIDE}
ATLAS = {"brand_name": "Atlas & Hearth", "brand_type": "Luxury cultural travel brand",
         "voice": ["Elegant", "Discreet", "Cultured", "Calm", "Precise", "Atmospheric", "Intelligent"],
         "style_guide": ATLAS_GUIDE}
WILD = {"brand_name": "WildKind Travel", "brand_type": "Responsible nature and conservation travel brand",
        "voice": ["Thoughtful", "Grounded", "Respectful", "Nature-led", "Ethical", "Calm", "Informative"],
        "style_guide": WILD_GUIDE}
PLAIN_GUIDE = {"brand_name": "Plain Co", "brand_type": "Small group tours", "voice": ["Clear", "Friendly"],
               "style_guide": "Keep it practical. Name the day."}
PANEL = {"TERRA": TERRA, "TRAIL": TRAIL, "ATLAS": ATLAS, "WILD": WILD}


def _flat(s: str) -> str:
    return " ".join(s.split())


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _prompt(channel: str, pack, angle=ANGLE) -> str:
    kw = {"atom_id": "a1"} if channel == "blog" else {}
    return prompts.build_user_prompt(content_seed="Zorig Chusum\nSource itinerary: A visit.", goal=GOAL,
                                     channel_style=get_channel_style(channel), brand_audience=pack, angle=angle,
                                     cta="Message us.", **kw)


# ---------------------------------------------------------------- versions and what round 7 must not touch

_ROUND7_BRIDGE = ("A BRAND FOCUS block, when the brief has one, is the style guide's own points: it says which of "
                  "them to cover where the content seed has a fact for it, never content of its own.")


def test_writer_prompt_version_is_round_seven():
    """SYSTEM_PROMPT's style-guide rule gains the BRAND FOCUS bridge, so the SYSTEM_PROMPT hash moves."""
    # round 6: b5978839; round 5: 79cd046a; round 4: 2b510e08; round 3: 24363541
    assert service.WRITER_PROMPT_VERSION == "ccad14c4"
    assert ai_tells.AI_TELLS_VERSION == "ai_tells.v3"  # the pass makes no new edit


def test_system_prompt_is_round_six_plus_the_bridge():
    assert _ROUND7_BRIDGE in _flat(prompts.SYSTEM_PROMPT)
    before = prompts.SYSTEM_PROMPT.replace(
        " A BRAND FOCUS block, when the\n  brief has one, is the style guide's own points: it says which of them to "
        "cover where the content seed has a\n  fact for it, never content of its own.", "")
    assert hashlib.sha256(before.encode()).hexdigest() == (
        "b5978839f7b24efec987aa89e4ffa77a50af2023d1c8ce14b734e9eb9b0ad17f")


def test_round_six_rules_are_byte_identical():
    """sha256[:16] on origin/main at round 6 (9c0fe930 + #1039)."""
    assert _sha16(PROMPT_RULES) == "b7c3298b75c1e49f" and _sha16(SELF_CHECK_RULE) == "f6dda73fc1c2a2a9"
    assert _sha16(FAMILY_RULE) == "682a32216b48e2ae"
    assert _sha16(FAMILY_SHORT_POST_RULE) == "4648e5c991b1e038"
    assert _sha16(VISIT_DETAIL_RULE) == "90f0442da4629567"
    assert _sha16(BLOG_HEADING_RULE) == "a2a8be6f07191274"
    assert _sha16(SOCIAL_AUDIENCE_RULE) == "86c478b0645ddd48"
    assert _sha16(BRAND_TEXTURE_COMMON) == "23c73c5825701f16"  # rebuilt from TRAVELLER_RETURN_LINE, same bytes
    assert _sha16(BRAND_TEXTURE_FAQ) == "2c23e3f4edc9f3db"
    assert _sha16(BRAND_TEXTURE_MOMENTUM) == "286c69f9c08bc273"
    assert _sha16(BRAND_TEXTURE_OBSERVATION) == "d5605351c51b83b2"
    assert BRAND_TEXTURE_COMMON.startswith("\n" + TRAVELLER_RETURN_LINE + "\n- Texture never adds content")


def test_atmosphere_changes_only_its_sentence_length_words():
    """Same words as round 6 but the sentence-length phrase (the line breaks moved with the new words)."""
    old = _flat(BRAND_TEXTURE_ATMOSPHERE).replace(
        "told in calm, plainly built sentences of ordinary length (one fact or one moment each; the AI TELLS length "
        "limits apply) with the reader present in the moment.",
        "told in unhurried, well-built sentences (still within the AI TELLS length limits) with the reader present "
        "in the moment.")
    assert old != _flat(BRAND_TEXTURE_ATMOSPHERE)
    assert _sha16(old) == "a63909c6927be3b5"  # _flat(round-6 BRAND_TEXTURE_ATMOSPHERE)
    assert "unhurried" not in BRAND_TEXTURE_ATMOSPHERE  # S1 writer v2 lists it as abstract filler


# ---------------------------------------------------------------- BRAND FOCUS: reading the style guide

@pytest.mark.parametrize("name, expected", [
    ("TERRA", ["Explain the pace clearly.", "Mention comfort, safety, learning, and variety.",
               "Show how the trip works for both adults and children.", "Use simple but polished language.",
               "Avoid childish language."]),
    ("ATLAS", ["Use slower, more polished sentences.", "Focus on place, heritage, atmosphere, guiding, and privacy.",
               "Avoid too much operational detail unless it supports comfort or exclusivity.",
               "Make the journey feel layered and personal."]),
    ("TRAIL", ["Use active verbs.", "Keep sentences short and dynamic.",
               "Make the trip feel social, mobile, and alive.",
               "Focus on what the traveller does, feels, and discovers.",
               "Use natural modern language, not formal travel-brochure language."]),
    ("WILD", ["Use careful nature language.", "Be specific about landscapes and habitats.",
              "Be cautious with wildlife claims.", "Show respect for communities, guides, and ecosystems.",
              "Avoid exaggerated sustainability claims unless verified.", "Use calm, observant writing."]),
])
def test_the_four_panel_brands(name, expected):
    """The points end at the first header ('Write like this:'): no example sentence and no 'Avoid:' item."""
    assert brand_focus_points(PANEL[name]) == expected


@pytest.mark.parametrize("guide, expected", [
    ("- Focus on the river.\n* Name the day.\n• Keep it short.\n1. Use verbs.\n2) Say where the night is.",
     ["Focus on the river.", "Name the day.", "Keep it short.", "Use verbs.", "Say where the night is."]),
    ("Focus on the group. Keep it short! Name the day", ["Focus on the group.", "Keep it short!", "Name the day."]),
    ("Focus on the group.\nAvoid: hype, slang.\nName the day.", ["Focus on the group."]),
    ("Focus on the group.\nExamples: a group that rides together.", ["Focus on the group."]),
    ("Focus on the group. Avoid: hype. Name the day.", ["Focus on the group."]),
    ("Focus on the group.\nWrite like “Ride the coast road.”\nName the day.", ["Focus on the group.", "Name the day."]),
    ("Focus on the group.\n\n\nName the day.", ["Focus on the group.", "Name the day."]),
    ("Calm.\nFocus on the group.", ["Focus on the group."]),  # one word is not a point
    # review fixes: a 'Label: rule' line is a point; only a bare label or a known example / don't label is a header
    ("Note: the brand is calm. Focus on the group.", ["Note: the brand is calm.", "Focus on the group."]),
    ("Tone: warm and calm. Pace: unhurried.\nFocus on the family.",
     ["Tone: warm and calm.", "Pace: unhurried.", "Focus on the family."]),
    ("Do: use verbs.\nDon't: use hype.\nName the day.", ["Do: use verbs."]),
    ("Lead with the practical: pace, rest, comfort.\nUse short sentences.",
     ["Lead with the practical: pace, rest, comfort.", "Use short sentences."]),
    ("Explain the pace: day lengths, rest and comfort.\nMention safety.",
     ["Explain the pace: day lengths, rest and comfort.", "Mention safety."]),
    ("Tone: warm, clear and practical.\nExplain the pace clearly.",
     ["Tone: warm, clear and practical.", "Explain the pace clearly."]),
    ("Focus on the group.\nNever: hype.\nName the day.", ["Focus on the group."]),
    ("Focus on the group.\nWrite like this: “Ride the coast road.”\nName the day.", ["Focus on the group."]),
    ("Focus on the group.\nTips:\nName the day.", ["Focus on the group."]),  # a bare label is a header
    # a quoted word is part of a rule; a quoted example sentence (4+ words, or an unclosed quote) is skipped
    ('Use "you" for the traveller.\nName the day.', ['Use "you" for the traveller.', "Name the day."]),
    ("Say “we” for the guides.", ["Say “we” for the guides."]),
    ("Focus on the group.\n“Each day balances active discovery\nName the day.",
     ["Focus on the group.", "Name the day."]),
    # no split after an abbreviation or before a lower-case word
    ("Use short sentences, e.g. three per paragraph.", ["Use short sentences, e.g. three per paragraph."]),
    ("Name the place, i.e. Paro. Keep it short.", ["Name the place, i.e. Paro.", "Keep it short."]),
    ("Name the saint, as in St. Paul. Keep it short.", ["Name the saint, as in St. Paul.", "Keep it short."]),
    ("Write like this:\n“Ride.”", []),
    ("", []),
    ("   \n  \n", []),
])
def test_parsing(guide, expected):
    assert brand_focus_points({"style_guide": guide}) == expected


def test_no_style_guide_no_points():
    for pack in (None, {}, {"brand_name": "X"}, {"style_guide": None}, {"voice": ["Calm"]}):
        assert brand_focus_points(pack) == [] and brand_focus_rule(pack) == ""


def test_points_are_capped_in_count_and_length():
    many = "\n".join(f"Point number {k} here." for k in range(20))
    assert len(brand_focus_points({"style_guide": many})) == FOCUS_MAX_POINTS
    long = "Focus on " + "the river and " * 30 + "the day.\nName the day."
    assert brand_focus_points({"style_guide": long}) == ["Name the day."]


def test_no_tenant_name_in_the_code():
    import inspect
    src = inspect.getsource(brand_focus_points) + inspect.getsource(brand_focus_rule)
    for name in ("Trail Pulse", "Atlas", "Hearth", "WildKind", "Terra"):
        assert name not in src


# ---------------------------------------------------------------- BRAND FOCUS: the block

def test_block_text():
    use = _flat(BRAND_FOCUS_USE)
    assert BRAND_FOCUS_HEAD == "\nBRAND FOCUS FOR THIS BRAND (the first rules of its style guide, in its own words):"
    assert "Cover each point wherever the CONTENT SEED or FACTS give you a fact for it" in use
    assert "check the piece against every point before you finish" in use
    assert "is met only by what the seed or FACTS give for it" in use
    assert "a day number, a walking or driving time, where the night is, a meal, what is optional" in use
    assert "When the seed gives nothing for a point, leave that point out: never a general line in its place" in use
    assert '"a structured, accessible stop"' in use
    assert "SOURCE FIDELITY, VISIT DETAIL and the AI TELLS rules apply to every line" in use


@pytest.mark.parametrize("name", sorted(PANEL))
def test_rule_shape(name):
    pack = PANEL[name]
    rule = brand_focus_rule(pack)
    points = "".join(f"\n- {p}" for p in brand_focus_points(pack))
    if brand_textures(pack):
        assert rule == BRAND_FOCUS_HEAD + points + BRAND_FOCUS_USE
    else:  # a family brand: no texture block, so the back-to-the-traveller line rides here
        assert name == "TERRA"
        assert rule == BRAND_FOCUS_HEAD + points + BRAND_FOCUS_USE + BRAND_FOCUS_TRAVELLER_HEAD + TRAVELLER_RETURN_LINE


@pytest.mark.parametrize("pack", [TERRA, TRAIL, ATLAS, WILD, PLAIN_GUIDE])
@pytest.mark.parametrize("channel", CHANNELS)
def test_traveller_line_once_in_every_prompt_with_focus_or_texture(pack, channel):
    p = _prompt(channel, pack)
    assert p.count(TRAVELLER_RETURN_LINE) == 1


@pytest.mark.parametrize("pack", [TERRA, TRAIL, ATLAS, WILD, PLAIN_GUIDE])
@pytest.mark.parametrize("channel", CHANNELS)
def test_focus_sits_last_among_the_brand_blocks_before_the_format(pack, channel):
    p = _prompt(channel, pack)
    rule = brand_focus_rule(pack)
    assert rule and p.count(rule) == 1 and p.count("BRAND FOCUS FOR THIS BRAND") == 1
    for before in (brand_texture_rule(pack, channel), family_presence_rule(pack, channel)):
        if before:
            assert p.index(before) + len(before) <= p.index(rule)
    tail = "BLOG-SPECIFIC FORMAT" if channel == "blog" else (
        "FORMAT FOR THIS CHANNEL" if channel in prompts.PLAIN_TEXT_CHANNELS else None)
    if tail:
        assert p.index(rule) + len(rule) <= p.index(tail)
    else:
        assert p.endswith(rule)


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_pack_without_a_style_guide_gets_no_focus(channel):
    for pack in ({}, {"brand_name": "X", "voice": ["Calm"]}):
        p = _prompt(channel, pack)
        assert "BRAND FOCUS" not in p and TRAVELLER_RETURN_LINE not in p


# ---------------------------------------------------------------- ANGLE BRIEF

def test_angle_brief_text():
    rule = ANGLE_BRIEF_RULE
    assert rule.startswith("ANGLE BRIEF: the SELECTED ANGLE lines above are an internal brief, not copy.")
    assert "never copy their phrasing into the piece" in rule
    assert "only the place names and facts they share with the content seed" in rule
    assert "leave out any detail, reassurance or framing they ask for that the content seed does not give" in rule
    assert "CONTENT SEED" not in rule  # the section label stays the first upper-case "CONTENT SEED" of the prompt


@pytest.mark.parametrize("pack", [TERRA, TRAIL, {}])
@pytest.mark.parametrize("channel", CHANNELS)
def test_angle_brief_follows_the_three_angle_lines(pack, channel):
    p = _prompt(channel, pack)
    lines = p.split("\n")
    k = lines.index(ANGLE_BRIEF_RULE)
    assert lines[k - 3].startswith("SELECTED ANGLE: ") and lines[k - 2].startswith("WHY THIS ANGLE WORKS: ")
    assert lines[k - 1].startswith("ANGLE'S BEST FINAL STYLE: ")
    assert lines[k + 1] == "" and lines[k + 2].startswith("CALL TO ACTION TO INCLUDE: ")


def test_angle_brief_does_not_move_the_inserted_lines():
    """destination / trip / scope / LEAD WITH go in with lines.insert(-2, …) and LENGTH AND SHAPE at index 5: the
    angle line sits before them all, so their places are unchanged."""
    p = prompts.build_user_prompt(content_seed="S", goal=GOAL, channel_style=get_channel_style("linkedin"),
                                  brand_audience=TERRA, angle=ANGLE, cta="Message us.", destination="Bhutan",
                                  trip_name="Trip", scope="SCOPE: one moment.", lead_focus="A fact.")
    order = [p.index(s) for s in ("LENGTH AND SHAPE", ANGLE_BRIEF_RULE, "CALL TO ACTION TO INCLUDE", "DESTINATION:",
                                  "TRIP:", "SCOPE:", "LEAD WITH THIS FACT", "CONTENT SEED (")]
    assert order == sorted(order)


# ---------------------------------------------------------------- contracts of the new text

NEW_TEXTS = (ANGLE_BRIEF_RULE, BRAND_FOCUS_HEAD, BRAND_FOCUS_USE, BRAND_FOCUS_TRAVELLER_HEAD, BRAND_TEXTURE_ATMOSPHERE,
             _ROUND7_BRIDGE)


@pytest.mark.parametrize("text", NEW_TEXTS)
def test_new_text_keeps_the_prompt_contracts(text):
    assert "—" not in text
    assert "{" not in text and "}" not in text  # appended raw, never .format()ed
    for word in ("genuinely", "precisely", "premium", "luxury"):
        assert word not in text.lower(), word
    assert not re.search(r"\bAA\b", text)
    for name in ("Trail Pulse", "Atlas", "Hearth", "WildKind", "Terra", "Kuang Si", "Bhutan", "Paro", "Thimphu"):
        assert name not in text, name


def test_the_full_prompts_carry_no_em_dash():
    for pack in PANEL.values():
        for channel in CHANNELS:
            assert "—" not in _prompt(channel, pack).replace(ANGLE["best_final_style"], "")


# ---------------------------------------------------------------- the stop-verdict residue (F15 stock line)

STOP_VERDICTS = [
    "As a government-run school in the capital, it is a structured, accessible stop with a clear purpose.",
    "It is an organised, purposeful stop.",
    "Being government-run, the institute is a structured, dependable stop in the city centre.",
    "Being government-run, the school is a structured, stable institution in the old town.",
    "As a government school, the institute is a structured, straightforward stop, and the arts are many.",
    "Being run by the state makes this a structured, accessible stop on day six of the trip.",
    "As a structured, accessible stop in the capital, it fits a day.",
    "The museum is a grounded, accessible stop where the training is visible.",
    "The school is centrally located, which makes it a calm, quiet part of the day.",
    "The school is a well-structured, accessible stop.",
    "The market remains a lively, welcoming part of the family day.",
    "The visit becomes a settled, accessible part of any itinerary.",
    # review fixes: contracted copulas, a hyphenated word that starts with a fact word, a praise pair
    "It's a structured, accessible stop.",
    "It’s a calm, easy stop.",
    "That's an organised, purposeful visit for the group.",
    "The school is a calm, free-flowing visit.",
    "It is a long-standing, respected institution.",
]


@pytest.mark.parametrize("sentence", STOP_VERDICTS)
def test_stop_verdicts_are_residue(sentence):
    hits = hygiene.template_lines(sentence)
    assert hits and hits[0][1] == sentence
    assert ai_tells.residue(sentence)  # the F15 stock-line list the pass hands on


FACTS = [
    "Kuang Si is a short, optional stop on the way back.",
    "It is a 30-minute, guided visit.",
    "The monastery is a steep, early stop before breakfast.",
    "The lodge is a small, family-run place by the river.",
    "This is a quiet, green valley.",
    "The ride is a long, full-day stage.",
    "The school is a government-run institution in the capital.",  # one modifier: a fact
    "Lunch is a simple, local meal.",  # not a trip unit noun
    "Paro is a final, overnight stop before the flight.",
    "The temple, a working, active monastery, is open daily.",  # no copula / 'as' before it
    "It is a free, private visit for the group.",
    # review fixes: the real trip nouns with plain seed modifiers (the verdict lexicon is closed)
    "The lodge is a small, family-run stop by the river.",
    "The market is a busy, open-air stop on day two.",
    "Punakha is a warm, low-lying stop after the pass.",
    "The weekend market is a covered, two-level stop.",
    "The hot spring is a natural, outdoor stop at the end of the trek.",
    "The museum is a large, modern institution in the capital.",
    "Day 3 is a relaxed, flexible part of the trip.",
    "It is a shorter, easier stop than the fort.",  # 'easier' is not 'easy'
    "It is a short, calm stop before lunch.",  # one verdict word, but the other is a fact
    "It's a free, welcoming visit.",
]


@pytest.mark.parametrize("sentence", FACTS)
def test_facts_are_not_residue(sentence):
    assert not hygiene.template_lines(sentence), sentence


def test_the_pass_leaves_a_stop_verdict_for_the_repair():
    """Residue, not a cut: rewrite never deletes it (the sentence carries facts), F15 routes it to the one-sentence
    repair on attempt 1."""
    text = ("The school trains painters in the capital.\n\n"
            "As a government-run school, it is a structured, accessible stop with a clear purpose.\n\n"
            "Message us.")
    ctx = TellContext(cta="Message us.", angle_name=None, source="The school trains painters in the capital.",
                      ignore=frozenset(), keep_words=frozenset(), angle_text="")
    out, _log = ai_tells.rewrite(text, ctx)
    assert "a structured, accessible stop" in out
    assert any("structured, accessible stop" in phrase for phrase, _ in ai_tells.residue(out))


def test_stop_verdict_reaches_f15_as_a_stock_line():
    """The existing F15_voice route (stock line, blocking on attempt 1 only, never a hold): no new gate."""
    text = "The school trains painters.\n\nIt is an organised, purposeful stop.\n\nMessage us."
    g = quality_gates.gate_voice(text, spec=None, stock_blocking=True)
    assert not g["passed"] and g["blocking"]
    assert any("organised, purposeful stop" in v for v in g["violations"])
    assert quality_gates.gate_voice(text, spec=None, stock_blocking=False)["blocking"] is False
