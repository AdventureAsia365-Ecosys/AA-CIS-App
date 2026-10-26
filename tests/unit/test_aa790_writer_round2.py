"""AA-790 round 2 (S251) — the forms the re-panel of the round-1 rewrite still found (AI-tells 5.12, bar 5.5; LinkedIn
source fidelity 8.92 → 7.08): invented visit content, 'do' denials and aphorism pairs, 'not incidental' verdicts,
verdict lead-ins, anaphora runs, internal labels, and the brand-energy line.

Every sentence here is synthetic (paraphrased shapes, other places), never a tenant's text. Each fix rule has
negative cases: practical facts, restrictions, objects a pronoun may point at, legitimate words.
"""
import re
import time

import pytest

from services.acp_content_writing import ai_tells, hygiene, prompts, service
from services.acp_content_writing.ai_tells import TellContext, brand_voice_rule, census, residue, rewrite
from services.acp_content_writing.quality_gates import gate_voice
from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import GOALS

CTX = TellContext(cta="Message us to start planning your trip.")
LEAD = "The old capital sits where two rivers meet."


def _blog(*paragraphs: str, heading: str = "## The River Valley") -> str:
    return "\n\n".join([LEAD, heading, *paragraphs, "The day ends at the riverside camp."])


def _run(text: str, ctx: TellContext = CTX) -> tuple[str, list[str]]:
    return rewrite(text, ctx)


# ---------------------------------------------------------------- fixed in code

@pytest.mark.parametrize("before, after", [
    # a 'do' denial with a personifying verb + its affirmation (the volcano / Mongolia shapes)
    # (round 3, S252: 'from the road' / 'from the ridge' is a denied place word, a fact the invariant keeps)
    ("The crater does not announce itself. It is simply there, tucked into the grass.",
     "The crater is simply there, tucked into the grass."),
    ("The shrines do not advertise their age. They are still repainted every spring.",
     "The shrines are still repainted every spring."),
    ("The valley doesn't announce its charm. It grows on the walk, field by field.",
     "The valley grows on the walk, field by field."),
    # a copula denial of a noun phrase + a short verb affirmation with an object
    ("The hall is not a relic. It hosts weddings.", "The hall hosts weddings."),
    # verdict lead-in before a colon: what follows is a sentence of its own
    ("The circle closes: the bridges on this route are repaired by students of the school.",
     "The bridges on this route are repaired by students of the school."),
    ("The thread that ties this section together is this: the river has fed these valleys for centuries.",
     "The river has fed these valleys for centuries."),
    # round-1 join keeps the affirmation's tense (review: it read 'The bridge is rebuilt.'); fourth review of round 3:
    # 'not the original' is a denied word, a fact, so the staged noun stands in for it here
    ("The bridge is not a replica. It was rebuilt.", "The bridge was rebuilt."),
    # three short sentences built the same way → one sentence (nothing cut, nothing added)
    ("The forts are still guarded. The shrines are still lit. The mills are still turning.",
     "The forts are still guarded, the shrines are still lit and the mills are still turning."),
])
def test_round2_form_is_rewritten(before, after):
    text, log = _run(_blog("The monks gather at dawn. " + before))
    assert text == _blog("The monks gather at dawn. " + after), text
    assert log and all(line.startswith("ai_tells: ") for line in log)


_COURTYARD = "Visitors walk the courtyard."


@pytest.mark.parametrize("cut, rest", [
    # aphorism pair, bare verb: both go while the piece still names the temple (fifth review of round 3: the
    # subject is a fact by presence, fact_invariant tests keep 'The mill is not a ruin. It grinds.' when it is not)
    ("The temple is not a ruin. It works.", "Visitors walk the temple courtyard."),
    # lone 'do' denial (round 3, S252: the village is named again, so the fact invariant lets the cut go)
    ("This kind of visit does not present the village as a stage.", "Visitors walk the village courtyard."),
    ("The order of the stops is not incidental.", _COURTYARD),           # 'not incidental' verdict
    ("The overlap of these cultures in this province is not accidental.", _COURTYARD),
    ("The continuity is deliberate.", _COURTYARD),
    ("Such timing is no coincidence.", _COURTYARD),
])
def test_round2_verdict_or_aphorism_is_cut(cut, rest):
    para = f"The monks gather at dawn. {cut} {rest}"
    text, log = _run(_blog(para))
    assert f"The monks gather at dawn. {rest}" in text, text
    assert len(log) == 1


def test_the_pass_is_idempotent_on_round2_forms():
    text = _blog("The monks gather at dawn. The crater does not announce itself. It is simply there.",
                 "The forts are still guarded. The shrines are still lit. The mills are still turning. Then the "
                 "road climbs.",
                 "The bus stops at noon. The order of the stops is not incidental. Lunch is at the camp.")
    once, _ = _run(text)
    assert census(text, CTX)["fixable_total"] >= 3
    assert census(once, CTX)["fixable_total"] == 0
    assert _run(once) == (once, [])


@pytest.mark.parametrize("kept, fact", [
    # round 3 (S252): the fact-preservation invariant keeps what a round-2 proposer would have dropped
    ("The crater does not announce itself from the road. It is simply there, tucked into the grass.", "road"),
    # fourth review of round 3: a denied word is a fact by default
    ("The crater does not announce itself from the ridge. It is simply there, tucked into the grass.", "ridge"),
    ("The bridge is not the original. It was rebuilt.", "original"),
    ("This kind of visit does not present the village as a stage.", "village"),      # the only mention
    ("This kind of stay does not position the village as a backdrop.", "village"),
    ("The overlap of the two cultures in this province is not accidental.", "two"),  # a number
])
def test_round2_proposals_the_fact_invariant_refuses(kept, fact):
    text = _blog(f"The monks gather at dawn. {kept} Visitors walk the courtyard.")
    out, log = _run(text)
    assert out == text
    assert len(log) == 1 and log[0].startswith("ai_tells: skipped ") and f"drops a fact: {fact}" in log[0]


# ---------------------------------------------------------------- left alone (precision first)

@pytest.mark.parametrize("clean", [
    # restrictions and logistics are facts: not a personifying verb, or the same verb twice
    "The ferry does not run on Sundays. It runs daily in summer.",
    "The bus does not stop at the falls. It continues to the village.",
    "The herders do not live in fixed villages. They move camp with the seasons.",
    "It does not open until May. It closes again in October.",
    "This does not include lunch.",
    # an object the next 'It' may point at: 'The guide is explained …' would be wrong
    "The guide does not explain the ritual. It is explained by the monks.",
    # 'it' in the affirmation points back at what the denial named
    "The shrine does not announce itself from the valley. It sits inside it.",
    # a practical word in the denied part
    "The path does not reveal how steep it is. It is a long climb.",
    # a limit in the denied part is a fact
    "The lake does not reveal itself until the last bend. It is hidden by the ridge.",
    # 'This means …' / 'That is …' point at the statement, not at the subject
    "The hall is not a temple. This means shoes stay on.",
    "The town does not hurry. This is a market town with two banks.",
    # copula + adjective, or a longer affirmation: the round-1 rules decide
    "The road is not paved. It climbs to the pass.",
    # 'not incidental to …' says what it relates to; practical words; a digit
    "The timing is not incidental to the festival calendar.",
    "The difficulty of the climb is not incidental.",
    "The route was deliberate about rest days.",
    # lead-ins: no meta word in the subject, or nothing after the colon that is a sentence
    "The rule at the temple is this: shoes stay outside the door.",
    "The circle closes: a short walk back to the gate.",
    # parallel sentences that are not a run: two only, different verbs, a digit
    "Each day is long. Each day is full.",
    "The bus leaves early. The ride is short. The road is paved.",
    "The pools are 3 metres deep. The pools are 2 metres wide. The pools are 1 metre apart.",
])
def test_round2_clean_text_unchanged(clean):
    text = _blog(f"The monks gather at dawn. {clean}")
    assert _run(text) == (text, [])


def test_a_bare_affirmation_the_seed_states_is_kept():
    text = _blog("The monks gather at dawn. The river is not a stream. It floods. Visitors walk the courtyard.")
    ctx = TellContext(cta=CTX.cta, source="Source itinerary: the river floods in July.")
    assert _run(text, ctx) == (text, [])


def test_a_denial_the_seed_states_is_kept():
    text = _blog("The monks gather at dawn. The festival does not advertise itself to outsiders. It is held for the "
                 "valley.")
    ctx = TellContext(cta=CTX.cta, source="Source itinerary: the festival does not advertise itself to outsiders.")
    assert _run(text, ctx) == (text, [])


def test_the_first_sentence_and_a_lone_section_sentence_are_never_cut():
    first = "The valley does not announce its charm. It works.\n\n## Day 1\n\nThe ride starts in town. It is short."
    assert _run(first) == (first, [])
    alone = f"{LEAD}\n\n## The Temple\n\nThe temple is not a ruin. It works.\n\n## The Camp\n\nThe day ends there."
    assert _run(alone) == (alone, [])  # cutting both would empty the section (its heading would go too)
    run = "The forts are still guarded. The shrines are still lit. The mills are still turning.\n\n## Day 1\n\nA walk."
    assert _run(run)[0] == run  # an anaphora run that opens the piece is its hook


# ---------------------------------------------------------------- residue: the existing F15 stock-line repair

@pytest.mark.parametrize("sentence", [
    "A carved mask in this valley is rarely just a mask.",
    "Day 3 is where the trip properly opens up.",
    "The waterfall is where the ride gets serious.",
    "The gorge is where most river trips find their footing.",
    "That is the shape of the valley on two wheels.",
    "That one fact is the key to understanding the valley.",
    "The river is the reason each of these towns is what it is.",
    "Those numbers are worth holding as you stand on the shore.",
    "The museum deserves its own attention.",
    "This matters as evidence.",
    "The age of the crater matters here too.",
    "The hedge in that last sentence matters for families.",
    "The word \"might\" is honest here.",
    "This is the argument that the old fort makes.",
    "The craft persists because the demand persists.",
    "That moment tends to reframe everything that follows.",
    "Each day builds on the last as the route climbs north.",
    "Each stage adds a layer, and no two days look alike.",
    "It happens when it happens, and nobody minds.",
    "The valley does not announce its beauty from the road.",
    "The circle closes with the school that trains the painters.",
    "The thread running through this part of the journey is this: a river and its towns.",
    # internal labels: the writer's inputs named in the copy
    "The brief describes a quiet stop by the river.",
    "Our chosen angle is the river and its towns.",
    "The angle here asks for a slower pace.",
    "This atom covers the morning walk to the shrine.",
    "Each seed fact appears once in this post.",
    # invented visit content and logistics
    "The school fits naturally into a family day in the capital.",
    "A visit puts your family in the middle of the training.",
    "For families it is a chance to watch the painters at work.",
    "The stop is an easy-to-navigate part of the day.",
    "The care is visible the moment you walk in.",
])
def test_round2_residue_goes_to_the_f15_stock_line_repair(sentence):
    text = _blog(sentence)
    assert residue(text), sentence
    result = gate_voice(text, spec=None, stock_blocking=True)
    assert any(v.startswith("stock line '") for v in result["violations"]) and result["blocking"]


@pytest.mark.parametrize("sentence", [
    # farming seed, science atoms, a brief stop, an angle of light: never internal labels
    "The farmers save seed from the best plants each year.",
    "Sesame seed oil is pressed in the village every autumn.",
    "Carbon atoms in the limestone bind with calcium over time.",
    "The peace park remembers the atom bomb and its victims.",
    "Splitting the atom changed the history of this town.",
    "The brief stop at the market is optional for the group.",
    "Lunch is quick, according to the brief schedule for the day.",
    "From this angle, the monastery seems to hang from the cliff.",
    "The angle of the slope is gentle for the first hour.",
    # facts that look like verdicts
    "Paro is where the trek begins on the second day.",
    "This is the heart of the old town and its market.",
    "The shuttle is never just a few minutes late in winter.",
    "The monastery matters to the whole valley as a seat of learning.",
    "The word 'dzong' means fortress in the local language.",
    "The first sentence of the prayer is carved over the door.",
    "The ferry runs because the river runs high in spring.",
    "The museum is easy to navigate with a stroller.",
    "Families have a chance to see the takin in the preserve.",
    "The camp fits well within the valley floor below the pass.",
    "The talk happens every evening when it is dry.",
    "The rule at the temple is this: shoes stay outside the door.",
    "The atom at the centre of the science exhibit is carbon.",
])
def test_round2_residue_leaves_legitimate_sentences_alone(sentence):
    assert residue(_blog(sentence)) == [], sentence


# ---------------------------------------------------------------- the brief

GOAL = GOALS[0]
FAMILY = {"brand_name": "Terra Family Expeditions", "customer_segment": "Families with children aged 7-16",
          "voice": ["Warm", "Reassuring", "Family-aware"]}
ENERGETIC = {"brand_name": "Trail Pulse", "voice": ["Energetic", "Fresh", "Lightly playful"]}
CALM = {"brand_name": "WildKind Travel", "voice": ["Thoughtful", "Grounded", "Respectful", "Calm"]}
ANGLE = {"name": "The Craft School", "why_it_works": "w", "formula_fit": "f", "best_final_style": "s"}


def _prompt(channel: str, pack: dict = FAMILY, **kw) -> str:
    return prompts.build_user_prompt(content_seed="Craft school\nSource itinerary: A visit.", goal=GOAL,
                                     channel_style=get_channel_style(channel), brand_audience=pack, angle=ANGLE,
                                     cta="Message us.", **kw)


def test_audience_rule_keeps_the_audience_and_forbids_inventing_the_visit():
    rule = " ".join(ai_tells.SOCIAL_AUDIENCE_RULE.split())
    assert "Say what they see or do" not in rule  # the round-1 wording that made the writer invent the visit
    assert "Name who they are" in rule and "a family brand writes to the family" in rule
    assert "Never describe what the visit shows, includes" in rule and "unless the CONTENT SEED says so" in rule
    assert ai_tells.SOCIAL_AUDIENCE_RULE in _prompt("linkedin")
    assert ai_tells.SOCIAL_AUDIENCE_RULE not in _prompt("blog", atom_id="a1")


def test_every_channel_gets_the_visit_detail_guard_through_the_system_prompt():
    assert ai_tells.VISIT_DETAIL_RULE in prompts.SYSTEM_PROMPT  # blog included: one system prompt for T9
    assert "unless the content seed states it" in " ".join(ai_tells.VISIT_DETAIL_RULE.split())


def test_system_prompt_names_the_internal_labels_and_meta_commentary():
    rules = " ".join(ai_tells.PROMPT_RULES.split())
    assert "seed, content seed, angle, brief and atom" in rules
    assert "Never comment on your" in rules and "is where the trip gets going" in rules


def test_writer_prompt_version_moves_off_round_one():
    assert service.WRITER_PROMPT_VERSION not in ("662d4aa5", "3c078692")  # pilot, round 1


def test_brand_voice_rule_per_energy():
    assert "PACE FOR THIS BRAND" in brand_voice_rule(ENERGETIC) and "Lightly playful" in brand_voice_rule(ENERGETIC)
    assert "RESTRAINT FOR THIS BRAND" in brand_voice_rule(CALM) and "PACE" not in brand_voice_rule(CALM)
    assert brand_voice_rule(FAMILY) == "" and brand_voice_rule({}) == "" and brand_voice_rule(None) == ""
    both = brand_voice_rule({"voice": ["Energetic", "Calm"]})
    assert "PACE" in both and "RESTRAINT" in both


@pytest.mark.parametrize("channel", ["linkedin", "blog", "facebook"])
def test_brand_voice_rule_reaches_every_channel(channel):
    kw = {"atom_id": "a1"} if channel == "blog" else {}
    assert brand_voice_rule(ENERGETIC) in _prompt(channel, ENERGETIC, **kw)
    assert brand_voice_rule(CALM) in _prompt(channel, CALM, **kw)
    assert "PACE FOR THIS BRAND" not in _prompt(channel, FAMILY, **kw)


# ---------------------------------------------------------------- round-2 review (S252): blocking findings + nits

def _mid(sentences: str) -> str:
    """The sentences mid-section, with prose before and after (never the first sentence, never a lone one)."""
    return _blog(f"The monks gather at dawn. {sentences} Visitors walk the courtyard.")


@pytest.mark.parametrize("clean", [
    # a lone 'do' denial with a direct object or an audience is a practical fact (review: cut as 'lone denial')
    "This shop does not sell alcohol.",
    "These stalls do not sell souvenirs.",
    "They do not sell to tourists.",
    "It does not perform for tourists.",
    # a season or an audience after a preposition is a restriction the join would drop
    "The dancers do not perform in the rainy season. They train in the monastery.",
    "The troupe does not perform in winter. It tours abroad.",
    "The workshop does not sell to visitors. It supplies the temples.",
    # an informative lead-in subject: 'only', a superlative, or a plain noun (way, place, route, day)
    "The only way in is this: the road from the coast is closed for half the year.",
    "The best place to stay is this: the guesthouse by the lake has six rooms and a garden.",
    "The plan for the day is this: the group is driven to the lake and back.",
    # 'not only X' asserts X
    "The hall is not only a temple. It hosts weddings.",
    # a name or a schedule phrase in a 'deliberate' sentence is pace / acclimatisation information
    "The rest day in Leh is deliberate.",
    "The slow pace of the first two days is deliberate.",
    # the same verb twice (-es stem): a restriction, not a set-up
    "The hamlet does not hide itself. It hides behind the ridge.",
])
def test_review_r2_facts_are_left_alone(clean):
    text = _mid(clean)
    assert _run(text) == (text, [])


@pytest.mark.parametrize("before, after", [
    # the reflexive form of a transactional verb is still a tell
    ("The village does not sell itself. It is a farming village with two temples.",
     "The village is a farming village with two temples."),
    ("The crater does not shout about itself. It is simply there, tucked into the grass.",
     "The crater is simply there, tucked into the grass."),
    # a piece-meta subject still makes a lead-in
    ("The opening of this section is this: the river has fed these valleys for centuries.",
     "The river has fed these valleys for centuries."),
    ("The thread of this part of the trip is this: the monasteries were built along the salt road.",
     "The monasteries were built along the salt road."),
    # a run of four mid-piece is joined whole
    ("The forts are still guarded. The shrines are still lit. The mills are still turning. The wells are still full.",
     "The forts are still guarded, the shrines are still lit, the mills are still turning and the wells are still "
     "full."),
])
def test_review_r2_tells_are_still_rewritten(before, after):
    text, log = _run(_mid(before))
    assert text == _mid(after), text
    assert len(log) == 1


@pytest.mark.parametrize("cut, rest", [
    ("It does not sell itself.", "Visitors walk the courtyard."),
    # round 3 (S252): the fact invariant lets a cut go only while the piece still names the village and no number goes
    ("This kind of stay does not position the village as a backdrop.", "Visitors walk the village courtyard."),
    ("This kind of visit does not present the village as a stage.", "Visitors walk the village courtyard."),
    ("The overlap of these cultures in this province is not accidental.", "Visitors walk the courtyard."),
])
def test_review_r2_lone_tells_are_still_cut(cut, rest):
    text, log = _run(_blog(f"The monks gather at dawn. {cut} {rest}"))
    assert text == _blog(f"The monks gather at dawn. {rest}"), text
    assert len(log) == 1


def test_review_r2_lemmas_keep_verbs_that_end_in_e():
    assert {"hide", "stage", "frame", "announce", "close"} == {
        v for w in ("hides", "stages", "frames", "announces", "closes") for v in ai_tells._lemmas(w)
        if v in {"hide", "stage", "frame", "announce", "close"}}
    assert "rush" in ai_tells._lemmas("rushes") and "carry" in ai_tells._lemmas("carries")
    assert ai_tells._lemmas("pass") == frozenset({"pass"})  # never 'pas'


def test_review_r2_an_anaphora_run_that_opens_the_piece_is_left_whole():
    run = ("The forts are still guarded. The shrines are still lit. The mills are still turning. The wells are still "
           "full.\n\n## Day 1\n\nA walk in the hills today.")
    assert _run(run) == (run, [])


def test_review_r2_brand_voice_rule_skips_negated_words():
    calm_only = brand_voice_rule({"voice": ["Calm, never playful"]})
    assert "RESTRAINT FOR THIS BRAND (its voice: Calm, never playful)" in calm_only and "PACE" not in calm_only
    assert brand_voice_rule({"voice": ["Not loud, not bold"]}) == ""
    assert brand_voice_rule({"voice": ["Not calm", "No nonsense"]}) == ""
    assert "PACE FOR THIS BRAND (its voice: Bold)" in brand_voice_rule({"voice": ["No-nonsense", "Bold"]})
    assert "PACE FOR THIS BRAND (its voice: Lightly playful)" in brand_voice_rule({"voice": ["Lightly playful"]})


@pytest.mark.parametrize("sentence", [
    "Kathmandu is where the trip finally ends.",
    "The route is this: Hanoi, Ninh Binh, Hue and Hoi An.",
    "The plan for the day is this: a boat to the caves and lunch on board.",
])
def test_review_r2_residue_false_positives_are_gone(sentence):
    assert residue(_blog(sentence)) == [], sentence


@pytest.mark.parametrize("sentence", [
    "Day 3 is where the trip properly opens up.",
    "The thread running through this part of the journey is this: a river and its towns.",
    "This matters as evidence.",
    "The age of the crater matters here too.",
])
def test_review_r2_residue_still_catches(sentence):
    assert residue(_blog(sentence)), sentence


def test_review_r2_matters_pattern_keeps_its_hits():
    rx = next(r for r in hygiene.AI_TELL_LINE_RES if "matters?" in r.pattern)
    assert rx.search("This matters as evidence.") and rx.search("The age of the crater matters here too.")
    assert not rx.search("The monastery matters to the whole valley as a seat of learning.")


def test_review_r2_prompt_bounds_reader_action_to_the_seed():
    rules = " ".join(ai_tells.PROMPT_RULES.split())
    assert ("Every sentence carries a fact from the content seed, a place, or what the content seed says the reader "
            "does there.") in rules
    assert "or what the reader does there" not in rules
    system = " ".join(prompts.SYSTEM_PROMPT.split())
    assert ("SOURCE FIDELITY and VISIT DETAIL override the brand's style guide and its examples: what the traveller "
            "does, feels or discovers comes only from the content seed. The style guide sets tone and sentence shape, "
            "not content.") in system
    assert '"you" (only what the CONTENT SEED states)' in " ".join(brand_voice_rule(ENERGETIC).split())


def test_review_r2_writer_prompt_version():
    # round 2 + review was afc08095 (round 2 as pushed: 751b0a34); round 3 (S252) moved it on, see
    # test_aa790_writer_round3.test_writer_prompt_version_moves_off_round_two
    assert service.WRITER_PROMPT_VERSION not in ("751b0a34", "afc08095")


# ---------------------------------------------------------------- linear time (CodeQL py/polynomial-redos)

_N = 8000
_ADVERSARIAL = {
    "spaces": " " * _N,
    "space-tab": " \t" * (_N // 2),
    "The+spaces": "The" + " " * _N + "x",
    "This+spaces": "This" + " " * _N,
    "words": "The " + "word " * (_N // 5),
    "is-not": "It is not " + "a " * (_N // 2) + "x",
    "does-not": "It does not " + "x " * (_N // 2),
    "lead-in": "The " + "a " * (_N // 2) + "is this: x",
    "is-where": "is where the " + "a " * (_N // 2),
    "itself": " " + "itself " * (_N // 7),
    "framing": " the " + "a " * (_N // 2) + "as",
    "letters": "a" * _N,
}


def _round2_patterns():
    """Every regex this PR adds or changes: the ai_tells round-2 patterns and the round-2 residue entries (all
    AI_TELL_LINE_RES after the round-1 'asks … of' line)."""
    names = ("_LEAD_IN_RE", "_FINITE_RE", "_DO_DENIAL_RE", "_AFFIRM_VERB_RE", "_DO_X_OK_RE", "_DO_X_REFLEXIVE_RE",
             "_FRAMING_AS_RE", "_LIMIT_RE", "_LONE_DO_SUBJ_RE", "_MEANT_RE", "_NOT_ONLY_RE", "_SCHEDULE_RE",
             "_PART_OF_RE", "_VOICE_WORD_RE")
    out = [(n, getattr(ai_tells, n)) for n in names] + [("_CONTRACTIONS[do]", ai_tells._CONTRACTIONS[3][0])]
    start = next(i for i, r in enumerate(hygiene.AI_TELL_LINE_RES) if r.pattern.startswith(r"\basks\s+")) + 1
    out += [(f"AI_TELL_LINE_RES[{i}]", r) for i, r in enumerate(hygiene.AI_TELL_LINE_RES) if i >= start]
    assert all(isinstance(p, re.Pattern) for _, p in out) and len(out) > 30
    return out


@pytest.mark.parametrize("name, rx", _round2_patterns(), ids=lambda v: v if isinstance(v, str) else "")
def test_review_r2_regexes_run_in_linear_time(name, rx):
    """8000-char whitespace / space-tab / word runs: each search + match under 50 ms (the old 'X matters.' pattern
    took 12 s on 'The' + 8000 spaces)."""
    for label, s in _ADVERSARIAL.items():
        best = min(_timed(rx, s) for _ in range(2))
        # 5x CI slack: runners are 2-3x slower; a quadratic regex takes seconds at 8000 chars
        assert best < 0.25, f"{name} on {label}: {best * 1000:.0f} ms"


def _timed(rx: re.Pattern, s: str) -> float:
    t = time.perf_counter()
    rx.search(s)
    rx.match(s)
    return time.perf_counter() - t
