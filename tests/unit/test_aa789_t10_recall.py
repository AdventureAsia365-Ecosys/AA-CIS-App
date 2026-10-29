"""AA-789 — T10 gate recall tuning (Report 12 §1.3, S234 panel rounds r2-r4).

Positive cases: each defect the S234 panel quoted in reports 03 / 04 / 07 (root
`docs/audits/2026-10-13-S234-t-series-rounds/`) that the deterministic gates missed. Report 12 §1.3 is a table of
classes without sentences, so the quotes come from the round reports; a comment names the report row. Fixtures that
quote the panel word for word are marked "quote"; the others are marked "synthetic, shape of …": a reworded sentence
with the same shape (never stored copy, AA-919 golden-text guard).

Negative cases: every clean sentence the S257 independent verifier ran through the first version and saw flagged
(`verifier`), every clean sentence the second reviewer found flagged by revision 2 (`reviewer`), plus near-misses
of our own. The widened patterns feed gates that block and can hold (F13_leak,
F13_repeat, F17_trip_fact, F18_rarity), so a pattern that could not be made precise was dropped (the notes list them)
and its panel example is pinned here as a known miss.

The no-new-fire check on the panel-clean LinkedIn pieces is the offline replay `scripts/aa789_recall_replay.py`
(real copy stays out of the repo); its compare logic is tested at the end of this file.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import trip_facts as tf
from services.acp_shared.channel_spec import linkedin_spec
from tests.unit.test_aa768_s234_round3 import PUNAKHA_SEED, ROUTE_SEED, TERRA_AUDIENCE, TERRA_CTA


def _frames(sentence: str) -> list[str]:
    return [f[2] for f in h.contrast_frames(sentence)]


def _trip(text: str, seed: str = ROUTE_SEED, channel: str = "blog") -> dict:
    return tf.find_trip_fact_issues(text, tf.TripFactContext(seed=seed, audience=TERRA_AUDIENCE, cta=TERRA_CTA,
                                                             channel=channel))


def _issues(text: str, seed: str = ROUTE_SEED) -> dict:
    return h.find_issues(text, h.HygieneContext(seed=seed, brand_name="Pretend Brand", cta=None))


# ---------------------------------------------------------------- severity flags are unchanged (S246 standing rule)

def test_gate_severity_flags_are_unchanged():
    """The flags are unchanged; note that F13_leak / F13_repeat / F17 / F18 already block and can hold, so every
    pattern widened under them blocks too (hence the precision cases below)."""
    assert qg.F15_STOCK_BLOCKING is True and qg.F17_BLOCKING is True and qg.F18_BLOCKING is True
    assert qg.F12_SCOPE_BLOCKING is False
    text = ("The yearly return is institutional, not ceremonial sentiment. "
            "The site was a founding argument, not a scenic preference.")
    attempt1 = qg.gate_voice(text, spec=None, stock_blocking=True)
    later = qg.gate_voice(text, spec=None, stock_blocking=False)
    assert not attempt1["passed"] and attempt1["blocking"]       # attempt 1: one sentence repair, as before
    assert not later["passed"] and not later["blocking"]          # attempt >= 2: a warn, never a hold


# ---------------------------------------------------------------- F15 contrast frames (§1.3 row 1)

@pytest.mark.parametrize("sentence", [
    "The yearly return is institutional, not ceremonial sentiment.",      # synthetic, shape of r4 #8
    "The location was a founding argument, not a scenic preference.",    # quote, r4 class 4 (Punakha)
    "It is a working monastery, not a site frozen for visitors.",         # synthetic around the r4 #1 quote
    "The dzong is a working monastery, not a museum.",                     # synthetic, stock dismissal noun
    "Not squeezed in. Planned in.",                                        # quote, r3 #15
    "The dzong is not simply seen; it is entered.",                        # synthetic, r4 #8 'not simply seen'
    "These aren't inconveniences — they're part of the rhythm.",          # synthetic, r4 #10 (existing)
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
])
def test_contrast_near_misses_stay_clean(sentence):
    assert not _frames(sentence), sentence


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
    ("The valley does not perform. It simply continues.", "does not perform"),           # synthetic, r4 #15
    ("The herders simply tolerates them.", "tolerates them"),                            # synthetic, r4 #17 quote
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
])
def test_blog_stock_line_near_misses(sentence):
    assert h.template_lines(sentence) == []


def test_linkedin_only_stock_lines_stay_linkedin_only():
    assert qg.gate_voice("That is not a backdrop. It is the reason for the trip.", spec=None)["passed"]
    li = linkedin_spec({"brand_name": "Pretend Brand"})
    assert not qg.gate_voice("The dzong tolerates visitors in the outer courtyard. Day two is long.", spec=None)[
        "violations"]
    assert qg.gate_voice("The dzong tolerates visitors. Day two is long.", spec=li)["violations"]  # LinkedIn as before


# ---------------------------------------------------------------- F17 'optional' flips (§1.3 row 7)

@pytest.mark.parametrize("sentence", [
    "Families cross the lake by boat, and walk the shore, and optionally ride horses.",  # synthetic, r4 #14
    "Families travel by boat and on foot, with only horse riding optional.",          # synthetic, r4 #5
    "Families travel by boat and on foot, with horse riding on request.",       # a verbless hedge is the item's own
    "Families travel by boat, and horse riding can be arranged.",               # the boat item is its own statement
    ("Hours on the lake — by boat — and walks along the shore are part of how this stage of the journey unfolds, "
     "with horse riding available as an extra."),                               # synthetic, r4 #5 (a plan statement)
])
def test_optional_boat_written_as_planned(sentence):
    found = _trip(sentence)["trip_fact"]
    assert any("as part of the plan" in v and "boat" in v for v in found), found


@pytest.mark.parametrize("sentence", [
    "Optional activities include boat rides on the water, horseback riding and nomad visits.",
    "Boat rides on the water, horse riding and trekking are all optional.",
    "The day includes the boundary walk and, if you wish, a boat ride on the water.",
    "Across the lake days, active options include trekking, horseback riding and boat rides on the water.",
    "Boat rides, horse riding and visits to nomadic families can be arranged on request.",   # reviewer
    "Boat rides and horse riding cost extra.",
    "Supplies for the lakeside camps still arrive by boat in summer.",          # reviewer: freight, not travellers
    "The camp's own supplies come in by boat once a week.",
    "Supplies arrive by boat, which is part of life on the lake.",
])
def test_hedged_lists_are_not_flagged(sentence):
    assert not any("as part of the plan" in v for v in _trip(sentence)["trip_fact"])


@pytest.mark.parametrize("sentence", [
    "An optional day with the Tsaatan reindeer herders is possible.",       # synthetic, r4 #18 (day made optional)
    "Visiting the Tsaatan herders is optional on day five.",                # synthetic, r4 #18
    "The Tsaatan visit is optional.",                                       # synthetic, r4 #18
    "An optional excursion to the Tsaatan reindeer herders is possible.",   # synthetic, r4 #18
])
def test_planned_moment_called_optional(sentence):
    assert any(v.startswith("calls Tsaatan") for v in _trip(sentence)["trip_fact"])


@pytest.mark.parametrize("sentence", [
    "At the Tsaatan settlements on day five, children can ride a reindeer if they wish.",   # verifier
    "At the Tsaatan settlements, milking the reindeer is optional.",                        # verifier
    "Reindeer riding on the Tsaatan day is optional.",                     # a sub-activity of the planned day
    "On day five, milking the reindeer at the Tsaatan settlements is optional.",
    "Sod Camp has a mountain range nearby for optional trekking and a mineral spring.",  # optional names trekking
    "Optional boat rides on Lake Khövsgöl fill the fifth day.",             # mixed moment: forward check's job
    "Children can feed the reindeer at the Tsaatan settlements.",          # 'can' is what a planned day allows
    "Sod Camp staff can arrange optional excursions to the mineral spring.",                 # reviewer (side place)
    "An optional stop at Uran Togoo's crater rim viewpoint is possible for older children.",  # reviewer
    "Visiting the mineral spring near Sod Camp is optional.",
])
def test_reverse_flip_near_misses(sentence):
    assert not any(v.startswith("calls ") for v in _trip(sentence)["trip_fact"])


def test_time_idiom_is_not_a_schedule():
    assert _trip("Children are rarely worn out by midday here.")["trip_fact"] == []      # synthetic, r4 #11 FP
    assert any("gives a time" in v for v in _trip("You reach the Tsaatan settlements by midday.")["trip_fact"])


def test_difficulty_promise_is_a_pace_finding():
    assert any("'technical'" in v for v in _trip("The caldera walk is not a technical climb.")["trip_fact"])  # r4 #14
    assert any("'technical'" in v for v in _trip("It isn't a technical hike.")["trip_fact"])  # synthetic, r4 #14
    assert _trip("The guides carry technical fabric jackets for the wind.")["trip_fact"] == []
    assert _trip("The caldera walk is not a technical climb.", channel="linkedin")["trip_fact"] == []  # blogs only


@pytest.mark.parametrize("sentence", [
    "The hike is strenuous in places, so good boots matter.",              # verifier (a safety caveat)
    "The last stretch is gruelling in the heat, so start early.",
    "No technical climbing gear is needed on the caldera walk.",               # reviewer (a packing fact)
    "Helmets are provided for the short technical scramble below the summit.",  # reviewer (a route fact)
    "The route has one technical section, which guides rope up.",
])
def test_difficulty_caveats_are_not_pace_promises(sentence):
    assert not any("pace or rest promise" in v for v in _trip(sentence)["trip_fact"])


def test_lunch_at_a_place_the_source_puts_it_near():
    found = _trip("Lunch is at Mörön, the provincial capital.")["trip_fact"]   # synthetic, r4 #4/#5/#14/#15/#16/#18/#19
    assert any("but the source says near it" in v for v in found)
    assert _trip("Lunch is near Mörön, the provincial capital.")["trip_fact"] == []
    found = _trip("A stop in Mörön breaks the drive.")["trip_fact"]              # synthetic, r3 'A stop in Mörön'
    assert any("but the source says near it" in v for v in found)


@pytest.mark.parametrize("sentence", [
    "After lunch, the drive continues to the airport in Mörön for those flying back.",   # reviewer (clause boundary)
    "The bus does not stop in Mörön.",                                                   # a negated stop
    "Lunch is a picnic; the evening is in Mörön.",
])
def test_near_as_at_near_misses(sentence):
    assert not any("but the source says near it" in v for v in _trip(sentence)["trip_fact"]), sentence


# ---------------------------------------------------------------- F18 superlatives and overclaims (§1.3 row 5)

@pytest.mark.parametrize("sentence", [
    "This is one of the most coherent family routes available.",               # synthetic, r4 #16
    "The forest here is unlike almost anything else on earth.",                # quote, r4 #16
    "It is a landscape most of the world has never considered.",               # quote, r4 #17
    "A way of life that most of the world has never seen survives here.",      # synthetic, r3 #17 quote
    "The lake holds a significant fraction of the world's fresh water.",        # synthetic, r4 #19 (0.4%)
])
def test_superlatives_and_overclaims(sentence):
    assert _trip(sentence)["rarity"], sentence


@pytest.mark.parametrize("sentence", [
    "Travellers from most of the world can get a visa on arrival in Mongolia.",            # verifier
    "A considerable share of the walk is on flat ground beside the lake.",                 # verifier
    "A substantial portion of the day is spent on the road between camps.",                # verifier
    "A significant share of the trip is spent outdoors.",
    "The importance of sun protection at this altitude is hard to overstate.",   # reviewer (a safety caveat)
    "The scale of the lake is hard to overstate.",            # r4 #4 shape: known miss ('hard to overstate' dropped)
    "The Tsaatan are one of the most distinct ethnic groups you can meet.",    # a superlative the source makes
])
def test_rarity_near_misses(sentence):
    seed = ROUTE_SEED + "\n- The Tsaatan are one of Mongolia's most distinct ethnic groups."
    assert _trip(sentence, seed=seed)["rarity"] == [], sentence


# ---------------------------------------------------------------- F13 repeats and leaks (§1.3 row 4)

@pytest.mark.parametrize("text", [
    ("Punakha Dzong is still a living religious seat. The courtyards are open on day four. "
     "It is a living religious institution today."),                       # synthetic, r4 #1
    ("Punakha Dzong is still a living religious seat. The courtyards are open on day four. "
     "The dzong continues to function as a living religious institution."),   # synthetic, r4 #1 wording
])
def test_paraphrased_descriptor_repeat(text):
    assert any(r.startswith("tells the same thing again") for r in _issues(text, PUNAKHA_SEED)["repeat"])


@pytest.mark.parametrize("text", [
    ("The evening brings a local musical performance in the camp. Dinner is mutton stew and bread. "
     "Children can try the local musical instruments in the camp afterwards."),                     # verifier
    ("The ecological boundary runs along the lake. The camp is on the shore. "
     "Families can discuss the ecological boundary itself over dinner."),
    "The camp is a traditional nomadic ger camp. The lodge is a traditional nomadic guesthouse.",  # two subjects
    ("Our day-two lodge is a comfortable rural guesthouse. Dinner is at seven. "
     "Our day-four lodge is a comfortable rural farmstay."),                                        # reviewer
    ("The first camp is a seasonal nomadic site. Dinner is at seven. "
     "The second camp is a seasonal nomadic settlement."),                                          # reviewer
    ("The monastery remains a working religious community. "
     "It is also a working religious school for young monks."),                                   # reviewer (additive)
    ("The dzong is a living religious seat. The monks are friendly. "
     "They are a living religious community."),                                    # a pronoun with another antecedent
])
def test_paraphrase_near_misses(text):
    assert h.paraphrased_repeats(text, ROUTE_SEED) == []


def test_paraphrase_check_on_many_varied_descriptor_sentences():
    """The verifier's synthetic stress text: hundreds of 'The <adj> <adj> <noun> …' sentences flagged 234 of 332."""
    adjs = ["ancient", "local", "musical", "religious", "seasonal", "typical", "historic", "natural", "rural"]
    nouns = ["hall", "camp", "road", "valley", "dance", "market", "bridge", "temple", "lake", "festival", "meal"]
    sents = [f"The {a} {b} {n} is near the camp." for a in adjs for b in adjs if a != b for n in nouns[:5]]
    assert len(sents) > 300
    assert h.paraphrased_repeats(" ".join(sents), ROUTE_SEED) == []


@pytest.mark.parametrize("text", [
    "## Practical notes\n\nEntry fees are included in the price.",                                  # verifier
    "## Uran Togoo\n\nThe approach road is paved for most of the way.",                             # verifier
    "## Punakha Dzong\n\nAdmission tickets for the dzong are arranged by your guide.",              # verifier
    "## Into the Taiga\n\nAccess requires a multi-hour horse or reindeer ride.",   # r4 #16: known miss (dropped)
])
def test_section_openers_are_not_repeat_findings(text):
    assert not any(r.startswith("opens its section") for r in _issues(text)["repeat"])


@pytest.mark.parametrize("text", [
    "## Ruins\n\nOne old wall, one year, one fact.",                                       # synthetic, r2 #19
    "## Mörön\n\nMörön has one phonetic approximation in English that locals dislike.",  # synthetic, r3 #14
    "## Mörön\n\nThe city is correctly spelled Mörön, with two umlauts.",   # synthetic, the seed's Naming note
    "## Mörön\n\nMurun is correctly spelled with two umlauts.",              # synthetic, another spelling of the note
])
def test_brief_and_naming_note_leaks(text):
    assert _issues(text)["leak"], text


@pytest.mark.parametrize("text", [
    "## Paro\n\nThe trail map at the gate is only a loose approximation of the real path.",           # verifier
    "## Bhutan\n\nThe two named passes on the road, Dochula and Pele La, both have prayer flags.",    # verifier
    "## Punakha\n\nThe two named rivers meet below the dzong walls.",       # r3 #7 shape: known miss (dropped)
    "## Paro\n\nThe midday sun on the trail is best avoided.",
    "## Paro\n\nA guide named Tashi meets the group at the trailhead.",
    "## Mörön\n\nMörön, also romanised as Murun, is the provincial capital.",
    "## Camp\n\nEach family gets one tent, one stove and one lantern.",
    "## Camp\n\nOne tent, one stove, one lantern.",
    "## Distances\n\nThe 3.5 km figure is a common approximation.",
    # reviewer
    "## Visas\n\nMake sure your name is correctly spelled on the visa application, exactly as in your passport.",
    "## Paro\n\nThe Dzongkha greeting kuzuzangpo is written here as a phonetic approximation.",   # reviewer
    "## Mörön\n\nWhen flying to Mörön, make sure your name is correctly spelled on the ticket.",
    "## Mörön\n\nThe city has one phonetic approximation in English that locals dislike.",   # names no Naming place
])
def test_leak_near_misses(text):
    assert _issues(text)["leak"] == [], text


def test_naming_note_terms_need_a_seed_naming_note():
    text = "## Paro\n\nThe Dzongkha greeting kuzuzangpo is written here as a phonetic approximation."   # reviewer
    assert _issues(text, PUNAKHA_SEED)["leak"] == []


def test_naming_note_terms_are_not_trimmed_by_autofix():
    text = "## Mörön\n\nMörön has one phonetic approximation in English.\n\nThe courtyards are open."
    fixed, _ = h.autofix(text, h.HygieneContext(seed=ROUTE_SEED, brand_name="Pretend Brand", cta=None))
    assert "Mörön has one phonetic approximation in English." in fixed


def test_brief_wording_is_reported_not_deleted_by_autofix():
    text = "## Ruins\n\nOne old wall, one year, one fact.\n\nThe courtyards are open."
    fixed, _ = h.autofix(text, h.HygieneContext(seed=PUNAKHA_SEED, brand_name="Pretend Brand", cta=None))
    assert "One old wall, one year, one fact." in fixed


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
