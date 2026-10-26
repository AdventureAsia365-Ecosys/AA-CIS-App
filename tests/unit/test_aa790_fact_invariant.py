"""AA-790 round 3 (S252) — the fact-preservation invariant and the explicit-cue age reading.

Three review rounds kept finding a new chain of rules that deleted a fact (a join the verdict rule then cut, a lone
denial that was itself the safety or access rule, a place / time choice folded away, a verdict that restated a seed
fact, an audience limit). The rules stay as proposers; ``ai_tells._accept`` (and the repeat deletes of
``hygiene.dedupe`` / ``autofix``) refuse any edit that removes a fact token the text after it no longer carries
(``hygiene.fact_tokens`` / ``missing_facts``). This file holds:

* exact-output tests for every sentence quoted in the second and third reviews of round 3;
* a generated property test: ≥ 300 sentences from denial / contrast / verdict frames × fact tokens of every class,
  with and without a seed, at a paragraph start and mid-paragraph, plus chained pairs; every fact token survives;
* the explicit-cue age reading (fourth review findings 5 and 7);
* linear-time checks for the invariant itself.

Every sentence is synthetic or the reviewer's own probe sentence, never a tenant's text.
"""
import itertools
import time

import pytest

from services.acp_content_writing import ai_tells, hygiene, text_integrity
from services.acp_content_writing.ai_tells import TellContext, rewrite

CTA = "Message us to start planning your trip."
LEAD = "The old capital sits where two rivers meet."
TAKTSANG = ("Paro — Taktsang\nSource itinerary: hike up to Taktsang Monastery, which clings to a cliff above the Paro "
            "valley. The walk up takes about three hours.")
CLIMB_SEED = ("Day 4: Taktsang hike. The trail climbs 900 m from the car park to the monastery. Pack rain gear in "
              "September.")
ROAD_SEED = "There is no road to the village; it is a two-hour walk."
HAA_SEED = ("Day 6: Trek from Haa to the lakes. Leeches are common from June to September. No phone coverage beyond "
            "Haa.")
PARO_SEED = ("Paro Dzong\nSource itinerary: visit the dzong and its goenkhang (protector chapel), where monks keep the "
             "protector deity.")


def _blog(*paragraphs: str) -> str:
    return "\n\n".join([LEAD, "## The River Valley", *paragraphs, "The day ends at the riverside camp."])


def _placed(sentences: str, where: str) -> str:
    return _blog(sentences) if where == "paragraph start" else _blog("Pack for the weather. " + sentences)


def _ctx(source: str = "") -> TellContext:
    return TellContext(cta=CTA, source=source)


def _unchanged(text: str, source: str) -> None:
    out, log = rewrite(text, _ctx(source))
    assert out == text, out
    assert all(line.startswith("ai_tells: skipped ") for line in log), log


_BOTH_PLACES = pytest.mark.parametrize("where", ["paragraph start", "mid paragraph"])


# ---------------------------------------------------------------- third review: every quoted sentence, exact output

@pytest.mark.parametrize("sentences, source", [
    # finding 1 — same-pronoun correction pairs: the denial cut + the verdict cut lost both facts
    ("Pack for the weather. This is not the dry season. This is the tail of the monsoon.", CLIMB_SEED),
    ("Pack for the weather. It is not the dry season. This is the tail of the monsoon.", CLIMB_SEED),
    ("The trail climbs 900 m to the monastery. This is not the end of the climb. This is the halfway point.",
     CLIMB_SEED),
    ("This is not the main entrance. This is the side gate.", ""),
    ("This is not the monsoon. This is the pre-monsoon heat.", ""),
    ("This is not the dry season. This is the tail of the monsoon.", CLIMB_SEED),
    ("It is not the dry season. This is the tail of the monsoon.", CLIMB_SEED),
    ("This is not the end of the climb. This is the halfway point.", CLIMB_SEED),
    ("This is not the dry season. This is the tail of the monsoon.", ""),
    ("This is not the end of the climb. This is the halfway point.", ""),
    # finding 2 — lone denials that state a safety, access or use fact
    ("We camp by the river tonight. This is not drinking water. Boil it or use the filter in the kitchen.", ""),
    ("This is not a swimming beach. It has strong currents all year.", ""),
    ("This is not a through road. Vehicles turn at the bridge.", ""),
    ("It is not a campsite. Tents go up at the lodge field instead.", ""),
    ("This is not a public footpath. Walkers need the farmer's leave.", ""),
    ("It is not a guided walk. Maps are handed out at the start.", ""),
    # finding 3 — 'X is not A; it is B' that dropped access, location and amenity facts
    ("The village is not reached by road; it is a two-hour walk from the trailhead.", ROAD_SEED),
    ("The homestay is not in the village; it is two kilometres above it.", ""),
    ("The lodge is not heated; it is cold at night.", ""),
    # finding 4 — place, time, channel, goods and vehicle choices without a comma, and ', not X' tails
    ("Dinner is not at the lodge but at a farmhouse.", ""),
    ("The market is held not on Sundays but on Saturdays.", ""),
    ("The boat leaves not from the main pier but from the fishing jetty.", ""),
    ("The festival falls not in April but in late March.", ""),
    ("The tour is not in English but in Mandarin.", ""),
    ("Breakfast is served on the terrace, not in the dining room.", ""),
    ("The dance happens at dusk, not at noon.", ""),
    ("The tickets are sold at the gate, not online.", ""),
    ("The market sells textiles, not food.", ""),
    ("The track is for jeeps, not cars.", ""),
    # finding 6 — verdict-shaped lines that restate a seed fact in other words or inflections
    ("The trail drops into wet forest below the pass. This is the leech zone.", HAA_SEED),
    ("We leave Haa after breakfast. This is where the mobile signal stops.", HAA_SEED),
    # finding 8 — gender, religious-role and visitor-type access limits
    ("The inner chapel admits monks, not women.", PARO_SEED),
    ("The protector chapel is entered by men, not by women.", PARO_SEED),
    ("The inner shrine is not for women but for monks.", PARO_SEED),
    ("This hall is not for women. It is for the monks who keep the protector deity.", PARO_SEED),
    ("The ride is not for pregnant women but for fit adults.", PARO_SEED),
    ("The inner hall is not for tourists but for pilgrims.", PARO_SEED),
    ("The trail is not for groups but for solo walkers.", PARO_SEED),
    ("The inner chapel admits monks, not women.", ""),
    ("This hall is not for women. It is for the monks who keep the protector deity.", ""),
    ("The inner hall is not for tourists but for pilgrims.", ""),
])
@_BOTH_PLACES
def test_third_review_quoted_sentences_keep_every_fact(sentences, source, where):
    _unchanged(_placed(sentences, where), source)


# ---------------------------------------------------------------- second review: every quoted sentence, exact output

@pytest.mark.parametrize("sentences, source", [
    # finding 1 — the 'It is the …' join the verdict rule then cut
    ("Pack for the weather. This is not the dry season. It is the tail of the monsoon.", TAKTSANG),
    ("This is not the end of the climb. It is the halfway point.", TAKTSANG),
    ("This is not the main entrance. It is the side gate used by pilgrims.", ""),
    ("This is not the last ferry of the day. It is the second-last.", ""),
    ("This is not the only way up. It is the quickest.", ""),
    ("This is not the only path to the monastery. It is the most direct route.", ""),
    # finding 3 — audience limits in the word rules
    ("The ferry is not for foot passengers but for vehicles only.", ""),
    ("The tour suits one group only: not families, but adults.", ""),
    ("This trek suits fit teenagers, not young children.", ""),
])
@_BOTH_PLACES
def test_second_review_quoted_sentences_keep_every_fact(sentences, source, where):
    _unchanged(_placed(sentences, where), source)


@_BOTH_PLACES
def test_second_review_rafting_sentence_keeps_its_audience(where):
    """Only the semicolon split applies (punctuation, every word kept)."""
    before = "The rafting section is not for non-swimmers; it is for confident swimmers aged 12 and over."
    out, log = rewrite(_placed(before, where), _ctx())
    assert out == _placed("The rafting section is not for non-swimmers. It is for confident swimmers aged 12 and "
                          "over.", where)
    assert log == [f"ai_tells: semicolon split: '{before}'"]


@pytest.mark.parametrize("sentences, source, fact", [
    # the invariant, not a rule guard, is what refuses these chains: the log names the fact
    ("Pack for the weather. This is not the dry season. This is the tail of the monsoon.", CLIMB_SEED, "season"),
    ("We camp by the river tonight. This is not drinking water. Boil it or use the filter in the kitchen.", "",
     "water"),
    ("The village is not reached by road; it is a two-hour walk from the trailhead.", ROAD_SEED, "road"),
    ("Dinner is not at the lodge but at a farmhouse.", "", "lodge"),
    ("The trail drops into wet forest below the pass. This is the leech zone.", HAA_SEED, "leech"),
    ("The inner chapel admits monks, not women.", PARO_SEED, "women"),
])
def test_the_invariant_names_the_fact_it_kept(sentences, source, fact):
    out, log = rewrite(_placed(sentences, "mid paragraph"), _ctx(source))
    assert out == _placed(sentences, "mid paragraph")
    assert any(line.startswith("ai_tells: skipped ") and "drops a fact:" in line and fact in line for line in log), log


# ---------------------------------------------------------------- the panel's tells still go

@pytest.mark.parametrize("before, after", [
    ("This is not a landscaped garden. It is a place where the frontier war left a mark on the valley.",
     "This is a place where the frontier war left a mark on the valley."),
    ("The craft is taught not as a museum object, but as living practice.", "The craft is taught as living practice."),
    ("The hour is set not by a calendar but by the forest.", "The hour is set by the forest."),
    ("The dzong is not a relic, but a working institution.", "The dzong is a working institution."),
    ("The contact is direct rather than staged.", "The contact is direct."),
])
def test_tells_without_a_fact_still_go(before, after):
    out, _ = rewrite(_placed(before, "mid paragraph"), _ctx())
    assert out == _placed(after, "mid paragraph")


@pytest.mark.parametrize("cut", ["This is the section worth slowing down for.", "That's the opening.",
                                 "This is the most rewarding part of the day."])
def test_verdicts_without_a_fact_still_go(cut):
    out, _ = rewrite(_blog(f"The falls drop into turquoise pools. {cut} The water is cold."), _ctx())
    assert out == _blog("The falls drop into turquoise pools. The water is cold.")


def test_a_lone_seed_word_is_kept_and_a_repeated_one_may_go():
    """'relic' is a seed word: the colon fold goes only while the piece still names the relic."""
    seed = "The dzong houses the sacred relic of the founder."
    before = "Few visitors know what a dzong is: not a relic, but a working institution."
    _unchanged(_blog("The monks gather at dawn. " + before), seed)
    out, _ = rewrite(_blog("The monks guard the relic. " + before), _ctx(seed))
    assert out == _blog("The monks guard the relic. Few visitors know what a dzong is: a working institution.")


# ---------------------------------------------------------------- generated property test

# (phrase X that carries the fact, the fact word as written, a seed that states it or None): every class
_FACTS = [
    # numbers and number words, units after a number
    ("the second ferry", "second", None), ("a 900 m climb", "900", None), ("two hours on foot", "two", None),
    ("a 4 km detour", "km", None), ("the third bridge", "third", None),
    # times, days, months, seasons, weather
    ("the dry season", "season", None), ("a Sunday market", "Sunday", None), ("the monsoon", "monsoon", None),
    ("the morning boat", "morning", None), ("a winter route", "winter", None), ("the April fair", "April", None),
    ("a foggy stretch", "foggy", None),
    # limit and rule words
    ("the only path", "only", None), ("a track closed after rain", "closed", None),
    ("a crossing open until noon", "until", None), ("a free visit", "free", None),
    # audience and person nouns (gender, religious role, visitor type)
    ("a walk for small children", "children", None), ("a chapel for women", "women", None),
    ("a hall for pilgrims", "pilgrims", None), ("a trail for solo walkers", "solo", None),
    ("a room for monks", "monks", None), ("a stop for tourists", "tourists", None),
    # proper names (capitalised, not opening the sentence)
    ("the Paro road", "Paro", None), ("the Haa pass", "Haa", None), ("the Wangdue bridge", "Wangdue", None),
    # place, amenity, access and vehicle nouns
    ("drinking water", "water", None), ("a public footpath", "footpath", None), ("the main entrance", "entrance", None),
    ("a through road", "road", None), ("a campsite", "campsite", None), ("the dining room", "dining", None),
    ("a jeep track", "jeep", None), ("the ferry pier", "pier", None), ("a toilet block", "toilet", None),
    ("a wine shop", "shop", None),
    # superlatives
    ("the steepest stretch", "steepest", None), ("the coldest month", "coldest", None),
    ("the most exposed ledge", "exposed", None),
    # seed content words (in other inflections too)
    ("the leech zone", "leech", "Leeches are common from June to September."),
    ("the prayer flag hill", "prayer", "Prayer flags line the ridge above the pass."),
    ("a pottery stall", "pottery", "Villagers sell pottery at the roadside."),
    # fourth review of round 3: open-class practical words in none of the closed lists the check uses (a denial's
    # own words are facts by default), so the test is not drawn from the lists it checks
    ("a potable spring", "potable", None), ("a paved stretch", "paved", None),
    ("a waymarked stretch", "waymarked", None), ("an insured activity", "insured", None),
    ("a lit passage", "lit", None), ("an air-conditioned hall", "conditioned", None),
    ("a smoking area", "smoking", None), ("a plowed pass", "plowed", None), ("a covered deck", "covered", None),
    ("an insulated hut", "insulated", None), ("an unmarked ledge", "unmarked", None),
    ("an unroped scramble", "unroped", None),
]
_Y = "a working stop"
_FRAMES = {
    "pronoun pair": "This is not {x}. It is {y}.",
    "same-pronoun pair": "This is not {x}. This is {y}.",
    "noun pair": "The stop is not {x}. It is {y}.",
    "lone denial": "This is not {x}. The path climbs on.",
    "not X but Y": "The stop is not {x} but {y}.",
    "colon not X, but Y": "Few visitors know what it is: not {x}, but {y}.",
    ", not X": "The stop is {y}, not {x}.",
    "inline pair": "The stop is not {x}; it is {y}.",
    "verdict": "The path climbs on. This is {x}.",
    "chained correction": "This is not {x}. This is the tail of the monsoon.",
}


def _cases():
    """(frame, sentence, fact word, seed, place). With a seed: the class's own seed, or one that states the same
    phrase. A seed-only word (the last class) is a fact only with its seed, so it runs with the seed only."""
    for (x, word, own_seed), (frame, shape), where, with_seed in itertools.product(
            _FACTS, _FRAMES.items(), ("paragraph start", "mid paragraph"), (False, True)):
        if own_seed and not with_seed:
            continue
        seed = (own_seed or f"Source itinerary: the route passes {x}.") if with_seed else ""
        yield frame, shape.format(x=x, y=_Y), word, seed, where


_CASES = list(_cases())


def _word_count(text: str, word: str) -> int:
    forms = set(hygiene._forms(hygiene.fold(word)))
    return sum(1 for m in hygiene._FACT_TOKEN_RE.finditer(hygiene.fold(text))
               if set(hygiene._forms(m.group(0))) & forms)


def test_the_generator_covers_every_class_and_frame():
    assert len(_CASES) >= 300
    assert {c[0] for c in _CASES} == set(_FRAMES)


@pytest.mark.parametrize("frame", list(_FRAMES))
def test_every_fact_token_survives_the_pass(frame):
    """For each generated sentence: the planted fact word keeps its count, and every fact token of the input (the
    invariant's own reading) is still in the output."""
    failures, proposals = [], 0
    for f, sentence, word, seed, where in _CASES:
        if f != frame:
            continue
        text = _placed(sentence, where)
        out, log = rewrite(text, _ctx(seed))
        proposals += bool(log)
        lost = [t for t, _ in hygiene.fact_tokens(text, hygiene.source_forms(seed))
                if not set(hygiene._forms(t)) & hygiene.word_forms(out)]
        if _word_count(out, word) < _word_count(text, word) or lost:
            failures.append((sentence, seed[:30], where, out.split("\n\n")[2], lost))
    assert not failures, failures[:5]
    if frame not in ("verdict",):
        assert proposals, f"{frame}: no rule proposed an edit, the test exercises nothing"


# ---------------------------------------------------------------- the invariant itself

@pytest.mark.parametrize("text, expected", [
    ("This is not the dry season.", ["dry", "season"]),
    ("The ferry leaves at 7 from the Paro pier.", ["ferry", "7", "paro", "pier"]),
    ("It is a two-hour walk for women and children.", ["two", "hour", "women", "children"]),
    ("This is the steepest and most exposed stretch.", ["steepest", "exposed"]),
    ("That's not a warm-up. The trip opens up properly.", []),       # phrasal 'warm-up', 'opens up'
    ("This is the most rewarding part of the day.", []),             # evaluative, and 'day' without a number
])
def test_fact_tokens(text, expected):
    assert [t for t, _ in hygiene.fact_tokens(text)] == expected


def test_seed_words_skip_the_seed_template():
    seed = ("Paro — Taktsang\nLocation: Paro, Bhutan (do not place it in any other town, region or country)\nDay 3 "
            "of the trip\nSource itinerary: hike to the monastery.\nWhy it matters: leeches.")
    forms = hygiene.source_forms(seed)
    assert {"monastery", "leeche", "hike"} <= forms
    assert not {"itinerary", "source", "matters", "region", "country", "town", "trip"} & forms


def test_missing_facts_counts_closed_words_and_finds_names_anywhere():
    after = hygiene.word_forms("The day ends at the camp near Paro.")
    # 'end' is in the text but the edit removed its own occurrence: a closed word must survive in the edit itself
    # (fourth review: 'climb' is a denied word, read the same way)
    assert hygiene.missing_facts(["This is not the end of the climb."], "", after) == ["end", "climb"]
    # a name only needs to be somewhere in the text after the edit
    assert hygiene.missing_facts(["It is not Paro."], "", after) == []
    assert hygiene.missing_facts(["It does not begin in Paro."], "", after) == ["begin"]  # a denied verb


# ---------------------------------------------------------------- fourth review: a denied word is a fact by default

# Finding 1: practical facts in none of the closed lists. Each sentence must come back byte for byte, with no seed, with
# an unrelated seed, at a paragraph start and mid-paragraph.
_DENIED_FACTS = [
    "The spring water is not potable; it is glacier melt.",
    "This is not an insured activity. The views from the ridge are wide.",
    "The trail is not waymarked. It is an old trade path.",
    "The pass is not plowed; it is a summer route.",
    "This is not a paved route. The views over the valley are wide all the way.",
    "The track is not paved; it is an old mule track.",
    "The rooms are not air-conditioned; they are simple and clean.",
    "The dzong is not air-conditioned; it is a seventeenth-century fortress.",
    "The cave is not lit; it is a short crawl from the entrance.",
    "It is not a smoking area. The courtyard fills with pilgrims at dusk.",
    "The ferry is not covered; it is an open longboat.",
    "The farmhouse is not insulated; it is a three-storey rammed-earth house.",
]
UNRELATED_SEED = "Day 5: Punakha to Gangtey. Cash only in the valley; no ATM."


@pytest.mark.parametrize("sentences", _DENIED_FACTS)
@pytest.mark.parametrize("source", ["", UNRELATED_SEED])
@_BOTH_PLACES
def test_fourth_review_denied_practical_facts_are_kept(sentences, source, where):
    _unchanged(_placed(sentences, where), source)


@pytest.mark.parametrize("sentences, fact", [
    ("The spring water is not potable; it is glacier melt.", "potable"),
    ("This is not an insured activity. The views from the ridge are wide.", "insured"),
    ("The trail is not waymarked. It is an old trade path.", "waymarked"),
    ("It is not a smoking area. The courtyard fills with pilgrims at dusk.", "smoking"),
])
def test_fourth_review_refusal_names_the_denied_word(sentences, fact):
    out, log = rewrite(_placed(sentences, "mid paragraph"), _ctx(UNRELATED_SEED))
    assert out == _placed(sentences, "mid paragraph")
    assert any("drops a fact:" in line and fact in line for line in log), log


@pytest.mark.parametrize("text, expected", [
    # the denied clause's own content words are DENIED tokens; staged / framing nouns and the frame words are not
    ("The water is not potable.", [("water", True), ("potable", hygiene.DENIED)]),
    ("It carries no lifejackets.", [("lifejackets", hygiene.DENIED)]),
    ("The trail isn't waymarked, but the path is clear.", [("trail", True), ("waymarked", hygiene.DENIED),
                                                          ("path", True)]),
    ("The dzong is not a museum or a backdrop.", []),
    ("It is not a performance of tradition.", [("tradition", hygiene.DENIED)]),
    # 'not because …' and 'not just …' deny nothing
    ("We start early, not because it is remote.", [("start", True), ("early", True)]),
    ("They want to walk the valley, not just pass through it.", []),
    # the scope ends at the clause
    ("The room is not heated; the views are wide.", [("room", True), ("heated", True)]),
])
def test_fact_tokens_read_a_denials_own_words(text, expected):
    assert hygiene.fact_tokens(text) == expected


@pytest.mark.parametrize("before, after", [
    # staging and framing denials still go: the denied word is on the allowlist, or the affirmation repeats it
    ("The seasonal movement of the monk body is not a performance of tradition. It is tradition, still practised.",
     "The seasonal movement of the monk body is tradition, still practised."),
    ("The hall is not a relic. It hosts weddings.", "The hall hosts weddings."),
    ("The dzong is not a museum but a working monastery where monks still live.",
     "The dzong is a working monastery where monks still live."),
    ("The NHDC centre is a government enterprise, not a heritage attraction.",
     "The NHDC centre is a government enterprise."),
])
def test_fourth_review_staging_denials_still_go(before, after):
    out, _ = rewrite(_placed(before, "mid paragraph"), _ctx())
    assert out == _placed(after, "mid paragraph")


@pytest.mark.parametrize("sentences, fact", [
    # proposals the fourth-review reading refuses on purpose (each a denied open-class word)
    ("The hall is not grey but very green.", "grey"),
    ("The bridge is not the original. It was rebuilt.", "original"),
    ("Tsaatan settlements are not permanent villages. They are seasonal camps moved several times a year.",
     "permanent"),
    ("The reindeer are not raised primarily for meat; they are used for riding and as pack animals.", "meat"),
    ("The valley's story does not begin in the capital. It begins in the south, where rice terraces climb.",
     "capital"),
])
def test_fourth_review_denied_words_refuse_old_proposals(sentences, fact):
    out, log = rewrite(_placed(sentences, "mid paragraph"), _ctx())
    assert out == _placed(sentences, "mid paragraph")
    assert any("drops a fact:" in line and fact in line for line in log), log


# Finding 2: the last step before storage (final_pass / settle) reads the seed + Facts, and a repeat delete never
# takes a word out of the piece.
_HAA = ("Haa lakes — trek\nSource itinerary: Day 6: Trek from Haa to the lakes. Leeches are common from June to "
        "September. No phone coverage beyond Haa.")
_REPEATS = [
    ("We leave Haa early. The path to the lakes follows the stream through the forest.",
     "The path to the lakes follows the stream through the forest, thick with leeches."),
    ("We leave Haa early. The last stretch to the lakes follows the ridge.",
     "The last stretch to the lakes follows the ridge, crawling with leeches."),
    ("We cross the lake by boat. The longboat leaves the pier at nine every morning.",
     "The longboat leaves the pier at nine every morning and carries no lifejackets."),
    ("We climb to the hut. The last stretch to the hut follows the ridge.",
     "The last stretch to the hut follows the ridge, unroped and unmarked."),
]


def _repeat_piece(first: str, repeat: str) -> str:
    return f"## The Lakes\n\n{first} {repeat} Lunch is at the top.\n\n{CTA}"


@pytest.mark.parametrize("first, repeat", _REPEATS)
@pytest.mark.parametrize("seed", [_HAA, ""])
def test_fourth_review_repeat_that_adds_a_word_survives_autofix_and_final_pass(first, repeat, seed):
    text = _repeat_piece(first, repeat)
    hyg = hygiene.HygieneContext(seed=seed or "Lakes — trek", cta=CTA)
    fixed, _ = hygiene.autofix(text, hyg)
    assert fixed == text
    out, log, residue = text_integrity.final_pass(fixed, hyg)
    assert out == text and not log
    assert residue  # F13_repeat still sees the repeat: the repair, not a delete, fixes it
    assert text_integrity.settle(fixed, hyg) == text
    assert text_integrity.final_pass(fixed, None, cta=CTA, source=seed)[0] == text


def test_final_pass_and_settle_forward_the_seed_to_dedupe(monkeypatch):
    seen = []
    real = text_integrity.dedupe

    def spy(text, ignore=frozenset(), cta=None, source=None):
        seen.append(source)
        return real(text, ignore, cta, source)

    monkeypatch.setattr(text_integrity, "dedupe", spy)
    hyg = hygiene.HygieneContext(seed=_HAA, cta=CTA)
    text = _repeat_piece(*_REPEATS[0])
    text_integrity.final_pass(text, hyg)
    text_integrity.settle(text, hyg)
    text_integrity.final_pass(text, hyg, source="explicit seed")
    assert seen == [hyg.source_text, hyg.source_text, "explicit seed"] and "Leeches" in seen[0]


@pytest.mark.parametrize("later, other, expected", [
    ("The ridge path is unroped and unmarked.", "The ridge path climbs.", ["unroped", "unmarked"]),
    ("The lake holds seventy percent of the water.", "The lake, holding seventy percent of the water.", []),
    ("This route rewards those who travel with patience.", "The route crosses the steppe.", []),
    ("The boat leaves at 9.", "The boat leaves at 8.", ["9"]),
])
def test_added_words(later, other, expected):
    assert hygiene.added_words(later, other) == expected


# ---------------------------------------------------------------- ages: explicit cue only (findings 5 and 7)

@pytest.mark.parametrize("source, claim, found", [
    # finding 5 — group sizes, discounts and counts
    ("Groups of 10 and over get 5% off the entry fee.", "Children aged 10 and up can join.", "aged 10 and up"),
    ("Parties of 12 or over must book ahead.", "Kids aged 12 and over will love it.", "aged 12 and over"),
    ("Tables for 8 and over need a reservation.", "Ideal for children aged 8 and up.", "aged 8 and up"),
    ("Orders of 5 and up ship free.", "Great for 5-year-olds.", "5-year-olds"),
    ("Groups of over 6 need a second guide.", "The visit suits children over 6.", "children over 6"),
    ("At least 8 of the dancers are novices.", "Kids aged 8 and up join the class.", "aged 8 and up"),
    # finding 7 — altitude, durations, head counts, ratings, prices
    ("Day 4 of the trip. Travellers above 4,000 m often feel the altitude on the pass.",
     "Great for 4-year-olds and up.", "4-year-olds"),
    ("Hikers over 3,500 m need a rest day.", "Children over 3 can ride the ponies.", "Children over 3"),
    ("Our guides have 15+ years of experience.", "Teens aged 15 and up can join.", "aged 15 and up"),
    ("The homestay family has hosted travellers for 10+ years.", "Kids aged 10 and over enjoy it.",
     "aged 10 and over"),
    ("The terraces were built by local families over 12 years.", "A 12-year-old can manage the walk.",
     "12-year-old"),
    ("Run by the same family for 12 years and over three generations.", "A 12-year-old can manage the walk.",
     "12-year-old"),
    ("Group discounts for groups of 6 and above.", "Suitable for children aged 6 and up.", "aged 6 and up"),
    ("Max participants: 12.", "Kids aged 12 and up will love the rafting.", "aged 12 and up"),
    ("Participants: 2-12", "Kids aged 12 and up will love the rafting.", "aged 12 and up"),
    ("Travellers: 2 to 14", "Teens aged 14 and over can kayak.", "aged 14 and over"),
    ("Riders 6 max per horse trek.", "Children aged 6 and up can ride.", "aged 6 and up"),
    ("Rated 4 and above by guests.", "Great for 4-year-olds.", "4-year-olds"),
    ("Trek grade 3 and above.", "Children over 3 can ride.", "Children over 3"),
    ("Price per person: adults USD 45, children 8", "Ideal for children aged 8 and up.", "aged 8 and up"),
])
def test_a_number_without_an_age_cue_is_not_a_known_age(source, claim, found):
    assert hygiene.unsupported_ages(claim, source) == [found]


@pytest.mark.parametrize("source, claim", [
    ("Minimum age is 12.", "Kids aged 12 and up will love the rafting."),
    ("Minimum age: 7.", "Kids aged 7 and up are welcome."),
    ("Not suitable for under-5s.", "Children under 5 stay at the camp."),
    ("Riders must be at least 14.", "Riders aged 14 and over may take the long loop."),
    ("Children 6–12 join the class.", "Kids aged 6 to 12 join the class."),
    ("Suitable for children from 8 years.", "Kids aged 8 and up join the class."),
    ("Open to children 7 and older.", "Kids aged 7 and up are welcome."),
    ("Suitable for 10 years and over.", "Kids aged 10 and over are welcome."),
    ("Recommended for travellers aged 10+.", "Kids aged 10 and over are welcome."),
    ("Riders must be 12 years old.", "A 12-year-old can ride."),
    ("Families with children of 8 years and over.", "Kids aged 8 and up join in."),
])
def test_an_explicit_age_cue_in_the_seed_keeps_the_age_known(source, claim):
    assert hygiene.unsupported_ages(claim, source) == []


@pytest.mark.parametrize("source, claim, found", [
    # fourth review (1): clock-time and visiting-rule ranges after a person noun
    ("Day 3. Visitors from 10 to 12 can enter the assembly hall.", "Kids aged 10 and over will love it.",
     "aged 10 and over"),
    ("Students from 7 to 9 in the morning practise in the courtyard.", "Ideal for children aged 7 and up.",
     "aged 7 and up"),
    ("Kids from 9 to 5 daily.", "Kids aged 5 and up can join.", "aged 5 and up"),
    ("Pilgrims from 6 to 8 every evening circle the chorten.", "Kids aged 6 and up can join.", "aged 6 and up"),
    # fourth review (2): a thing's age written 'aged N (years)', and eras
    ("Day 5: Taste a single malt aged 12 years at the distillery.", "A 12-year-old can manage the walk.",
     "12-year-old"),
    ("Sample Pu'er tea aged 15 years in the cellar.", "Great for 15-year-olds.", "15-year-olds"),
    ("Rice wine aged 3 years.", "Great for 3-year-olds.", "3-year-olds"),
    ("Kimchi aged 2 years.", "Great for 2-year-olds.", "2-year-olds"),
    ("Sake aged 10 years and over.", "Kids aged 10 and over will love it.", "aged 10 and over"),
    ("Visit a temple aged 12 years.", "A 12-year-old can manage the walk.", "12-year-old"),
    ("Whisky aged 12.", "A 12-year-old can manage the walk.", "12-year-old"),
    ("The Middle Ages 12 monks lived here.", "A 12-year-old can manage the walk.", "12-year-old"),
    ("The Stone Age 8 caves.", "Great for 8-year-olds.", "8-year-olds"),
    ("Group of travellers 2–12.", "Kids aged 12 and up will love the rafting.", "aged 12 and up"),
])
def test_fourth_review_non_age_numbers_are_not_known_ages(source, claim, found):
    assert not hygiene._source_ages(source)
    assert hygiene.unsupported_ages(claim, source) == [found]
    assert hygiene.unsupported_ages(claim, "") == [found]


@pytest.mark.parametrize("source, claim", [
    ("Children from 6 to 12 years join the class.", "Kids aged 6 to 12 join the class."),
    ("Children from 6 to 12.", "Kids aged 6 to 12 join the class."),
    ("Ages 6–12.", "Kids aged 6 to 12 join the class."),
    ("Kids aged 7 and up are welcome.", "Kids aged 7 and up are welcome."),
    ("Children aged 8 years and over.", "Kids aged 8 and up join in."),
    ("Minimum age: travellers must be aged 10 years or over.", "Kids aged 10 and over are welcome."),
])
def test_fourth_review_explicit_age_cues_still_count(source, claim):
    assert hygiene.unsupported_ages(claim, source) == []


def test_a_things_age_in_the_output_is_not_an_age_claim():
    assert hygiene.unsupported_ages("Taste a single malt aged 12 years at the distillery.", "") == []
    assert hygiene.unsupported_ages("For families with children aged 12 and up.", "") == ["aged 12 and up"]


def test_a_measurement_in_the_output_is_not_an_age_claim():
    assert hygiene.unsupported_ages("Hikers over 30 km a day need a rest.", "") == []
    assert hygiene.unsupported_ages("Travellers above 4,000 m feel the altitude.", "") == []


# ---------------------------------------------------------------- fifth review (round 3 ship criterion, S252)

GUESTHOUSE = "We leave the guesthouse after breakfast. "
TANGO_SEED = "Source itinerary: Day 2: Tango monastery. Photography is not permitted inside the temples."
CAMP_SEED = "Source itinerary: Camp water is untreated stream water; bring purification tablets."


def _after_guesthouse(sentences: str, where: str) -> str:
    return _blog(sentences) if where == "paragraph start" else _blog(GUESTHOUSE + sentences)


# Finding 1: the staging exemption holds only in the copula frame; a ', not X' tail after a lexical verb, a modal or
# a preposition, a place / time contrast and an inline pair keep their denied word as a fact.
@pytest.mark.parametrize("sentences, sources", [
    ("You can sketch the murals, not photograph them.", ["", TANGO_SEED]),
    ("The water is not treated; it is drawn straight from the stream.", ["", CAMP_SEED]),
    ("The inner chapel is for prayer, not photos.", ["", UNRELATED_SEED]),
    ("The co-op sells raw wool, not souvenirs.", ["", UNRELATED_SEED]),
    ("On the second morning you watch the rehearsal, not the performance.", ["", UNRELATED_SEED]),
    ("Breakfast is served in the kitchen, not in the garden.", ["", UNRELATED_SEED]),
    ("Children see the dance from the courtyard, not the stage.", ["", UNRELATED_SEED]),
    ("The dancers wear their own robes, not costumes.", ["", UNRELATED_SEED]),
])
@_BOTH_PLACES
def test_fifth_review_denied_words_outside_the_copula_frame_are_kept(sentences, sources, where):
    for source in sources:
        _unchanged(_after_guesthouse(sentences, where), source)


@pytest.mark.parametrize("text, kind", [
    ("You can sketch the murals, not photograph them.", hygiene.DENIED),
    ("The co-op sells raw wool, not souvenirs.", hygiene.DENIED),
    ("The inner chapel is for prayer, not photos.", hygiene.DENIED),
    ("Breakfast is served in the kitchen, not in the garden.", hygiene.DENIED),
    ("The water is not treated; it is drawn straight from the stream.", hygiene.DENIED),
    ("No photos inside the temple.", hygiene.DENIED),
])
def test_fifth_review_fact_tokens_read_the_frame(text, kind):
    assert kind in {k for _, k in hygiene.fact_tokens(text)}


@pytest.mark.parametrize("text", [
    "The NHDC centre is a government enterprise, not a heritage attraction.",
    "The dzong is not a museum or a backdrop.",
    "The craft is taught not as a museum object, but as living practice.",
    "The hour is set not by a calendar but by the forest.",
    "To visit their settlements is not to encounter a preserved exhibit.",
    "Such timing is no coincidence.",
    "This kind of visit does not present the village as a stage.",
])
def test_fifth_review_the_copula_frame_keeps_its_exemption(text):
    assert hygiene.DENIED not in {k for _, k in hygiene.fact_tokens(text)}


# Finding 2: 'not just / merely / simply X' asserts X; the denial is never cut alone or joined away.
@pytest.mark.parametrize("sentences", [
    "This is not just a glacier. It is the valley's only source of water in spring.",
    "It is not just a monastery. It is also the district court and the land registry.",
    "This is not simply a pond. It is the sacred lake where the oracle bathes.",
    "The temple is not merely a monastery. It is also the district court.",
])
@pytest.mark.parametrize("source", ["", UNRELATED_SEED])
@_BOTH_PLACES
def test_fifth_review_not_just_is_never_cut(sentences, source, where):
    _unchanged(_after_guesthouse(sentences, where), source)


# Finding 3: a 'This / That is …' sentence that names a concrete thing is an identification, kept unless the piece
# names it elsewhere.
@pytest.mark.parametrize("sentences", [
    "We pass a flat rock above the monastery. This is the sky burial site.",
    "A white post marks the turn. This is the cremation ground.",
    "We stop at a wide meadow. This is the herders' pasture.",
    "This is where the porters change shifts.",
])
@pytest.mark.parametrize("source", ["", UNRELATED_SEED])
@_BOTH_PLACES
def test_fifth_review_identifications_are_kept(sentences, source, where):
    _unchanged(_after_guesthouse(sentences, where), source)


def test_fifth_review_a_verdict_noun_named_elsewhere_may_go():
    out, log = rewrite(_blog("The pasture belongs to the herders. We stop at a wide meadow. This is the herders' "
                             "pasture. The grass is short."), _ctx())
    assert out == _blog("The pasture belongs to the herders. We stop at a wide meadow. The grass is short.")
    assert log == ["ai_tells: verdict line: 'This is the herders' pasture.'"]


# Finding 4: an aphorism-pair cut never takes out a subject the piece names nowhere else.
@pytest.mark.parametrize("pair", ["The old forge is not a relic. It works.", "The mill is not a ruin. It grinds."])
@pytest.mark.parametrize("source", ["", UNRELATED_SEED])
@_BOTH_PLACES
def test_fifth_review_cut_both_keeps_a_lone_subject(pair, source, where):
    _unchanged(_after_guesthouse(pair + " Lunch is packed for the trail.", where), source)


def test_fifth_review_cut_both_goes_when_the_subject_is_named_elsewhere():
    text = _blog("The forge smoke rises at dawn. The old forge is not a relic. It works. "
                 "Lunch is packed for the trail.")
    out, log = rewrite(text, _ctx())
    assert out == _blog("The forge smoke rises at dawn. Lunch is packed for the trail.")
    assert log == ["ai_tells: aphorism pair (denial + bare verb): 'The old forge is not a relic. It works.'"]


# Finding 5: a repeat that differs only by a quantity or frequency word stays.
FERRY = ("The ferry crosses to the island in the morning. Lunch is at a fish shack on the pier. The ferry crosses to "
         "the island every morning.")


@pytest.mark.parametrize("seed", ["", UNRELATED_SEED])
def test_fifth_review_a_frequency_word_keeps_the_repeat(seed):
    hyg = hygiene.HygieneContext(seed=seed)
    assert hygiene.autofix(FERRY, hyg)[0] == FERRY
    assert text_integrity.final_pass(FERRY, hyg)[0] == FERRY
    assert text_integrity.settle(FERRY, hyg) == FERRY


@pytest.mark.parametrize("later, other, expected", [
    ("The ferry crosses to the island every morning.", "The ferry crosses to the island in the morning.", ["every"]),
    ("Both boats still run daily.", "The boats run.", ["both", "still", "daily"]),
    ("The visits are all available as options.", "The visits are available as options.", []),  # floating 'all'
])
def test_fifth_review_quantity_words_count_in_added_words(later, other, expected):
    assert hygiene.added_words(later, other) == expected


# Finding 6: an 'aged N' in the piece is a person's age unless it follows an aged good or object.
PUNAKHA_SEED = "Punakha — Chimi Lhakhang\nSource itinerary: walk through rice fields to the temple. Day 3 of the trip."


@pytest.mark.parametrize("claim, found", [
    ("Grandkids aged 6 and up love the farmhouse lunch.", "aged 6 and up"),
    ("Grandchildren aged 8 and over can manage the climb.", "aged 8 and over"),
    ("Grandparents and grandchildren aged 6 and up share the walk.", "aged 6 and up"),
    ("Trekkers aged 12 and up will enjoy the terraces.", "aged 12 and up"),
    ("Beginners aged 10 and over are welcome.", "aged 10 and over"),
    ("Cyclists aged 14 and over can ride the valley road.", "aged 14 and over"),
    ("Rafters aged 12+ can run the river.", "aged 12+"),
    ("Novices aged 10 to 14 are welcome.", "aged 10 to 14"),
    ("Sons and daughters aged 9 and up can join.", "aged 9 and up"),
    ("Pilgrims aged 70 and over walk it each spring.", "aged 70 and over"),
    ("Best enjoyed by members aged 8 and over.", "aged 8 and over"),
    ("The walk is open to all aged 8 and over.", "aged 8 and over"),
    ("Suitable for aged 7+.", "aged 7+"),
    ("Kids love the cheese aged 8 and up.", "aged 8 and up"),  # an aged good, but a person word in the sentence
])
@pytest.mark.parametrize("source", ["", PUNAKHA_SEED])
def test_fifth_review_an_output_aged_n_is_an_age_by_default(claim, found, source):
    assert hygiene.unsupported_ages(claim, source) == [found]


@pytest.mark.parametrize("claim", ["Taste a single malt aged 12 years at the distillery.",
                                   "Sample Pu'er tea aged 15 years in the cellar.", "The barrels aged 10 years."])
def test_fifth_review_an_aged_good_is_still_a_things_age(claim):
    assert hygiene.unsupported_ages(claim, "") == []


def test_fifth_review_grand_forms_cue_a_seed_age():
    assert hygiene.unsupported_ages("Grandkids aged 8 and up love it.", "Grandchildren aged 8 and over join.") == []


# Finding 7: group sizes and space-grouped altitudes are never known ages.
@pytest.mark.parametrize("source, claim, found", [
    ("Families of 4 and over get 10% off the farmstay.", "Kids aged 4 and up love the farmstay.", "aged 4 and up"),
    ("Travellers 6 and up get a group discount.", "Children aged 6 and up can ride.", "aged 6 and up"),
    ("Participants 4-8.", "Ideal for 8-year-olds.", "8-year-olds"),
    ("Group discounts: travellers 2 to 14.", "Kids aged 2 to 14 welcome.", "aged 2 to 14"),
    ("Couples 2 to 4 share a room.", "Kids aged 2 to 4 welcome.", "aged 2 to 4"),
    ("Explorers 4 to 6 to a jeep.", "Kids aged 4 to 6 welcome.", "aged 4 to 6"),
    ("Hikers above 3 500 m need a rest day.", "Children aged 3 and up can join.", "aged 3 and up"),
    ("Travellers over 4 000 m feel the altitude.", "Children aged 4 and up can join.", "aged 4 and up"),
    ("Travellers over 4\u202f000 m feel the altitude.", "Children aged 4 and up can join.", "aged 4 and up"),
])
def test_fifth_review_group_sizes_and_altitudes_are_not_ages(source, claim, found):
    assert not hygiene._source_ages(source)
    assert hygiene.unsupported_ages(claim, source) == [found]
    assert hygiene.unsupported_ages(claim, "") == [found]


@pytest.mark.parametrize("source, claim", [
    ("Children 6–12 join the class.", "Kids aged 6 to 12 join the class."),
    ("Open to children 7 and older.", "Kids aged 7 and up are welcome."),
    ("Travellers aged 6 to 12 join the class.", "Kids aged 6 to 12 join the class."),
    ("Participants aged 8 to 14.", "Kids aged 8 and up join in."),
    ("Minimum age: travellers 10 and over.", "Kids aged 10 and over are welcome."),
])
def test_fifth_review_age_cues_on_a_head_count_noun_still_count(source, claim):
    assert hygiene.unsupported_ages(claim, source) == []


# ---------------------------------------------------------------- linear time

_N = 8000
_INPUTS = {
    "words": "word " * (_N // 5),
    "caps": "Paro " * (_N // 5),
    "digits": "7 " * (_N // 2),
    "hyphen-up": "warm-up " * (_N // 8),
    "fact-id": "[Fact id=" * (_N // 9),
    "sentences": "This is not the dry season. " * (_N // 28),
    "letters": "a" * _N,
    "spaces": " " * _N,
    "years-and": "7 years and " * (_N // 12),
    "children-of": "children of " * (_N // 12) + "8 years",
    "aged-of": "aged of " * (_N // 8) + "7",
    # fourth review: the denial scope, the repeat-word check and the age look-backs
    "denials": "The pass is not plowed; " * (_N // 24),
    "nots": "not " * (_N // 4),
    "no-commas": "no lifejackets, " * (_N // 16),
    "from-to": "Visitors from 10 to 12 " * (_N // 23),
    "aged-years": "a malt aged 12 years " * (_N // 21),
    "ages-n": "Middle Ages 12 " * (_N // 15),
    # fifth review: the frame look-arounds, the verdict noun phrases and the output-side aged-thing read
    "tails": "We sell wool, not souvenirs " * (_N // 28),
    "verdicts": "This is the sky burial site. " * (_N // 29),
    "aged-person": "Grandkids aged 6 and up " * (_N // 24),
    "aged-thing": "the malt aged 12 years " * (_N // 23),
    "head-count": "Families of 4 and over " * (_N // 23),
}


def _invariant_pass(s: str) -> float:
    seed = hygiene.source_forms(s)
    return min(_timed(lambda: (hygiene.fact_tokens(s, seed), hygiene.missing_facts([s], s[: len(s) // 2],
                                                                                    hygiene.word_forms(s), seed),
                                hygiene._keeps_facts(s, s, seed), ai_tells._fact_losers([], s, seed)))
               for _ in range(2))


def _repeat_pass(s: str) -> float:
    seed = hygiene.source_forms(s)
    counts = hygiene._word_keys(s)
    return min(_timed(lambda: (hygiene.added_words(s, s), hygiene._repeat_delete_ok(counts, s, s, seed, frozenset())))
               for _ in range(2))


# Growth ratio, not a wall-clock budget: CI runners are 2-3x slower than a laptop, so an absolute 50 ms line failed
# there on linear code (S252, CIS #1024). 4x the input must cost < 8x the time (+20 ms of timer noise); a quadratic
# path costs ~16x. The absolute ceiling only catches a pathological case.
@pytest.mark.parametrize("label", list(_INPUTS))
def test_the_invariant_check_is_linear(label):
    s = _INPUTS[label]
    for timed in (_invariant_pass, _repeat_pass):
        small, big = timed(s), timed(s * 4)
        assert big < 8 * small + 0.02, f"{label} {timed.__name__}: {small * 1000:.0f} ms -> {big * 1000:.0f} ms at 4x"
        assert big < 2.0, f"{label} {timed.__name__}: {big * 1000:.0f} ms at 32000 characters"


def test_the_invariant_check_scales_linearly():
    unit = "This is not the dry season at the Paro pier. "
    small, big = unit * 180, unit * 1440
    t_small = min(_timed(lambda: hygiene.missing_facts([small], "", hygiene.word_forms(small))) for _ in range(3))
    t_big = min(_timed(lambda: hygiene.missing_facts([big], "", hygiene.word_forms(big))) for _ in range(3))
    assert t_big < 16 * t_small + 0.01  # 8× the text: linear (≈8×), never quadratic (64×)


@pytest.mark.parametrize("label", list(_INPUTS))
def test_the_age_readers_are_linear(label):
    s = _INPUTS[label]
    # 5x CI slack: runners are 2-3x slower; a quadratic path costs seconds here
    assert min(_timed(lambda: (hygiene._source_ages(s), hygiene.unsupported_ages(s, s))) for _ in range(2)) < 0.5


def test_the_pass_with_many_refused_edits_stays_fast():
    text = "We camp here. This is not drinking water. Boil it first. " * 140   # ~8000 characters, 140 refusals
    # 5x CI slack (runners are 2-3x slower); the S252 quadratic refusal path took 1 h 42 m for the suite
    assert min(_timed(lambda: rewrite(text, _ctx())) for _ in range(2)) < 2.5


def test_dedupe_with_many_refused_repeats_stays_fast():
    """Fourth review: a refused repeat costs O(its own words) (``_TextWithout``), not O(text) per candidate."""
    names = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
    text = " ".join(f"The last stretch to the hut follows the ridge above the lake, unroped {names[i % 12]}{i // 12}x."
                    for i in range(100))   # ~8000 characters; every near-duplicate adds a word
    assert min(_timed(lambda: hygiene.dedupe(text)) for _ in range(2)) < 2.0


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t
