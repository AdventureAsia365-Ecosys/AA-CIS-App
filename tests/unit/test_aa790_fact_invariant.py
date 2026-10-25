"""AA-790 round 3 (S252) — the fact-preservation invariant and the explicit-cue age reading.

Three review rounds kept finding a new chain of rules that deleted a fact (a join the verdict rule then cut, a lone
denial that was itself the safety or access rule, a place / time choice folded away, a verdict that restated a seed
fact, an audience limit). The rules stay as proposers; ``ai_tells._accept`` (and the repeat deletes of
``hygiene.dedupe`` / ``autofix``) refuse any edit that removes a fact token the text after it no longer carries
(``hygiene.fact_tokens`` / ``missing_facts``). This file holds:

* exact-output tests for every sentence quoted in the second and third reviews of round 3;
* a generated property test: ≥ 300 sentences from denial / contrast / verdict frames × fact tokens of every class,
  with and without a seed, at a paragraph start and mid-paragraph, plus chained pairs; every fact token survives;
* linear-time checks for the invariant itself.

Every sentence is synthetic or the reviewer's own probe sentence, never a tenant's text.
"""
import itertools
import time

import pytest

from services.acp_content_writing import ai_tells, hygiene
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
    assert hygiene.missing_facts(["This is not the end of the climb."], "", after) == ["end"]
    # a name only needs to be somewhere in the text after the edit
    assert hygiene.missing_facts(["It does not begin in Paro."], "", after) == []


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
}


@pytest.mark.parametrize("label", list(_INPUTS))
def test_the_invariant_check_is_linear(label):
    s = _INPUTS[label]
    seed = hygiene.source_forms(s)
    best = min(_timed(lambda: (hygiene.fact_tokens(s, seed), hygiene.missing_facts([s], s[: _N // 2],
                                                                                   hygiene.word_forms(s), seed),
                               hygiene._keeps_facts(s, s, seed), ai_tells._fact_losers([], s, seed)))
               for _ in range(2))
    assert best < 0.05 * 2, f"{label}: {best * 1000:.0f} ms"  # four passes over 8000 characters


def test_the_invariant_check_scales_linearly():
    unit = "This is not the dry season at the Paro pier. "
    small, big = unit * 180, unit * 1440
    t_small = min(_timed(lambda: hygiene.missing_facts([small], "", hygiene.word_forms(small))) for _ in range(3))
    t_big = min(_timed(lambda: hygiene.missing_facts([big], "", hygiene.word_forms(big))) for _ in range(3))
    assert t_big < 16 * t_small + 0.01  # 8× the text: linear (≈8×), never quadratic (64×)


def test_the_pass_with_many_refused_edits_stays_fast():
    text = "We camp here. This is not drinking water. Boil it first. " * 140   # ~8000 characters, 140 refusals
    assert min(_timed(lambda: rewrite(text, _ctx())) for _ in range(2)) < 0.5


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t
