"""AA-789 — T10 gate recall tuning (Report 12 §1.3, S234 panel rounds r2-r4), revision 4: precision over recall.

Three independent S257 verifier rounds found false positives on CLEAN travel copy in the widened patterns, and every
widened pattern under F13_leak, F13_repeat, F17_trip_fact and F18_rarity feeds a gate that blocks and can hold a
tenant piece. Revision 4 keeps a widened pattern only when it is narrow, literal and fires on none of the clean
sentences of any round:
- F18_rarity: `trip_facts.AA789_RARITY` ('unlike almost anything', 'anything else on earth', '(most of | the rest of)
  the world has never / rarely <verb>').
- F15_voice (blocks on attempt 1 for one sentence repair, never holds): contrast frame 8 (closed list: 'ceremonial
  sentiment', 'scenic preference', 'frozen in time'), frame 9 (the 'Not X in. Y in.' pair) and two blog stock lines
  (`channel_spec.CROSS_CHANNEL_STOCK_PATTERNS`).
- F17 time idioms ('worn out by midday'): only removes findings.
Everything else of revisions 1-3 is reverted to `main`; the panel examples it caught are pinned below as KNOWN MISSES
(they need the planned post-repair integrity check, an LLM, not regex).

Fixtures quoting the panel are marked "quote" (all shorter than 8 words); the others are "synthetic, shape of …".
Negative cases: every clean sentence of the three verifier rounds — `verifier` (round 1), `reviewer` (round 2) and
ROUND3_CLEAN (round 3, 40 sentences with their seeds) — plus near-misses of our own.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import trip_facts as tf
from services.acp_shared.channel_spec import CROSS_CHANNEL_STOCK_PATTERNS, linkedin_spec
from tests.unit.test_aa768_s234_round3 import PUNAKHA_SEED, ROUTE_SEED, TERRA_AUDIENCE, TERRA_CTA

# the patterns this issue adds, run alone on the clean sentences
AA789_FRAME_INDICES = (8, 9)
AA789_RARITY_RE = re.compile(tf.AA789_RARITY, re.I)


def _frames(sentence: str) -> list[str]:
    return [f[2] for f in h.contrast_frames(sentence)]


def _aa789_frames(text: str) -> list[str]:
    return [f[2] for f in h.contrast_frames(text) if f[3] in AA789_FRAME_INDICES]


def _aa789_stock(text: str) -> list[str]:
    return [m.group(0) for sp in h.sentence_spans(text) for rx in CROSS_CHANNEL_STOCK_PATTERNS
            if (m := rx.search(sp["clean"]))]


def _trip(text: str, seed: str = ROUTE_SEED, channel: str = "blog") -> dict:
    return tf.find_trip_fact_issues(text, tf.TripFactContext(seed=seed, audience=TERRA_AUDIENCE, cta=TERRA_CTA,
                                                             channel=channel))


def _issues(text: str, seed: str = ROUTE_SEED) -> dict:
    return h.find_issues(text, h.HygieneContext(seed=seed, brand_name="Pretend Brand", cta=None))


# ---------------------------------------------------------------- severity flags are unchanged (S246 standing rule)

def test_gate_severity_flags_are_unchanged():
    """The flags are unchanged. The F15 frames this issue adds block on attempt 1 only and never hold."""
    assert qg.F15_STOCK_BLOCKING is True and qg.F17_BLOCKING is True and qg.F18_BLOCKING is True
    assert qg.F12_SCOPE_BLOCKING is False
    text = ("The yearly return is institutional, not ceremonial sentiment. "
            "The site was a founding argument, not a scenic preference.")
    attempt1 = qg.gate_voice(text, spec=None, stock_blocking=True)
    later = qg.gate_voice(text, spec=None, stock_blocking=False)
    assert not attempt1["passed"] and attempt1["blocking"]       # attempt 1: one sentence repair, as before
    assert not later["passed"] and not later["blocking"]          # attempt >= 2: a warn, never a hold
    assert qg.never_holds({"gate": "F15_voice", "passed": False, "blocking": True, "violations": ["x"]})


# ---------------------------------------------------------------- F15 contrast frames (§1.3 row 1)

@pytest.mark.parametrize("sentence", [
    "The yearly return is institutional, not ceremonial sentiment.",      # synthetic, shape of r4 #8
    "The location was a founding argument, not a scenic preference.",    # quote, r4 class 4 (Punakha)
    "It is a working monastery, not a village frozen in time.",           # synthetic, the closed-list cliché
    "Not squeezed in. Planned in.",                                        # quote, r3 #15
    "The dzong is not simply seen; it is entered.",                        # synthetic, r4 #8 (existing frame)
    "These aren't inconveniences — they're part of the rhythm.",          # synthetic, r4 #10 (existing frame)
])
def test_contrast_frame_shapes_are_found(sentence):
    assert _frames(sentence), sentence


@pytest.mark.parametrize("sentence", [
    "Accommodation is in a family-run guesthouse, not a large hotel.",     # verifier
    "Your guide meets you at the airport in Paro, not Thimphu.",           # verifier (proper noun)
    "Prices are per person, not per room.",                                 # verifier ('per')
    "Pack for wind, not rain.",                                             # verifier
    "The drive takes four hours, not two.",                                 # verifier (spelled number)
    "The camp does not have Wi-Fi; it does have a generator for charging phones.",   # verifier
    "The bus does not stop at the pass: it continues straight to Wangdue.",          # verifier
    "Not suitable for children under seven.",                               # verifier
    "Expect less driving, more walking on the third day.",                  # verifier
    "The visit is optional, not compulsory.",
    "Children aged 9, not 7, can join the ride.",
    "Rest before lunch, not after the long climb.",
    "The pools stay open, not least for the swimmers.",
    "Not far from the lake, the road climbs into the forest.",
    "Not all families choose the ride.",
    "Not included. Lunch is included.",
    "It doesn't rain much here, it is said.",
    "The guide is less than an hour away.",
    # revision 4: the shape rule of revision 3 is gone; these are known misses or clean facts
    "It is a working monastery, not a site frozen for visitors.",          # synthetic r4 #1: known miss
    "The dzong is a working monastery, not a museum.",                     # stock noun: known miss (dropped)
])
def test_contrast_near_misses_stay_clean(sentence):
    assert not _aa789_frames(sentence), sentence


def test_density_rule_still_decides_the_finding():
    one = "The return is institutional, not ceremonial sentiment. The pools are on the second day of the route."
    assert qg.gate_voice(one, spec=None)["passed"]              # 1 frame within 1 per 150 words: allowed
    two = one + " The site was a founding argument, not a scenic preference."
    r = qg.gate_voice(two, spec=None)
    assert any(v.startswith("contrast frame") for v in r["violations"])


def test_a_frame_inside_a_stock_quote_is_not_counted_twice():
    text = "Rest is built in. Not as filler. Not as an afterthought. It is the plan."
    r = qg.gate_voice(text, spec=linkedin_spec({"brand_name": "Pretend Brand"}))
    assert not any(v.startswith("contrast frame") for v in r["violations"])


# ---------------------------------------------------------------- F15 stock lines in blogs (§1.3 row 1)

@pytest.mark.parametrize("sentence,line", [
    ("The landscape does not perform for visitors.", "does not perform for visitors"),   # quote, r4 #15 (blog)
    ("The valley does not perform for visitors. It simply continues.", "does not perform for visitors"),  # r4 #15
    ("The herding families simply tolerates them.", "tolerates them"),                   # synthetic, r4 #17 quote
    ("The herding economy is a detail worth pausing on.", "worth pausing on"),           # synthetic, r4 #5 (existing)
])
def test_returning_stock_lines_are_found_on_every_channel(sentence, line):
    assert [ln for ln, _ in h.template_lines(sentence)] == [line]
    assert any(v.startswith("stock line") for v in qg.gate_voice(sentence + " Day two is long.", spec=None)
               ["violations"])


@pytest.mark.parametrize("sentence", [
    "The dzong tolerates visitors in the outer courtyard during the festival.",          # verifier
    "The troupe does not perform for visitors outside the festival.",                    # an operating schedule
    "The dancers do not perform the cham on Mondays.",
    "In winter the troupe does not perform for visitors.",                               # a season qualifier
    "On festival days the troupe does not perform for visitors.",
    "The valley does not perform. It simply continues.",              # bare 'does not perform': known miss
    "An adult reindeer tolerates them only when they approach slowly.",                  # not sentence-final
])
def test_blog_stock_line_near_misses(sentence):
    assert _aa789_stock(sentence) == [], sentence


def test_linkedin_only_stock_lines_stay_linkedin_only():
    assert qg.gate_voice("That is not a backdrop. It is the reason for the trip.", spec=None)["passed"]
    li = linkedin_spec({"brand_name": "Pretend Brand"})
    assert not qg.gate_voice("The dzong tolerates visitors in the outer courtyard. Day two is long.", spec=None)[
        "violations"]
    assert qg.gate_voice("The dzong tolerates visitors. Day two is long.", spec=li)["violations"]  # LinkedIn as before


# ---------------------------------------------------------------- F17 (§1.3 row 7): only the time idioms stay

def test_time_idiom_is_not_a_schedule():
    assert _trip("Children are rarely worn out by midday here.")["trip_fact"] == []      # synthetic, r4 #11 FP
    assert any("gives a time" in v for v in _trip("You reach the Tsaatan settlements by midday.")["trip_fact"])


@pytest.mark.parametrize("sentence", [
    # KNOWN MISSES (revision 4 reverted these to main: each widening fired on clean copy in a verifier round)
    "Families cross the lake by boat, and walk the shore, and optionally ride horses.",  # synthetic, r4 #14
    "Families travel by boat and on foot, with only horse riding optional.",            # synthetic, r4 #5
    ("Hours on the lake — by boat — and walks along the shore are part of how this stage of the journey unfolds, "
     "with horse riding available as an extra."),                                       # synthetic, r4 #5
    "Visiting the Tsaatan herders is optional on day five.",                            # synthetic, r4 #18
    "The Tsaatan visit is optional.",                                                   # synthetic, r4 #18
    "The caldera walk is not a technical climb.",                                       # quote, r4 #14
    "Lunch is at Mörön, the provincial capital.",                                       # synthetic, r4 fix 2
    "A stop in Mörön breaks the drive.",                                                # synthetic, r3
])
def test_f17_panel_examples_are_known_misses(sentence):
    found = _trip(sentence)["trip_fact"]
    assert not any(k in v for v in found for k in ("as part of the plan", "calls Tsaatan", "'technical'",
                                                   "but the source says near it")), found


@pytest.mark.parametrize("sentence", [
    "Optional activities include boat rides on the water, horseback riding and nomad visits.",
    "Boat rides on the water, horse riding and trekking are all optional.",
    "The day includes the boundary walk and, if you wish, a boat ride on the water.",
    "Across the lake days, active options include trekking, horseback riding and boat rides on the water.",
    "Boat rides, horse riding and visits to nomadic families can be arranged on request.",   # reviewer
    "Supplies for the lakeside camps still arrive by boat in summer.",                     # reviewer
    "The camp's own supplies come in by boat once a week.",
    "Supplies arrive by boat, which is part of life on the lake.",
    "At the Tsaatan settlements on day five, children can ride a reindeer if they wish.",   # verifier
    "At the Tsaatan settlements, milking the reindeer is optional.",                        # verifier
    "Reindeer riding on the Tsaatan day is optional.",
    "Sod Camp staff can arrange optional excursions to the mineral spring.",                # reviewer
    "An optional stop at Uran Togoo's crater rim viewpoint is possible for older children.",  # reviewer
    "Visiting the mineral spring near Sod Camp is optional.",
    "The hike is strenuous in places, so good boots matter.",                               # verifier
    "No technical climbing gear is needed on the caldera walk.",                            # reviewer
    "Helmets are provided for the short technical scramble below the summit.",              # reviewer
    "After lunch, the drive continues to the airport in Mörön for those flying back.",      # reviewer
    "The bus does not stop in Mörön.",
])
def test_f17_near_misses_stay_clean(sentence):
    found = _trip(sentence)["trip_fact"]
    assert not any(k in v for v in found for k in ("as part of the plan", "calls ", "pace or rest promise",
                                                   "but the source says near it")), found


# ---------------------------------------------------------------- F18 overclaims (§1.3 row 5)

@pytest.mark.parametrize("sentence", [
    "The forest here is unlike almost anything else on earth.",                # quote, r4 #16
    "It is a landscape most of the world has never considered.",               # quote, r4 #17
    "A way of life that most of the world has never seen survives here.",      # synthetic, r3 #17 quote
    "The rest of the world has rarely heard of this valley.",                  # synthetic, r4 #17 shape
    "Nothing here resembles anything else on earth.",                          # synthetic, r4 #16 shape
])
def test_overclaims_are_found(sentence):
    assert _trip(sentence)["rarity"], sentence


@pytest.mark.parametrize("sentence", [
    "This is one of the most coherent family routes available.",               # synthetic, r4 #16: known miss
    "The lake holds a significant fraction of the world's fresh water.",        # synthetic, r4 #19: known miss
    "The scale of the lake is hard to overstate.",                             # r4 #4 shape: known miss
])
def test_f18_panel_examples_are_known_misses(sentence):
    assert _trip(sentence)["rarity"] == [], sentence


@pytest.mark.parametrize("sentence", [
    "Travellers from most of the world can get a visa on arrival in Mongolia.",            # verifier
    "A considerable share of the walk is on flat ground beside the lake.",                 # verifier
    "A substantial portion of the day is spent on the road between camps.",                # verifier
    "A significant share of the trip is spent outdoors.",
    "The importance of sun protection at this altitude is hard to overstate.",             # reviewer
    "The Tsaatan are one of the most distinct ethnic groups you can meet.",    # a superlative the source makes
    "Unlike most ger camps on the route, Sod Camp faces a mountain range.",
])
def test_rarity_near_misses(sentence):
    seed = ROUTE_SEED + "\n- The Tsaatan are one of Mongolia's most distinct ethnic groups."
    assert _trip(sentence, seed=seed)["rarity"] == [], sentence


# ---------------------------------------------------------------- F13 (§1.3 row 4): all reverted, known misses

@pytest.mark.parametrize("text,seed", [
    ("Punakha Dzong is still a living religious seat. The courtyards are open on day four. "
     "The dzong continues to function as a living religious institution.", PUNAKHA_SEED),   # synthetic, r4 #1
    ("## Ruins\n\nOne old wall, one year, one fact.", ROUTE_SEED),                        # synthetic, r2 #19
    ("## Mörön\n\nMörön has one phonetic approximation in English that locals dislike.", ROUTE_SEED),  # r3 #14
    ("## Punakha\n\nThe two named rivers meet below the dzong walls.", ROUTE_SEED),          # r3 #7 shape
    ("## Into the Taiga\n\nAccess requires a multi-hour horse or reindeer ride.", ROUTE_SEED),  # r4 #16
])
def test_f13_panel_examples_are_known_misses(text, seed):
    r = _issues(text, seed)
    assert r["leak"] == [] and r["repeat"] == [], r


@pytest.mark.parametrize("text", [
    ("The evening brings a local musical performance in the camp. Dinner is mutton stew and bread. "
     "Children can try the local musical instruments in the camp afterwards."),                     # verifier
    "The camp is a traditional nomadic ger camp. The lodge is a traditional nomadic guesthouse.",
    ("The first camp is a seasonal nomadic site. Dinner is at seven. "
     "The second camp is a seasonal nomadic settlement."),                                          # reviewer
    ("The monastery remains a working religious community. "
     "It is also a working religious school for young monks."),                                    # reviewer
    "## Practical notes\n\nEntry fees are included in the price.",                                  # verifier
    "## Uran Togoo\n\nThe approach road is paved for most of the way.",                             # verifier
    "## Punakha Dzong\n\nAdmission tickets for the dzong are arranged by your guide.",              # verifier
    "## Paro\n\nThe trail map at the gate is only a loose approximation of the real path.",         # verifier
    "## Bhutan\n\nThe two named passes on the road, Dochula and Pele La, both have prayer flags.",  # verifier
    "## Visas\n\nMake sure your name is correctly spelled on the visa application, exactly as in your passport.",
    "## Paro\n\nThe Dzongkha greeting kuzuzangpo is written here as a phonetic approximation.",     # reviewer
    "## Camp\n\nEach family gets one tent, one stove and one lantern.",
])
def test_f13_near_misses_stay_clean(text):
    r = _issues(text)
    assert r["leak"] == [] and r["repeat"] == [], r


# ---------------------------------------------------------------- round 3: 40 clean sentences, no AA-789 pattern


_SEED_V = """Ha Long Bay — overnight cruise among limestone karsts
Location: Ha Long Bay, Quang Ninh Province, Vietnam
Day 2 of the trip
Source itinerary: Board a traditional junk for an overnight cruise. Optional activities include kayaking through Luon Cave and a cooking class on board. Lunch is served on board.

Yen Duc village — water puppet show
Location: Yen Duc, Quang Ninh Province, Vietnam
Day 3 of the trip
Source itinerary: Stop for lunch near Yen Duc village on the drive back to Hanoi; water puppet show at the village."""

_SEED_N = """Thorong La — trek over the pass
Location: Thorong La, Manang District, Nepal
Day 9 of the trip
Source itinerary: Cross Thorong La (5,416 m) on a non-technical trek; the main challenge is altitude. Two acclimatisation days in Manang precede the crossing."""

_SEED_B = """Lake Baikal — cross the ice by hovercraft
Location: Lake Baikal, Irkutsk Oblast, Russia
Day 3 of the trip
Source itinerary: Cross the frozen lake by hovercraft to Olkhon Island.
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- Lake Baikal holds about 20 percent of the world's unfrozen fresh surface water.
- It is the deepest lake in the world, at 1,642 m."""

_SEED_G = """Elephant sanctuary — half-day visit
Location: Mae Taeng, Chiang Mai Province, Thailand
Day 2 of the trip
Source itinerary: Half-day visit to an observation-only elephant sanctuary for retired logging elephants; no riding or bathing. Lunch near Mae Taeng market."""

ROUND3_CLEAN = [
 ("blog", _SEED_G, "The sanctuary is a rescue centre for retired logging elephants, not commercial entertainment."),
 ("blog", ROUTE_SEED, "Tipping your driver and guide is customary in Mongolia, not a legal obligation."),
 ("blog", PUNAKHA_SEED, "Bhutan's Sustainable Development Fee is paid to the government, not a personal commission."),
 ("blog", _SEED_N, "Travel insurance with helicopter evacuation cover is a condition of booking this trek, not an optional addition."),
 ("blog", PUNAKHA_SEED, "Photography inside the assembly hall is forbidden by monastery rule, not a seasonal restriction."),
 ("blog", _SEED_G, "Unlike at the old trekking camps, the herd at this sanctuary does not perform."),
 ("blog", ROUTE_SEED, "Reindeer are calm, but keep a respectful distance: an adult animal tolerates you only when you approach slowly."),
 ("blog", PUNAKHA_SEED, "The masked dancers rehearse in the dzong courtyard for weeks before the festival; outside the festival days the troupe does not perform."),
 ("blog", PUNAKHA_SEED, "Gangtey Goemba is an active monastery with monks in residence, not a museum, so visiting hours follow the prayer schedule."),
 ("blog", ROUTE_SEED, "Rangers check every permit at the national park gate at Khatgal; the check is strict, not a formality."),
 ("blog", ROUTE_SEED, "Families explore the shore on foot or by boat, depending on the weather and on which optional activities they choose."),
 ("blog", ROUTE_SEED, "If the lake is calm on day five, families can head out by boat; horseback riding across the hills is the other optional choice."),
 ("blog", ROUTE_SEED, "We reach Khatgal by road from Mörön, and from there the optional boat rides set off from the jetty."),
 ("blog", ROUTE_SEED, "Guests travel between camps by four-wheel-drive van, and boat rides on the water can be added on day five for those who want them."),
 ("blog", ROUTE_SEED, "On the lake day, families walk the shoreline and forest paths, and anyone who wants to can take a boat out on the water."),
 ("blog", ROUTE_SEED, "Families travel by horse or reindeer for several hours to reach the Tsaatan camps, because no road runs into the taiga."),
 ("blog", _SEED_V, "Guests who would rather stay dry can see Luon Cave from a bamboo rowing boat instead of by kayak."),
 ("blog", _SEED_V, "The junk anchors in a sheltered bay for the night, and guests are free to swim, kayak or join the cooking class on board."),
 ("blog", ROUTE_SEED, "The Tsaatan day is part of the plan, not an optional extra: it is why the route runs this far north."),
 ("blog", ROUTE_SEED, "Long-distance buses from Ulaanbaatar to Khatgal make a stop in Mörön, the provincial capital."),
 ("blog", ROUTE_SEED, "Mörön's small airport has scheduled flights to Ulaanbaatar, which is useful if a family needs to cut the trip short."),
 ("blog", ROUTE_SEED, "## FAQ\n\n**How do you pronounce Mörön?**\n\nRoughly 'MUR-urn'. Signs and tickets often show Muren, which is only a phonetic approximation of the Mongolian."),
 ("blog", ROUTE_SEED, "## Mörön\n\nThe drive north passes through Mörön, the capital of Khövsgöl province, where the road turns towards the lake."),
 ("blog", ROUTE_SEED, "## Practical notes\n\nCheck that Mörön is correctly spelled on your domestic flight ticket; some booking sites list the airport as Muren."),
 ("linkedin", ROUTE_SEED, "One driver, one guide, one pick-up point.\n\nThat is how we keep arrival day in Ulaanbaatar calm for families with young children."),
 ("blog", PUNAKHA_SEED, "## Day 2\n\nThe drive is a short scenic transfer from Thimphu to Punakha over Dochula Pass.\n\n## Day 5\n\nThe drive is a short scenic run back to Paro for the flight home."),
 ("blog", _SEED_V, "Lunch on day two is served on board, and the last stop is in Yen Duc village for the water puppet show."),
 ("blog", ROUTE_SEED, "Lake Khövsgöl is a vast freshwater lake near the Russian border. It holds an estimated 70 percent of Mongolia's fresh water. The lake is a vital ecological reserve, protected as a national park."),
 ("blog", ROUTE_SEED, "The Tsaatan are among the most distinctive communities you can meet in northern Mongolia, a small group of a few hundred people."),
 ("blog", _SEED_B, "Lake Baikal holds about 20 percent of the world's unfrozen fresh surface water, a significant share of the planet's supply."),
 ("blog", ROUTE_SEED, "Mongolian place names have no single agreed romanisation, and the world has yet to settle on one, so you will see Mörön written several ways."),
 ("blog", ROUTE_SEED, "Unlike most ger camps on the route, Sod Camp faces a mountain range with trekking access and a mineral spring."),
 ("blog", _SEED_N, "The Annapurna Circuit is not a technical trek, but Thorong La sits at 5,416 m, so the plan builds in two acclimatisation days in Manang."),
 ("blog", _SEED_N, "## FAQ\n\n**Do we need climbing gear for Thorong La?**\n\nNo. No technical climbing gear is needed; trekking poles and microspikes for ice near the pass are enough."),
 ("blog", PUNAKHA_SEED, "Punakha Dzong served as the capital of Bhutan until 1955, when the government moved to Thimphu; today it is the winter residence of the Je Khenpo."),
 ("linkedin", ROUTE_SEED, "Families ask us whether Lake Khövsgöl is worth the long drive north.\n\nIt holds an estimated 70 percent of Mongolia's fresh water, and the boundary between steppe and taiga runs along its shore.\n\nBoat rides and horseback riding are optional on day five, so each family picks what suits its children."),
 ("blog", _SEED_G, "Lunch is at a noodle shop near Mae Taeng market, a ten-minute drive from the sanctuary."),
 ("blog", _SEED_G, "The sanctuary is observation-only, so there is no riding or bathing, and children watch the elephants feed from a raised platform."),
 ("blog", ROUTE_SEED, "The trail is a gentle gradual ascent through larch forest. Lunch is a picnic at the rim. The trail is a gentle gradual descent on the far side."),
 ("blog", PUNAKHA_SEED, "## FAQ\n\n**Is Punakha Dzong a museum?**\n\nNo. It is a working dzong: the district administration and the Central Monk Body both use it, and the monks winter here."),
]


@pytest.mark.parametrize("channel,seed,text", ROUND3_CLEAN)
def test_round3_clean_sentences_fire_no_aa789_pattern(channel, seed, text):
    """Round 3 of the S257 verification (40 clean sentences, own seeds). The gate-level check (branch vs main, every
    gate) is the harness in the notes; here each pattern this issue adds runs alone."""
    assert _aa789_frames(text) == [], text
    assert _aa789_stock(text) == [], text
    assert not AA789_RARITY_RE.search(text), text


@pytest.mark.parametrize("channel,seed,text", ROUND3_CLEAN)
def test_round3_clean_sentences_get_no_aa789_finding(channel, seed, text):
    """…and none of the findings the reverted patterns produced comes back on them."""
    trip = tf.find_trip_fact_issues(text, tf.TripFactContext(seed=seed, audience=TERRA_AUDIENCE, cta=TERRA_CTA,
                                                             channel=channel))
    issues = h.find_issues(text, h.HygieneContext(seed=seed, brand_name="Pretend Brand", cta=TERRA_CTA))
    flat = trip["trip_fact"] + trip["rarity"] + issues["leak"] + issues["repeat"]
    for gone in ("presents 'boat", "puts the stop", "puts the lunch", "but the source says near it",
                 "tells the same thing again", "'phonetic approximation'", "'correctly spelled'",
                 "leaks the writer's instructions ('One", "significant share", "the world has yet to",
                 "among the most distinctive"):
        assert not any(gone in v.rsplit(": '", 1)[0] for v in flat), (gone, text)   # the problem, not the quote


@pytest.mark.parametrize("text,where", [
    ("Boat rides and horse riding cost extra.", "trip_fact"),           # own near-miss of revision 3
    ("Our day-two lodge is a comfortable rural guesthouse. Dinner is at seven. "
     "Our day-four lodge is a comfortable rural farmstay.", "repeat"),  # reviewer (round 2)
])
def test_pre_existing_main_findings_are_unchanged(text, where):
    """Two near-misses of revisions 2-3 that `main` already flags through older checks (optional_as_fixed's hedge
    list has no 'cost extra'; near_duplicates reads the two lodge sentences as one). Revision 4 reverts to `main`, so
    they fire exactly as on `main`; fixing them is not recall tuning and is left out of this PR."""
    found = _trip(text)["trip_fact"] if where == "trip_fact" else _issues(text)["repeat"]
    assert found, text


# ---------------------------------------------------------------- F16 answer addresses the question (§1.3 row 8)

@pytest.mark.parametrize("question,answer,why", [
    ("Who are the Tsaatan?", "The road north takes several days and the region sits high.", "no_subject"),  # r4 #5
    ("What do the Tsaatan use their reindeer for?",                                                          # r4 #19:
     "Unlike other herding peoples, the Tsaatan do not mainly keep them for meat.", "negative_only"),     # synthetic
])
def test_faq_non_answers_are_already_caught(question, answer, why):
    assert qg.faq_answer_problem(question, answer, ROUTE_SEED) == why


@pytest.mark.parametrize("question,answer", [                 # verifier: none of these may be dropped
    ("What do the Tsaatan use their reindeer for?", "The Tsaatan ride their reindeer and milk them."),
    ("What do the Tsaatan use their reindeer for?", "Tsaatan reindeer give riding, milk and transport."),
    ("What is the dining ger at Sod Camp used for?", "The dining ger at Sod Camp hosts meals and card games."),
    ("How cold does it get at Lake Khövsgöl?", "At Lake Khövsgöl nights can drop below zero even in September."),
    ("How cold does it get at Lake Khövsgöl?", "Lake Khövsgöl sits around −10 °C at night in early October."),
    ("What is the weather like at Lake Khövsgöl in June?", "Lake Khövsgöl has clear skies and crisp nights."),
    ("Can I use my credit card at Sod Camp for payments?", "No. Sod Camp takes cash only."),
])
def test_faq_real_answers_pass(question, answer):
    assert qg.faq_answer_problem(question, answer, ROUTE_SEED) is None


# ---------------------------------------------------------------- the offline replay (no-new-fire evidence)

def _replay_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "aa789_recall_replay.py"
    spec = importlib.util.spec_from_file_location("aa789_recall_replay", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_replay_reduces_findings_to_kinds_without_copy():
    mod = _replay_module()
    assert mod._kind("contrast frame 'x, not y' (2 in 40 words) — say it: 'x, not y.'") == "contrast frame"
    assert mod._kind("presents 'boat rides' as part of the plan: 'Sentence.'") == "presents"


def test_replay_compare_lists_new_fires_on_clean_linkedin(tmp_path, capsys):
    mod = _replay_module()
    base = {"1": {"channel": "linkedin", "panel": "light", "gates": {}},
            "2": {"channel": "linkedin", "panel": "heavy", "gates": {}}}
    new = {"1": {"channel": "linkedin", "panel": "light", "gates": {"F15_voice": [["contrast frame", 1]]}},
           "2": {"channel": "linkedin", "panel": "heavy", "gates": {"F18_rarity": [["makes a rarity claim", 1]]}}}
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "n.json").write_text(json.dumps(new))
    mod.compare(str(tmp_path / "b.json"), str(tmp_path / "n.json"), clean_linkedin=True)
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["n"] == "1" and rows[0]["new_failing_gates"] == ["F15_voice"]
    assert rows[-1] == {"pieces_with_new_findings": 1}
