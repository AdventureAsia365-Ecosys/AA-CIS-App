"""AA-790 round 3 (S252) — the forms the blind 3-arm panel still quoted on the round-2 rewrite (AI-tells 5.33, bar
5.5; LinkedIn source fidelity 7.08 vs pilot 8.83): denial / contrast frames the round-2 pass refused or did not see,
'something rarer than X:' build-ups, 'That single fact changes how you think …' verdicts, brief words used as a
heading, more residue for the F15 stock-line repair, and two seed-aware findings (an age band, a founding reason)
on the existing F15 route. Plus the SOURCE FIDELITY prompt rules (AGES, CAUSE AND SCOPE, 'feels like').

Every sentence here is synthetic (paraphrased shapes, other places), never a tenant's text. Every rule has negative
cases: practical facts, restrictions, 'not only' assertions, logistics, seed-stated facts.
"""
import re
import time

import pytest

from services.acp_content_writing import ai_tells, hygiene, prompts, service
from services.acp_content_writing.ai_tells import TellContext, brand_voice_rule, residue, rewrite
from services.acp_content_writing.quality_gates import UNSUPPORTED_DETAIL_PREFIX, gate_voice

CTX = TellContext(cta="Message us to start planning your trip.")
LEAD = "The old capital sits where two rivers meet."


def _blog(*paragraphs: str, heading: str = "## The River Valley") -> str:
    return "\n\n".join([LEAD, heading, *paragraphs, "The day ends at the riverside camp."])


# ---------------------------------------------------------------- fixed in code: pairs

@pytest.mark.parametrize("before, after", [
    # a demonstrative copula denial + an 'It' affirmation: joined on the denial's own subject (round 2 cut the
    # denial and the integrity check refused the paragraph opening on 'It is …')
    ("This is not a landscaped garden. It is a place where the frontier war left a mark on the valley.",
     "This is a place where the frontier war left a mark on the valley."),
    ("These are not museum notes. They are part of what the village still argues about.",
     "These are part of what the village still argues about."),
    # a demonstrative copula denial + an 'It' verb affirmation of any length
    ("This is not a single sudden view. It builds across three days, each stop adding a new layer of history.",
     "This builds across three days, each stop adding a new layer of history."),
])
def test_demonstrative_denial_is_joined_at_a_paragraph_start(before, after):
    text, log = rewrite(_blog(before), CTX)
    assert text == _blog(after), text
    assert any("joined" in line for line in log)


def test_relocated_narrative_verb_is_joined_even_as_the_hook():
    """'X's story does not begin in A. It begins in B, …' → one sentence; a join keeps the first sentence (hook).
    Fourth review of round 3: A is a name (the panel's 'does not begin in Thimphu'); a common noun ('the capital')
    is a denied word, a fact by default, and the join is refused (test_aa790_fact_invariant)."""
    hook = ("The valley's story does not begin in Thimphu. It begins in the south, where rice terraces climb "
            "from the river.")
    text, log = rewrite(hook + "\n\n## The South\n\nThe road south from Thimphu follows the river for a day.", CTX)
    assert text.startswith("The valley's story begins in the south, where rice terraces climb from the river.\n")
    assert log == ["ai_tells: denial + relocation joined: '" + hook + "'"]


@pytest.mark.parametrize("pair", [
    # logistics: a trek, a ferry, a bus is never a narrative subject
    "The trek does not begin in the capital. It begins in the south, at the trailhead by the bridge.",
    "The ferry does not stop in the old town. It stops at the pier below the market.",
    # a different preposition / a different verb is not a relocation
    "The valley's story does not begin in the capital. It begins with the river that cut the gorge.",
    "The valley's story does not begin in the capital. It ends in the south, by the lake.",
    # a limit word or a digit in the denied part
    "The valley's story does not begin until the harvest. It begins in the south after the rains.",
])
def test_relocation_leaves_logistics_and_limits_alone(pair):
    text = _blog("The monks gather at dawn. " + pair)
    assert rewrite(text, CTX)[0] == text


def test_relocation_keeps_a_contrast_the_seed_states():
    ctx = TellContext(cta=CTX.cta, source="The festival does not begin in the capital; it begins in the east.")
    pair = "The festival's story does not begin in the capital. It begins in the east, in the villages."
    text = _blog("The monks gather at dawn. " + pair)
    assert rewrite(text, ctx)[0] == text


@pytest.mark.parametrize("pair", [
    # 'not simply X' asserts X: never folded into a join (round-1 _pair still proposes its cut, refused here)
    "This is not simply a lake. It is a place where two forests meet.",
    # practical / rule words, a digit, a name
    "This is not a difficult walk. It is a path along the river bank.",
    "This is not a free visit. It is a ticketed tour of the hall.",
    "This is not the 1950 bridge. It is a copy built on the old piers.",
])
def test_demonstrative_join_negatives(pair):
    text = _blog(pair)
    assert rewrite(text, CTX)[0] == text


def test_same_pronoun_pair_keeps_the_round_one_cut():
    text = _blog("The monks gather at dawn. It is not a museum. It is a working monastery.")
    out, log = rewrite(text, CTX)
    assert out == _blog("The monks gather at dawn. It is a working monastery.")
    assert log == ["ai_tells: denial before its affirmation: 'It is not a museum.'"]


def test_a_passive_denial_is_a_fact_and_is_never_cut():
    """S252 measure: the round-1 lone-denial cut dropped 'They were not saved.' (monasteries lost in a purge)."""
    text = _blog("Most of the old monasteries were burned in the purges. They were not saved. "
                 "Only a few walls stand today.")
    assert rewrite(text, CTX)[0] == text


# ---------------------------------------------------------------- fixed in code: one sentence

@pytest.mark.parametrize("before, after", [
    # 'not A, but B' after a colon, B ends the sentence
    ("The monastery is the clearest example of what a dzong is here: not a museum piece, but a working seat.",
     "The monastery is the clearest example of what a dzong is here: a working seat."),
    # 'not as A, but as B' (same preposition) with a comma
    ("The school was founded so the skills reach new students, not as museum objects, but as living practice.",
     "The school was founded so the skills reach new students, as living practice."),
    # staging contrast tails
    ("The contact with the herders is direct rather than staged.", "The contact with the herders is direct."),
    ("The blue colour comes from dissolved limestone rather than by any trick of the light.",
     "The blue colour comes from dissolved limestone."),
    ("The mix of peoples in the province is a long-standing condition rather than an incidental detail.",
     "The mix of peoples in the province is a long-standing condition."),
    ("The brand plans trips for people who want to move through a valley, not just pass through it.",
     "The brand plans trips for people who want to move through a valley."),
    ("The brand plans journeys that give children real access to the towns, not just a view of them.",
     "The brand plans journeys that give children real access to the towns."),
    # the rule word sits in the main clause, not in the tail: the tail still goes
    ("Visits to the camps are included, and the contact with the herders is direct rather than staged.",
     "Visits to the camps are included, and the contact with the herders is direct."),
    # 'something … :' build-ups
    ("The camps offer something rarer than scenery: a way of life built around the herds.",
     "The camps offer a way of life built around the herds."),
    ("The elder's talk offers something that no guidebook can substitute: a first-hand account of the village.",
     "The elder's talk offers a first-hand account of the village."),
    ("The museum offers something specific: the chance to read the cloth as a record of rank.",
     "The museum offers the chance to read the cloth as a record of rank."),
])
def test_round3_sentence_form_is_rewritten(before, after):
    text, log = rewrite(_blog("The monks gather at dawn. " + before), CTX)
    assert text == _blog("The monks gather at dawn. " + after), text
    assert log and all(line.startswith("ai_tells: ") for line in log)


@pytest.mark.parametrize("clean", [
    # 'rather than' with a fact on both sides
    "Lunch is at a farmhouse rather than a restaurant.",
    "The masks are worn in the dance rather than kept on display.",
    "The group crosses by boat rather than by the old bridge.",
    # no comma before a bare 'not': the cut would leave 'The dance is.'
    "The dance in the courtyard is not staged.",
    # 'not just X' with a fact X stays ('not only X' asserts X)
    "The temple is a working monastery, not just a museum.",
    "The lake is open for swimming, not just viewing.",
    "The festival is free, not just a show for visitors.",
    # a practical word in the tail
    "The walk is steady rather than a steep scramble.",
    # 'not A, but B' where B opens a clause, or B does not end the sentence
    "The hall is not a museum, but the monks welcome visitors every morning.",
    "The walk is not long, but the path is steep.",
    "The pass is crossed not on foot, but by jeep when the snow allows it.",
    # a colon frame whose X is a practical word
    "The rule at the gate is simple: not cheap, but fair to everyone.",
    # 'something' with no build-up word, or no verb before it
    "The museum offers something for every age: a quiz trail and a garden.",
    "There is something specific: the gate closes at five.",
    # verdict-like 'changes how' about a fact, not about the reader's view
    "The monks gather at dawn. That fact changes how the festival is scheduled each year.",
    "The location is not incidental to its history as a trading post.",
])
def test_round3_clean_text_unchanged(clean):
    text = _blog("The monks gather at dawn. " + clean)
    assert rewrite(text, CTX)[0] == text


@pytest.mark.parametrize("cut", [
    "That single fact changes how you think about the country.",
    "This detail alone reshapes what a visit means.",
    "The position is not incidental to its significance.",
])
def test_round3_verdict_is_cut(cut):
    first = "The lake holds most of the country's fresh water."
    text, log = rewrite(_blog(f"{first} {cut} The shore is lined with larch."), CTX)
    assert text == _blog(f"{first} The shore is lined with larch.")
    assert log == [f"ai_tells: verdict line: '{cut}'"]


def test_a_verdict_that_is_the_first_sentence_is_never_cut():
    text = "That single fact changes how you think about the country.\n\n## The Lake\n\nThe lake is deep."
    assert rewrite(text, CTX)[0] == text


# ---------------------------------------------------------------- headings lifted from the brief

BRIEF = TellContext(cta=CTX.cta, angle_name="Where Two Worlds Meet",
                    angle_text="Where Two Worlds Meet. Structure the piece around the idea of convergence as the "
                               "organising logic for this section.",
                    source="Lake Khovsgol sits in northern Mongolia at the edge of the taiga. A dormant volcano.")


@pytest.mark.parametrize("heading, after", [
    ("## Northern Mongolia and the Logic of Convergence", "## Northern Mongolia"),
    ("## Lake Khovsgol: The Logic of Convergence", "## Lake Khovsgol"),
])
def test_brief_words_heading_part_is_dropped(heading, after):
    text, log = rewrite(_blog("The lake is deep and cold.", "The taiga starts at the shore.", heading=heading), BRIEF)
    assert text == _blog("The lake is deep and cold.", "The taiga starts at the shore.", heading=after)
    assert log == [f"ai_tells: brief words as heading: '{heading[3:]}'"]


@pytest.mark.parametrize("heading", [
    "## Uran Togoo: A Dormant Volcano",           # seed words
    "## The Logic of Convergence",                # the whole heading: never emptied
    "## Northern Mongolia and the Taiga Edge",    # 'taiga' / 'edge' are the seed's
    "## Kurje Lhakhang and Jambay Lhakhang",      # no 'and the' split, names
])
def test_brief_heading_negatives(heading):
    text = _blog("The lake is deep and cold.", "The taiga starts at the shore.", heading=heading)
    assert rewrite(text, BRIEF)[0] == text


def test_angle_text_helper_and_service_wiring():
    angle = {"name": "N", "why_it_works": "W", "formula_fit": "F", "best_final_style": "B", "other": "x"}
    assert ai_tells.angle_text(angle) == "N W F B"
    assert ai_tells.angle_text(None) == ""
    import inspect
    assert "angle_text=ai_tells.angle_text(chosen)" in inspect.getsource(service.run_write_background)


def test_the_pass_is_idempotent_on_round3_forms():
    text = _blog("The monks gather at dawn. The camps offer something rarer than scenery: a way of life.",
                 "This is not a landscaped garden. It is a place where the frontier war left a mark.",
                 "The colour comes from the limestone rather than by any trick of the light.",
                 heading="## Northern Mongolia and the Logic of Convergence")
    once, _ = rewrite(text, BRIEF)
    assert once != text and rewrite(once, BRIEF) == (once, [])


def test_census_reports_v3():
    assert ai_tells.census("A plain sentence about the river.", CTX)["version"] == "ai_tells.v3"


# ---------------------------------------------------------------- residue (F15 stock line → one cheap repair)

@pytest.mark.parametrize("sentence", [
    "After a long ride, that's a detail worth knowing.",
    'The word "may" is worth noting: the elder does not always come.',
    "That gap between the two calendars is worth pausing on.",
    "Those numbers matter less than what they produce.",
    "This is the section of the journey that rewards the greatest attention.",
    "This is the section worth the most of your attention on the second day.",
    "The falls are where the route begins to reveal its logic.",
    "The lake earns its place as the anchor of this route.",
    "The old name means tiger's den, and the place earns both names.",
    "The climb makes the swim feel earned in the best way.",
    "The trip begins the way most good journeys should: slowly.",
    "It asks something of a young mind in a way that a photograph does not.",
    "Children respond to the workshop in a way a market stall cannot replicate.",
    "Children learn something here that no classroom exercise replicates.",
    "These crafts supply the dzongs, so the training carries real weight.",
    "The connection is something that stays with a child long after the day is over.",
    "Standing in the crater resets a family's sense of scale.",
    "The geography is the lesson, and every stop builds on it.",
    "Each step adds a layer to the same story told by the river.",
    "Their presence here is a product of it.",
    "The difficulty of the climb is part of what the site means.",
    "The school sends its graduates to the temples, closing the circle.",
    "For families travelling with children, that context matters.",
    "For families, that context changes what a visit means.",
    "There is no performance in this, no arranged demonstration.",
])
def test_round3_residue_goes_to_the_f15_stock_line_repair(sentence):
    text = f"The camp sits by the lake. {sentence} The road north is long."
    assert residue(text), sentence
    g = gate_voice(text, spec=None, stock_blocking=True)
    assert not g["passed"] and any(v.startswith("stock line '") and sentence in v for v in g["violations"])


@pytest.mark.parametrize("sentence", [
    "The detail worth knowing is that the gate closes at five.",
    "Each porter carries weight of up to fifteen kilos.",
    "The mules carry weight up the pass every morning.",
    "The trek closes the loop around the lake on the last day.",
    "This is the section that crosses the high pass.",
    "The town earned both names from its two rivers.",
    "The road climbs in a way that a jeep can handle easily.",
    "The village keeps a recipe that no other valley uses.",
    "That detail matters for the permit application.",
    "The first workshop is the lesson in knot-tying.",
    "Lacquer is applied in layers, each one adding a layer of shine to the bowl.",
    "That is not a backdrop. It is the reason for the trip.",
    "There is no fee for the shrine.",
    "Pack the way most guides do: light.",
])
def test_round3_residue_leaves_legitimate_sentences_alone(sentence):
    assert not residue(f"The camp sits by the lake. {sentence} The road north is long."), sentence


# ---------------------------------------------------------------- seed-aware findings (F15, one repair, never a hold)

SEED = ("School of Arts — visit\nSource itinerary: visit the School of Arts.\nPlace background:\n"
        "- The institute was established to ensure craft skills are passed to new generations.\n"
        "- The institute plays a direct role in supplying artisans for monasteries.")


@pytest.mark.parametrize("sentence, found", [
    ("For families travelling with children aged seven and up, the school is a calm stop.", "aged seven and up"),
    ("The workshop suits ages 8+ and their parents.", "ages 8+"),
    ("The visit works well for children over ten who like to draw.", "children over ten"),
    ("A 10-year-old can follow the carving from block to panel.", "10-year-old"),
])
def test_unsupported_age_is_found(sentence, found):
    assert hygiene.unsupported_ages(sentence, SEED) == [found]


@pytest.mark.parametrize("sentence, source", [
    ("The temple is 300 years old.", SEED),                                   # a thing's age
    ("The drive takes 25 to 40 minutes.", SEED),                              # a duration
    ("Children aged seven and up can join the class.", SEED + "\n- Classes are open to children aged 7 and over."),
    ("The class is for ages 8 to 12.", "Workshops for ages 8 to 12 run on Saturdays."),
])
def test_age_the_seed_states_or_not_an_age_is_not_found(sentence, source):
    assert hygiene.unsupported_ages(sentence, source) == []


def test_unsupported_founding_reason_is_found():
    s = "The institute was established because these skills supply the artisans for monasteries."
    assert hygiene.unsupported_reasons(s, SEED) == ["was established because"]
    assert hygiene.unsupported_reasons("The institute exists because the monasteries need painters.", SEED) == [
        "exists because"]


@pytest.mark.parametrize("sentence, source", [
    # the seed gives a reason of its own
    ("The institute was established because the old masters were ageing.",
     SEED + "\n- The institute was founded because the old masters were ageing."),
    # the seed states no purpose for this subject: background the SOURCE FIDELITY rule allows
    ("The preserve exists because the takin kept returning to the city.", SEED),
    ("The bridge was built because the ford flooded every spring.", SEED),
    # no founding verb
    ("Many people visit because the falls are close to town.", SEED),
    ("The institute was established in 1971 to pass the skills on.", SEED),
])
def test_founding_reason_negatives(sentence, source):
    assert hygiene.unsupported_reasons(sentence, source) == []


def test_seed_aware_findings_ride_f15_and_read_the_seed():
    text = ("The school trains painters for years. For families travelling with children aged seven and up, the "
            "school is a calm stop. The institute was established because these skills supply the artisans.")
    g = gate_voice(text, spec=None, stock_blocking=True, exclude_texts=[SEED])
    found = [v for v in g["violations"] if v.startswith(UNSUPPORTED_DETAIL_PREFIX)]
    assert not g["passed"] and g["blocking"] and len(found) == 2
    assert found[0].startswith("unsupported detail: an age ('aged seven and up') the source does not give")
    assert found[1].startswith("unsupported detail: a reason ('was established because') the source does not give")
    assert all(re.match(r"^(.+?): '(.+)'\s*$", v) for v in found)  # sentence_repair's quote shape
    seed_says = SEED + "\n- Families with children aged 7 and over are welcome.\n- It was established because X."
    assert not [v for v in gate_voice(text, spec=None, exclude_texts=[seed_says])["violations"]
                if v.startswith(UNSUPPORTED_DETAIL_PREFIX)]
    assert gate_voice(text, spec=None, stock_blocking=False, exclude_texts=[SEED])["blocking"] is False


# ---------------------------------------------------------------- the prompt

def test_system_prompt_gets_the_round3_fidelity_rules():
    system = " ".join(prompts.SYSTEM_PROMPT.split())
    assert ('AGES: never give an age, an age band or a minimum age ("children aged seven and up", "ages 8+") unless '
            "the content seed states it. The AUDIENCE line's ages describe the brand's customers, not this trip or "
            "this place.") in system
    assert ("CAUSE AND SCOPE: keep the seed's own links and limits. Never make one fact the reason for another "
            '("established because it supplies artisans", "which is why") unless the seed links them') in system
    assert '"like most" is not "every"; "one of the" is not "the".' in system
    assert "what the place looks, sounds, smells or feels like (cold water, a quiet hall)" in system
    numbers = system.split("NUMBERS:")[1].split("AGES:")[0]
    assert "age," not in numbers and "day count" in numbers


def test_brand_energy_lines_are_unchanged():
    """Round 3 must not weaken brand voice (round-2 panel: 7.33, every tenant ≥ 7): PACE / RESTRAINT stay as they
    were."""
    pace = brand_voice_rule({"voice": ["Energetic"]})
    calm = brand_voice_rule({"voice": ["Calm"]})
    assert "PACE FOR THIS BRAND (its voice: Energetic): short paragraphs of two or three sentences" in pace
    assert '"you" (only what the CONTENT SEED states)' in " ".join(pace.split())
    assert "RESTRAINT FOR THIS BRAND (its voice: Calm): state each fact and move on." in calm


def test_writer_prompt_version_moves_off_round_two():
    assert service.WRITER_PROMPT_VERSION == "24363541"  # round 2 + review: afc08095


# ---------------------------------------------------------------- linear time (CodeQL py/polynomial-redos)

_N = 8000
_ADVERSARIAL = {
    "spaces": " " * _N,
    "space-tab": " \t" * (_N // 2),
    "The+spaces": "The" + " " * _N + "x",
    "words": "The " + "word " * (_N // 5),
    "not": "x " + "not " * (_N // 4) + "x",
    "not-comma": "x, not " + "a " * (_N // 2) + "x",
    "rather-than": " rather than " + "a " * (_N // 2) + "x",
    "colon": "x: not " + "a " * (_N // 2) + ", but",
    "something": " offers something " + "more " * (_N // 5) + ":",
    "something-no": " offers something that no " + "a " * (_N // 2),
    "digit-spaces": "7" + " " * _N + "x",
    "digits": "7 " * (_N // 2),
    "aged": "aged " * (_N // 5),
    "for-comma": "For " + "a" * _N,
    "and-the": "x" + " and" * (_N // 4),
    "because": "was established " + "in " * (_N // 3),
    "letters": "a" * _N,
    # second review: the source-age cues and their linking words
    "age-cues": "children aged at least " * (_N // 23) + "x",
    "under-dash": "under-" * (_N // 6) + "5",
    "num-and": "7 and " * (_N // 6) + "x",
    "num-dash": "7-" * (_N // 2),
    # third review: superlatives, 'years' runs, years-old cue chains, 'per' and plural runs
    "most": "the most " * (_N // 9) + "x",
    "years": "7 years " * (_N // 8),
    "years-old": "children must be " * (_N // 17) + "7 years old",
    "per": "7 per " * (_N // 6),
    "of-link": "families of " * (_N // 12) + "4 x",
}


def _round3_patterns():
    """Every regex this round adds or changes: ai_tells (round-1 _NOT_BUT_RE gains the comma group, _MEANT_RE the
    'to its …' tail), hygiene's two seed-aware patterns and every round-3 AI_TELL_LINE_RES entry."""
    names = ("_NOT_BUT_RE", "_STAGING_TAIL_RE", "_SOMETHING_RE", "_MEANT_RE", "_CHANGES_VERDICT_RE",
             "_RELOCATE_X_RE", "_AND_THE_RE", "_Y_PHRASE_RE", "_STAGED_NOUN_X_RE", "_AUDIENCE_RE", "_SUPERLATIVE_RE")
    out = [(n, getattr(ai_tells, n)) for n in names]
    out += [(n, getattr(hygiene, n)) for n in ("_AGE_CLAIM_RE", "_FOUNDING_REASON_RE", "_PURPOSE_RE",
                                                "_SOURCE_SENTENCE_RE", "_COUNT_AFTER_RE", "_PLURAL_AFTER_RE",
                                                "_PER_AFTER_RE", "_YEARS_AFTER_RE", "_YEARS_OLD_CUE_RE",
                                                # fourth review: the explicit-cue source-age readers
                                                "_SRC_AGE_WORD_RE", "_SRC_PERSON_RANGE_RE", "_SRC_PERSON_TAIL_RE",
                                                "_SRC_HYPHEN_AGE_RE", "_SRC_YEARS_AND_OVER_RE", "_SRC_YEARS_OLD_RE",
                                                "_DURATION_TAIL_RE", "_UNIT_AFTER_RE",
                                                # the fact-preservation invariant's tokeniser and particle look
                                                "_FACT_TOKEN_RE", "_PARTICLE_AFTER_RE", "_SEED_LABEL_RE")]
    start = next(i for i, r in enumerate(hygiene.AI_TELL_LINE_RES)
                 if r.pattern.startswith(r"\b(?:that|this|it)(?:['’]s|\s+is)\s+(?:a|the|one)\s+(?:detail"))
    out += [(f"AI_TELL_LINE_RES[{i}]", r) for i, r in enumerate(hygiene.AI_TELL_LINE_RES) if i >= start]
    assert all(isinstance(p, re.Pattern) for _, p in out) and len(out) > 30
    return out


@pytest.mark.parametrize("name, rx", _round3_patterns(), ids=lambda v: v if isinstance(v, str) else "")
def test_round3_regexes_run_in_linear_time(name, rx):
    for label, s in _ADVERSARIAL.items():
        best = min(_timed(rx, s) for _ in range(2))
        assert best < 0.05, f"{name} on {label}: {best * 1000:.0f} ms"


def test_round3_functions_run_in_linear_time():
    """The whole pass and the seed-aware finders on long adversarial input (one sentence each)."""
    for s in _ADVERSARIAL.values():
        t = time.perf_counter()
        hygiene.unsupported_ages(s, s)
        hygiene.unsupported_reasons(s, s)
        ai_tells._staging_tail(s, frozenset())
        ai_tells._something_lead(s, frozenset())
        ai_tells._not_but(s, frozenset())
        ai_tells._seed_denies("relic", ai_tells._SourceWords(s))
        ai_tells._audience(s)
        ai_tells._superlative_fact(s)
        ai_tells._verdict(s, frozenset())
        hygiene._source_ages(s)
        assert time.perf_counter() - t < 0.5


def _timed(rx: re.Pattern, s: str) -> float:
    t = time.perf_counter()
    rx.search(s)
    rx.match(s)
    return time.perf_counter() - t


# ---------------------------------------------------------------- round-3 review fixes (S252, blocking findings)
# Each finding's own sentences: the fact must stay. The tells the panel quoted still go (tests above).

def _seed_ctx(source: str) -> TellContext:
    return TellContext(cta=CTX.cta, source=source)


@pytest.mark.parametrize("sentence, source", [
    # finding 1 — the seed-fact guard holds in the colon 'not X, but Y' frame
    ("The best months are dry: not in the monsoon, but in April.",
     "Avoid the monsoon months; April is the best time to visit."),
    ("The dzong is what it has always been: not a museum, but a working institution.",
     "The dzong is not a museum; it is a working institution."),
    ("Families stay in the lodge: not in tents, but in rooms.", "The lodge has rooms; there are no tents."),
    # a staged-thing noun the seed itself denies keeps its contrast
    ("Few visitors know what a dzong is: not a relic, but a working institution.",
     "The dzong is not a relic of the past; it is a working institution."),
])
def test_colon_not_but_keeps_a_contrast_the_seed_states(sentence, source):
    text = _blog("The monks gather at dawn. " + sentence)
    assert rewrite(text, _seed_ctx(source))[0] == text


def test_colon_not_but_folds_a_staged_noun_the_seed_uses_as_a_plain_noun():
    """The panel's quote: the seed names 'the sacred relic of …' but never denies a relic."""
    ctx = _seed_ctx("The dzong houses the sacred relic of the founder.")
    before = "Few visitors know what a dzong is: not a relic, but a working institution."
    # round 3 (S252): 'relic' is a seed word, so the fact invariant lets it go only while the piece still names the
    # relic (it does in the panel's text; a lone mention is kept: test_fact_invariant_keeps_a_lone_seed_word)
    text, log = rewrite(_blog("The monks guard the relic. " + before), ctx)
    assert text == _blog("The monks guard the relic. Few visitors know what a dzong is: a working institution.")
    assert log == [f"ai_tells: not X but Y: '{before}'"]


@pytest.mark.parametrize("sentence, source", [
    # finding 3 — itinerary nouns and verbs in a 'rather than / instead of' tail are expectation or welfare facts
    ("On the third morning you will see the dance rehearsal rather than the full performance.", ""),
    ("Most departures fall before the festival, so you see the village preparations rather than the festival "
     "performances.", ""),
    ("The elephants are watched from a raised platform rather than used in shows.", ""),
    ("Children can join the cooking class rather than watch a demonstration.", ""),
    ("The elephants live in the forest rather than used as backdrops.", ""),
    ("The weaving is a long commitment rather than a demonstration.", ""),
    ("On the third morning you will see the dance rehearsal rather than the full performance.",
     "Visitors watch rehearsals; the masked dances themselves are held only at Tshechu."),
])
def test_staging_tail_keeps_itinerary_and_welfare_facts(sentence, source):
    text = _blog("The monks gather at dawn. " + sentence)
    assert rewrite(text, _seed_ctx(source))[0] == text


@pytest.mark.parametrize("before, after", [
    ("The welcome in the village is a working morning rather than a mere backdrop.",
     "The welcome in the village is a working morning."),
    ("The masks are carved for the monastery rather than as museum pieces.",
     "The masks are carved for the monastery."),
])
def test_staging_tail_still_cuts_staging_words(before, after):
    text, log = rewrite(_blog("The monks gather at dawn. " + before), CTX)
    assert text == _blog("The monks gather at dawn. " + after)
    assert log == [f"ai_tells: staging contrast tail: '{before}'"]


@pytest.mark.parametrize("pair", [
    # finding 4 — an audience limit in the denial is a fact: never joined, never cut, at a paragraph start too
    "This is not a walk for small children. It climbs steeply on loose scree for most of the morning.",
    "This is not a hike for beginners. It climbs over the ridge to the lake.",
    "This is not a stop for toddlers. It is a long climb on worn stone steps.",
    "These are not rooms for families. They are small cells built for the monks.",
    "This is not a trail for wheelchair users. It crosses two rope bridges.",
])
@pytest.mark.parametrize("where", ["paragraph start", "mid paragraph"])
def test_an_audience_denial_is_never_joined_or_cut(pair, where):
    text = _blog(pair) if where == "paragraph start" else _blog("The monks gather at dawn. " + pair)
    assert rewrite(text, CTX)[0] == text


@pytest.mark.parametrize("lone", ["This is not a place for small children.", "It is not for seniors."])
def test_an_audience_lone_denial_is_never_cut(lone):
    text = _blog(f"The monks gather at dawn. {lone} The path climbs on loose scree.")
    assert rewrite(text, CTX)[0] == text


@pytest.mark.parametrize("sentence", [
    # finding 5 — schedules, bookings, footing and routes are facts, never F15 stock lines
    "There is no show on Mondays, when the theatre is closed.",
    "There is no performance during Ramadan.",
    "There is no performance here on the first evening.",
    "There is no show.",
    "This is the stage of the route worth booking early, as seats on the boat are limited.",
    "The steep path rewards careful attention to footing in the wet season.",
    "After rain the stone steps reward close attention.",
    "The loop road closes the circle back to Paro on the last day.",
])
def test_round3_residue_leaves_schedule_and_practical_sentences_alone(sentence):
    text = f"The camp sits by the lake. {sentence} The road north is long."
    assert not residue(text), sentence
    g = gate_voice(text, spec=None, stock_blocking=True)
    assert not any(sentence in v for v in g["violations"])


@pytest.mark.parametrize("sentence", [
    "There is no performance in this, no arranged demonstration.",
    "There is no performance here.",
    "There is no show, no script.",
    "This is the section of the journey that rewards the greatest attention.",
    "The final stretch rewards careful attention.",
    "The school sends its graduates to the temples, closing the circle.",
])
def test_round3_residue_still_flags_the_aphorism_forms(sentence):
    assert residue(f"The camp sits by the lake. {sentence} The road north is long."), sentence


@pytest.mark.parametrize("sentence, source", [
    # findings 2 and 6 — a number the seed writes in any wording is a known age
    ("Kids aged twelve and over can ride the ponies.", "Minimum age is 12."),
    ("Kids aged 8 and up join the class.", "Suitable for children from 8 years."),
    ("Children under 5 stay at the camp.", "Not suitable for under-5s."),
    ("Riders aged 14 and over may take the long loop.", "Riders must be at least 14."),
    ("Kids aged 6 to 12 join the class.", "Children 6–12 join the class."),
    ("Kids aged 6 and up are welcome.", "Minimum age: 6."),
    ("The class is a hit with 10-year-olds.", "Open to children 10 and older."),
    ("Kids aged 6 and up are welcome.", "Open to children from 6 years."),
    # a thing's age, attributive: never a person's age, whatever the seed says
    ("The 300-year-old monastery stands above the river.", "The monastery is about 300 years old."),
    ("The 300-year-old temple still stands.", "The temple was built 300 years ago. The city is over 600 years old."),
    ("The 600-year-old city walls are intact.", "The temple was built 300 years ago."),
    ("The 2,000-year-old clay tradition lives on in the village.", ""),
    ("A 12-year-old tradition of mask carving survives.", ""),
    ("The 300-year-old is the oldest tree in the valley.", ""),
])
def test_age_known_from_the_seed_or_a_things_age_is_not_found(sentence, source):
    assert hygiene.unsupported_ages(sentence, source) == []


@pytest.mark.parametrize("sentence, found", [
    ("It is as legible to a twelve-year-old as to a geographer.", "twelve-year-old"),
    ("The seven-year-old girl in the group carried the lantern.", "seven-year-old"),
    ("The class is a hit with 10-year-olds.", "10-year-olds"),
    ("Kids aged 8 and up join the class.", "aged 8 and up"),
])
def test_unsupported_person_age_is_still_found(sentence, found):
    assert hygiene.unsupported_ages(sentence, "The monastery is 300 years old; the class runs for 3 hours.") == [
        found]


def test_a_seed_stated_age_reaches_no_f15_finding():
    text = "The camp sits by the lake. Kids aged twelve and over can ride the ponies. The road north is long."
    g = gate_voice(text, spec=None, stock_blocking=True, exclude_texts=("Minimum age is 12.",))
    assert not any(v.startswith(UNSUPPORTED_DETAIL_PREFIX) for v in g["violations"])


def test_an_anchored_residue_entry_reports_its_own_phrase():
    """The 'rewards … attention' entry is anchored at the sentence start for its fact guard; the F15 finding still
    names the phrase, not the sentence's first words."""
    text = "The camp sits by the lake. The ninth day turns to two temples that reward careful attention. The end."
    assert residue(text) == [("reward careful attention",
                              "The ninth day turns to two temples that reward careful attention.")]


# ---------------------------------------------------------------- second review of round 3 (S252, 4 blocking)
# Every quoted sentence, exact output, at a paragraph start and mid-paragraph. A rule only removes words that are a
# tell; it never deletes a fact, restriction, audience or seasonal limit, and never acts on a sentence it wrote.

TAKTSANG = ("Paro — Taktsang\nSource itinerary: hike up to Taktsang Monastery, which clings to a cliff above the Paro "
            "valley. The walk up takes about three hours.")


def _placed(sentences: str, where: str) -> str:
    return _blog(sentences) if where == "paragraph start" else _blog("Pack for the weather. " + sentences)


@pytest.mark.parametrize("pair", [
    # finding 1 — the demonstrative join would read 'This is the …', which the second structure pass cut as a
    # verdict line: both facts went. The pair now stays whole.
    "This is not the dry season. It is the tail of the monsoon.",
    "This is not the end of the climb. It is the halfway point.",
    "This is not the main entrance. It is the side gate used by pilgrims.",
    "This is not the last ferry of the day. It is the second-last.",
    "This is not the only way up. It is the quickest.",
    # a limit in the denied part ('only'): other routes exist
    "This is not the only path to the monastery. It is the most direct route.",
])
@pytest.mark.parametrize("where", ["paragraph start", "mid paragraph"])
@pytest.mark.parametrize("source", [TAKTSANG, ""], ids=["taktsang seed", "no source"])
def test_a_correction_pair_keeps_both_facts(pair, where, source):
    text = _placed(pair, where)
    assert rewrite(text, _seed_ctx(source)) == (text, [])


def test_the_reviewers_seasonal_paragraph_is_unchanged():
    text = _blog("Pack for the weather. This is not the dry season. It is the tail of the monsoon.")
    assert rewrite(text, _seed_ctx(TAKTSANG))[0] == text


@pytest.mark.parametrize("lone", ["This is not the only way up.", "It is not the only bridge across."])
def test_a_lone_denial_with_a_limit_is_never_cut(lone):
    text = _blog(f"The monks gather at dawn. {lone} The path climbs on loose scree.")
    assert rewrite(text, CTX)[0] == text


@pytest.mark.parametrize("where", ["paragraph start", "mid paragraph"])
def test_a_sentence_the_pass_wrote_is_never_cut_by_a_later_pass(where):
    """'not X but Y' writes 'This is the heart of the valley.' (verdict-shaped); the structure passes never cut it."""
    before = "This is not a museum but the heart of the valley."
    text, log = rewrite(_placed(before, where), CTX)
    assert text == _placed("This is the heart of the valley.", where)
    assert log == [f"ai_tells: not X but Y: '{before}'"]


def test_the_tell_join_still_happens():
    """The round-3 join the panel needed is unaffected: its Y is no verdict shape."""
    before = "This is not a landscaped garden. It is a place where the frontier war left a mark on the valley."
    text, log = rewrite(_placed(before, "mid paragraph"), CTX)
    assert text == _placed("This is a place where the frontier war left a mark on the valley.", "mid paragraph")
    assert log == [f"ai_tells: denial + affirmation joined: '{before}'"]


@pytest.mark.parametrize("sentence", [
    # finding 3 — an audience limit in the denied part of the three word rules
    "The ferry is not for foot passengers but for vehicles only.",              # not X but Y, same preposition
    "The tour suits one group only: not families, but adults.",                 # not X but Y, colon / comma frame
    "The tour suits one group: not families, but adults.",
    "This trek suits fit teenagers, not young children.",                       # , not X.
    "The bridge carries cyclists, not pedestrians.",
])
@pytest.mark.parametrize("where", ["paragraph start", "mid paragraph"])
def test_an_audience_limit_in_a_word_rule_is_never_cut(sentence, where):
    text = _placed(sentence, where)
    assert rewrite(text, CTX) == (text, [])


@pytest.mark.parametrize("where", ["paragraph start", "mid paragraph"])
def test_an_audience_inline_pair_keeps_its_denial(where):
    """'X is not A; it is B' with an audience in A is never folded; the semicolon split (punctuation only, every
    word kept) still applies, and the structure pass never joins the two sentences it wrote."""
    before = "The rafting section is not for non-swimmers; it is for confident swimmers aged 12 and over."
    text, log = rewrite(_placed(before, where), CTX)
    assert text == _placed("The rafting section is not for non-swimmers. It is for confident swimmers aged 12 and "
                           "over.", where)
    assert log == [f"ai_tells: semicolon split: '{before}'"]


# findings 2 and 4 — a seed number is a known age only in an age context
ITINERARY_SEED = "Day 8: Paro. Drive 3-4 hours to the valley, max 12 guests. Lunch at 12:30."
TERRA_SEED = "Day 7 of the trip\nSource itinerary: 10 km from the hotel. Students train for six years."


@pytest.mark.parametrize("sentence, found", [
    ("Ideal for families with children aged 8 and up.", "aged 8 and up"),            # 8 from 'Day 8'
    ("A 12-year-old can manage the walk easily.", "12-year-old"),                    # 12 from 'max 12 guests'
    ("Great for 4-year-olds.", "4-year-olds"),                                       # 4 from '3-4 hours'
    ("The guide is a 30-year-old who grew up in the valley.", "30-year-old"),        # 30 from '12:30'
])
def test_day_duration_group_and_clock_numbers_are_not_known_ages(sentence, found):
    assert hygiene.unsupported_ages(sentence, ITINERARY_SEED) == [found]


@pytest.mark.parametrize("sentence, found", [
    ("For families travelling with children aged seven and up, the school is a calm stop.", "aged seven and up"),
    ("Kids aged 10 and over enjoy the carving room.", "aged 10 and over"),
    ("The visit suits children over six.", "children over six"),
    ("A 7-year-old can follow the carving from block to panel.", "7-year-old"),
    ("Kids aged 8 and up join the class.", "aged 8 and up"),
])
def test_a_day_number_distance_or_training_years_are_not_known_ages(sentence, found):
    assert hygiene.unsupported_ages(sentence, TERRA_SEED) == [found]


@pytest.mark.parametrize("sentence, source", [
    # the reviewer's wordings stay known next to day / duration / group numbers
    ("Kids aged twelve and over can ride the ponies.", ITINERARY_SEED + " Minimum age is 12."),
    ("Kids aged 8 and up join the class.", ITINERARY_SEED + " Suitable for children from 8 years."),
    ("Children under 5 stay at the camp.", TERRA_SEED + " Not suitable for under-5s."),
    ("Riders aged 14 and over may take the long loop.", TERRA_SEED + " Riders must be at least 14."),
    ("Kids aged 6 to 12 join the class.", TERRA_SEED + " Children 6–12 join the class."),
    ("Kids aged 7 and up are welcome.", "Day 3 of the trip. Open to children 7 and older."),
    ("Kids aged 7 and up are welcome.", "Day 3 of the trip. Minimum age: 7."),
    ("Kids aged 7 and up are welcome.", "Day 3. Children under the age of 7 stay with a carer."),
    ("Kids aged 10 and over are welcome.", "Day 3. Suitable for 10 years and over."),
    ("Kids aged 10 and over are welcome.", "Day 3. Recommended for travellers aged 10+."),
])
def test_a_seed_stated_age_stays_known_beside_day_numbers(sentence, source):
    assert hygiene.unsupported_ages(sentence, source) == []


@pytest.mark.parametrize("source", [
    "Day 30 of the trip.", "The ride is over 30 km.", "Over 30 monasteries line the valley.",
    "Groups of 30 people.", "Dinner at 7:30.", "The festival draws 1,300 dancers.", "At least 30 minutes.",
])
def test_non_age_numbers_never_make_an_age_known(source):
    assert hygiene.unsupported_ages("The guide is a 30-year-old who grew up here.", source) == ["30-year-old"]


def test_an_age_the_itinerary_seed_does_not_give_reaches_f15():
    text = "The camp sits by the lake. A 12-year-old can manage the walk easily. The road north is long."
    g = gate_voice(text, spec=None, stock_blocking=True, exclude_texts=(ITINERARY_SEED,))
    assert any(v.startswith(UNSUPPORTED_DETAIL_PREFIX) and "12-year-old" in v for v in g["violations"])


# ---------------------------------------------------------------- third review of round 3 (S252, 6 blocking)
# Every quoted sentence, exact output, at a paragraph start and mid-paragraph, with no source and with the Taktsang
# seed. A rule only removes the words that are the tell: never a limit, a place / time choice, an audience, a
# superlative or 'only' fact, and never a sentence the pass itself wrote.

_BOTH_SOURCES = pytest.mark.parametrize("source", [TAKTSANG, ""], ids=["taktsang seed", "no source"])
_BOTH_PLACES = pytest.mark.parametrize("where", ["paragraph start", "mid paragraph"])


@pytest.mark.parametrize("sentence", [
    # finding 1 — the comma / colon 'not X, but Y' frame folds only on the copula path, never on a limit
    "The east trail is not the only route up, but the shortest.",
    "The tour is not for vegetarians, but for meat lovers.",
    "Dinner is not at the lodge, but at a farmhouse.",
    "The ferry runs on one day only: not on Sundays, but on Saturdays.",
    # finding 2 — a limit ('only') in the denied part of 'not X but Y', ', not X.' and 'X is not A; it is B'
    "This is not the only path to the monastery; it is the most direct.",
    "This is not the only ferry to the island; it is the fastest.",
    "This is not the only path to the monastery but the most direct one.",
    "The ferry is the quickest crossing, not the only one.",
    # finding 4 — audience words in a lone denial
    "Bring water. This is not a trek for people with vertigo.",
    "Bring water. This is not a walk for anyone with vertigo.",
    # finding 5 (b) — a superlative or 'only' fact after its practical sentence is no verdict line
    "Bring a jacket. This is the coldest part of the trek.",
    "Do not come in July. This is the wettest month.",
    "The museum is closed on Mondays. This is the only day off.",
])
@_BOTH_PLACES
@_BOTH_SOURCES
def test_third_review_sentences_are_unchanged(sentence, where, source):
    text = _placed(sentence, where)
    assert rewrite(text, _seed_ctx(source)) == (text, [])


@pytest.mark.parametrize("before, after", [
    # finding 2 — 'after heavy rain' is a limit: the inline pair keeps it; only the semicolon split applies
    ("The bridge is not passable after heavy rain; it is a simple log crossing.",
     "The bridge is not passable after heavy rain. It is a simple log crossing."),
    # finding 4 — the split's second sentence names an audience ('the unfit') and was written by the pass
    ("We walk for two hours along the ridge; this is not a hike for the unfit.",
     "We walk for two hours along the ridge. This is not a hike for the unfit."),
    # finding 5 (a) — the split's second sentence is a written sentence: never cut as a verdict line
    ("The road climbs through pine forest for most of the morning; this is the hardest section of the whole walk.",
     "The road climbs through pine forest for most of the morning. This is the hardest section of the whole walk."),
])
@_BOTH_PLACES
@_BOTH_SOURCES
def test_third_review_semicolon_sentences_keep_every_word(before, after, where, source):
    text, log = rewrite(_placed(before, where), _seed_ctx(source))
    assert text == _placed(after, where)
    assert log == [f"ai_tells: semicolon split: '{before}'"]


@_BOTH_PLACES
def test_a_split_marks_each_written_sentence(where):
    """The guard behind finding 5 (a), without the superlative guard: each half of a split is a written sentence,
    so the structure passes never cut the verdict-shaped second half ('Edit.new' alone held both halves joined)."""
    before = "The road climbs through pine forest for most of the morning; this is the section worth a slow pace."
    text, log = rewrite(_placed(before, where), CTX)
    assert text == _placed("The road climbs through pine forest for most of the morning. This is the section worth a "
                           "slow pace.", where)
    assert log == [f"ai_tells: semicolon split: '{before}'"]


@pytest.mark.parametrize("before, after", [
    # the role frame 'not as X, but as Y' still folds (the panel's 'not as cultural artefacts, but as living …')
    ("The craft is taught not as a museum object, but as living practice.",
     "The craft is taught as living practice."),
    # the bare same-preposition frame still folds (no comma, no colon)
    ("The hour is set not by a calendar but by the forest.", "The hour is set by the forest."),
    # the copula path after a comma still folds
    ("The dzong is not a relic, but a working institution.", "The dzong is a working institution."),
])
def test_third_review_tell_frames_still_fold(before, after):
    text, log = rewrite(_placed(before, "mid paragraph"), CTX)
    assert text == _placed(after, "mid paragraph")
    assert log == [f"ai_tells: not X but Y: '{before}'"]


@pytest.mark.parametrize("cut", [
    "This is the most rewarding part of the day.",       # an evaluative 'most' is still a verdict
    "This is the section worth slowing down for.",
])
def test_an_evaluative_verdict_is_still_cut(cut):
    text, log = rewrite(_blog(f"The falls drop into turquoise pools. {cut} The water is cold."), CTX)
    assert text == _blog("The falls drop into turquoise pools. The water is cold.")
    assert log == [f"ai_tells: verdict line: '{cut}'"]


@pytest.mark.parametrize("rest, fact", [
    ("the coldest part of the trek", True), ("the wettest month", True), ("the most exposed stretch", True),
    ("the least crowded hour", True), ("the most rewarding part", False), ("the forest edge", False),
    ("the harvest", False), ("the opening", False),
])
def test_superlative_fact(rest, fact):
    assert ai_tells._superlative_fact(rest) is fact


# findings 3 and 6 — counts, durations, group sizes and a thing's age never make a number a known age
DORJI_SEED = ("Day 4 of the trip. The Dorji family has run this weaving workshop for over 12 years. Students from 8 "
              "villages train here for at least 10 years.")


@pytest.mark.parametrize("sentence, source, found", [
    ("Ideal for families with children aged 12 and up.", DORJI_SEED, "aged 12 and up"),
    ("Kids aged 10 and over can try the loom.", DORJI_SEED, "aged 10 and over"),
    ("Children over 8 will love it.", DORJI_SEED, "Children over 8"),
    ("A 8-year-old can manage the walk.", DORJI_SEED, "8-year-old"),
    ("Ideal for families with children aged 12 and up.", "Day 4 of the trip.", "aged 12 and up"),
    ("Children aged 4 and up will love it.", "Families of 4 share one farmhouse room.", "aged 4 and up"),
    ("Great for 4-year-olds.", "Ideal for families of 4. Day 2 walk.", "4-year-olds"),
    ("Children aged 12 and over can join the class.", "Students from 12 districts train here for six years.",
     "aged 12 and over"),
    ("Ideal for children aged 12 and up.", "Travellers from 12 countries join each departure.", "aged 12 and up"),
    ("Children over 3 can ride.", "Families from 3 villages farm the valley.", "Children over 3"),
    ("Suitable for children aged 12 and up.", "Participants: 2-12 per departure.", "aged 12 and up"),
    ("Ideal for travellers over 30.", "Guests at over 30 stalls taste cheese.", "travellers over 30"),
    ("Children aged 7 and up.", "Hikers at 7 viewpoints stop for tea.", "aged 7 and up"),
    ("Kids aged 8 and up join in.", "Students of 8 schools perform.", "aged 8 and up"),
    ("Kids aged 10 and over enjoy it.", "The institute has trained artisans for over 10 years.", "aged 10 and over"),
    ("Children aged 8 and up.", "The school opened over 8 years ago.", "aged 8 and up"),
    ("Great for children aged 6 and up.", "The new visitor centre is 6 years old.", "aged 6 and up"),
    ("A 12-year-old can follow the dances.", "The festival, now 12 years old, fills the square.", "12-year-old"),
])
def test_counts_durations_and_thing_ages_are_not_known_ages(sentence, source, found):
    assert hygiene.unsupported_ages(sentence, source) == [found]


@pytest.mark.parametrize("source, number", [
    ("The family has run it for over 12 years.", 12), ("They have trained for at least 10 years.", 10),
    ("Children from 8 villages attend.", 8), ("Students from 3 universities visit.", 3), ("Adults over 2 nights.", 2),
    ("Families of 4 share a room.", 4), ("Participants: 2-12 per departure.", 12),
    ("The new visitor centre is 6 years old.", 6), ("The festival, now 12 years old, fills the square.", 12),
])
def test_a_count_or_duration_never_makes_an_age_known(source, number):
    assert number not in hygiene._source_ages(source)


@pytest.mark.parametrize("source, number", [
    # every wording a seed states an age in stays known
    ("Suitable for children from 8 years.", 8), ("Riders must be at least 14.", 14), ("Children 6–12 join.", 12),
    ("Open to children 7 and older.", 7), ("Suitable for 10 years and over.", 10),
    ("Children under the age of 7 stay with a carer.", 7), ("Minimum age is 12.", 12), ("Not for under-5s.", 5),
    ("Children 12 years old and up.", 12), ("Riders must be 12 years old.", 12), ("Must be 12 years of age.", 12),
    ("Children under 12 stays free.", 12), ("Families with children of 8 years and over.", 8),
    ("At least 12 years old.", 12), ("Kids aged 10+.", 10),
])
def test_every_age_wording_stays_known(source, number):
    assert number in hygiene._source_ages(source)
