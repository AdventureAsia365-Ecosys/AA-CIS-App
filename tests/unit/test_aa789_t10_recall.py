"""AA-789 — T10 gate recall tuning (Report 12 §1.3, S234 panel rounds r2-r4).

Every defect the S234 panel cited in Report 12 §1.3 and its source rounds (reports 03, 04, 07) that the deterministic
gates missed gets a case here, as a minimal synthetic sentence that reproduces the pattern (never the stored copy:
the AA-919 golden-text guard). Each class also has the near-misses that must stay clean. Severity is unchanged
(standing rule since S246: no new blocking gates): the checks feed the existing F15_voice, F17_trip_fact, F18_rarity,
F13_leak, F13_repeat findings and the existing F16 FAQ dropper.

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


# ---------------------------------------------------------------- severity is unchanged (S246 standing rule)

def test_gate_severity_is_unchanged():
    assert qg.F15_STOCK_BLOCKING is True and qg.F17_BLOCKING is True and qg.F18_BLOCKING is True
    assert qg.F12_SCOPE_BLOCKING is False
    text = "The return is institutional, not ceremonial. The site was an argument, not a preference."
    attempt1 = qg.gate_voice(text, spec=None, stock_blocking=True)
    later = qg.gate_voice(text, spec=None, stock_blocking=False)
    assert not attempt1["passed"] and attempt1["blocking"]       # attempt 1: one sentence repair, as before
    assert not later["passed"] and not later["blocking"]          # attempt >= 2: a warn, never a hold


# ---------------------------------------------------------------- F15 contrast frames (§1.3 row 1)

@pytest.mark.parametrize("sentence", [
    "The yearly return is institutional, not ceremonial sentiment.",       # r4 #8, reversed 'Y, not X'
    "The location was a political argument, not a scenic preference.",    # r4 class 4
    "This is a working monastery, not a museum frozen in time.",            # r4 #1
    "The valley does not perform for anyone; it simply keeps its year.",   # r4 #15, verb negation + answer
    "Not squeezed in. Planned in.",                                        # r4 broetry pair
    "The camp is less a hotel, more a home.",                              # 'less X, more Y'
    "The dzong is not simply seen; it is entered.",                        # r4 #8 (already caught, kept)
    "These aren't inconveniences — they're part of the rhythm.",          # r4 #10 (already caught, kept)
])
def test_contrast_frame_shapes_are_found(sentence):
    assert _frames(sentence), sentence


@pytest.mark.parametrize("sentence", [
    "Children aged 9, not 7, can join the ride.",                          # a number, not a frame
    "Rest before lunch, not after the long climb.",                        # a practical prepositional contrast
    "The pools stay open, not least for the swimmers.",                    # additive 'not least'
    "Not far from the lake, the road climbs into the forest.",            # a sentence, not a fragment
    "Not all families choose the ride.",
    "It doesn't rain much here, it is said.",                              # comma: no verb-negation frame
    "The guide is less than an hour away.",
])
def test_contrast_near_misses_stay_clean(sentence):
    assert not _frames(sentence), sentence


def test_density_rule_still_decides_the_finding():
    one = "The return is institutional, not ceremonial. The pools are on the second day of the route."
    assert qg.gate_voice(one, spec=None)["passed"]              # 1 frame within 1 per 150 words: allowed
    two = one + " The site was an argument, not a preference."
    r = qg.gate_voice(two, spec=None)
    assert any(v.startswith("contrast frame") for v in r["violations"])


def test_a_frame_inside_a_stock_quote_is_not_counted_twice():
    text = "Rest is built in. Not as filler. Not as an afterthought. It is the plan."
    r = qg.gate_voice(text, spec=linkedin_spec({"brand_name": "Pretend Brand"}))
    assert not any(v.startswith("contrast frame") for v in r["violations"])


# ---------------------------------------------------------------- F15 stock lines in blogs (§1.3 row 1)

@pytest.mark.parametrize("sentence,line", [
    ("The landscape does not perform for visitors.", "does not perform"),   # r4 #15 (blog)
    ("The herders simply tolerates them.", "tolerates them"),                # r4 #17 (blog)
    ("The herding economy is a detail worth pausing on.", "worth pausing on"),  # r4 #5 (blog)
])
def test_returning_stock_lines_are_found_on_every_channel(sentence, line):
    assert [ln for ln, _ in h.template_lines(sentence)] == [line]
    assert any(v.startswith("stock line") for v in qg.gate_voice(sentence + " Day two is long.", spec=None)
               ["violations"])


def test_linkedin_only_stock_lines_stay_linkedin_only():
    assert qg.gate_voice("That is not a backdrop. It is the reason for the trip.", spec=None)["passed"]
    assert h.template_lines("The dancers do not perform the cham on Mondays.") == []


# ---------------------------------------------------------------- F17 'optional' flips (§1.3 row 7)

@pytest.mark.parametrize("sentence", [
    "Families cross the lake by boat, and walk the shore, and optionally ride horses.",  # r4 #14
    "Families travel by boat and on foot, with only horse riding optional.",          # r4 #5
])
def test_optional_boat_written_as_planned(sentence):
    found = _trip(sentence)["trip_fact"]
    assert any("as part of the plan" in v and "boat" in v for v in found), found


@pytest.mark.parametrize("sentence", [
    "Optional activities include boat rides on the water, horseback riding and nomad visits.",
    "Boat rides on the water, horse riding and trekking are all optional.",
    "The day includes the boundary walk and, if you wish, a boat ride on the water.",
    "Across the lake days, active options include trekking, horseback riding and boat rides on the water.",
])
def test_hedged_lists_are_not_flagged(sentence):
    assert not any("as part of the plan" in v for v in _trip(sentence)["trip_fact"])


@pytest.mark.parametrize("sentence", [
    "An optional day with the Tsaatan reindeer herders is possible.",       # r4 #18, the reverse flip
    "Visiting the Tsaatan herders is optional on day five.",
])
def test_planned_moment_called_optional(sentence):
    assert any(v.startswith("calls Tsaatan") for v in _trip(sentence)["trip_fact"])


@pytest.mark.parametrize("sentence", [
    "Sod Camp has a mountain range nearby for optional trekking and a mineral spring.",  # optional names trekking
    "Optional boat rides on Lake Khövsgöl fill the fifth day.",             # mixed moment: forward check's job
    "Children can feed the reindeer at the Tsaatan settlements.",          # 'can' is what a planned day allows
])
def test_reverse_flip_near_misses(sentence):
    assert not any(v.startswith("calls ") for v in _trip(sentence)["trip_fact"])


def test_time_idiom_is_not_a_schedule():
    assert _trip("Children are rarely worn out by midday here.")["trip_fact"] == []      # r4 #11 false positive
    assert any("gives a time" in v for v in _trip("You reach the Tsaatan settlements by midday.")["trip_fact"])


def test_difficulty_promise_is_a_pace_finding():
    assert any("'technical'" in v for v in _trip("The caldera walk is not a technical climb.")["trip_fact"])  # r4 #14
    assert _trip("The guides carry technical fabric jackets for the wind.")["trip_fact"] == []
    assert _trip("The caldera walk is not a technical climb.", channel="linkedin")["trip_fact"] == []  # blogs only


def test_lunch_at_a_place_the_source_puts_it_near():
    found = _trip("Lunch is at Mörön, the provincial capital.")["trip_fact"]   # r4 #4/#5/#14/#15/#16/#18/#19
    assert any("but the source says near it" in v for v in found)
    assert _trip("Lunch is near Mörön, the provincial capital.")["trip_fact"] == []


# ---------------------------------------------------------------- F18 superlatives and overclaims (§1.3 row 5)

@pytest.mark.parametrize("sentence", [
    "This is one of the most coherent family routes available.",               # r4 #16
    "The forest here is unlike almost anything on earth.",                     # r4 #16
    "Most of the world has never heard of this valley.",                       # r4 #17
    "A significant fraction of global fresh water sits here.",                 # r4 #19 (0.4%)
    "The scale of the lake is hard to overstate.",                             # r4 #4
])
def test_superlatives_and_overclaims(sentence):
    assert _trip(sentence)["rarity"], sentence


def test_a_superlative_the_source_makes_is_kept():
    assert _trip("The Tsaatan are one of the most distinct ethnic groups you can meet.")["rarity"] == []


# ---------------------------------------------------------------- F13 repeats, orphans, leaks (§1.3 row 4)

def test_paraphrased_descriptor_repeat():
    text = ("Punakha Dzong is still a living religious seat. The courtyards are open on day four. "
            "It is a living religious institution today.")                     # r4 #1
    assert any(r.startswith("tells the same thing again") for r in _issues(text, PUNAKHA_SEED)["repeat"])


def test_a_repeated_noun_phrase_is_not_a_paraphrase():
    text = ("The ecological boundary runs along the lake. The camp is on the shore. "
            "Families can discuss the ecological boundary itself over dinner.")
    assert h.paraphrased_repeats(text, ROUTE_SEED) == []


def test_section_opening_on_a_bare_relational_noun():
    text = ("## Lake Khövsgöl\n\nThe lake holds 70 percent of Mongolia's fresh water.\n\n"
            "## The Tsaatan\n\nAccess requires a long ride on horseback.\n\n"
            "The Tsaatan are a small community.")                              # r4 #16
    assert any(r.startswith("opens its section with nothing") for r in _issues(text)["repeat"])
    ok = text.replace("Access requires", "Access to the settlements requires")
    assert not any(r.startswith("opens its section") for r in _issues(ok)["repeat"])


@pytest.mark.parametrize("text", [
    "## Ruins\n\nOne old wall, one year, one story.",                                       # r2 #19
    "## Mörön\n\nIts name has a phonetic approximation that locals dislike.",                # r3 #14
    "## Punakha\n\nThe two named rivers meet below the dzong walls.",                       # r3 #7
])
def test_brief_and_naming_note_leaks(text):
    assert _issues(text)["leak"], text


@pytest.mark.parametrize("text", [
    "## Paro\n\nThe midday sun on the trail is best avoided.",
    "## Paro\n\nA guide named Tashi meets the group at the trailhead.",
    "## Mörön\n\nMörön, also romanised as Murun, is the provincial capital.",
])
def test_leak_near_misses(text):
    assert _issues(text)["leak"] == []


def test_brief_wording_is_reported_not_deleted_by_autofix():
    text = "## Punakha\n\nThe two named rivers meet below the dzong walls.\n\nThe courtyards are open."
    fixed, _ = h.autofix(text, h.HygieneContext(seed=PUNAKHA_SEED, brand_name="Pretend Brand", cta=None))
    assert "two named rivers" in fixed


# ---------------------------------------------------------------- F16 answer addresses the question (§1.3 row 8)

@pytest.mark.parametrize("question,answer,why", [
    ("Who are the Tsaatan?", "The road north takes several days and the region sits high.", "no_subject"),  # r4 #5
    ("What do the Tsaatan use their reindeer for?",
     "The Tsaatan number only a few hundred people and live in the taiga.", "off_attribute"),             # r4 #19
    ("What is the weather like at Lake Khövsgöl?",
     "Lake Khövsgöl holds 70 percent of Mongolia's fresh water.", "off_attribute"),                       # r4 #17
])
def test_faq_non_answers(question, answer, why):
    assert qg.faq_answer_problem(question, answer, ROUTE_SEED) == why


@pytest.mark.parametrize("question,answer", [
    ("What do the Tsaatan use their reindeer for?", "The Tsaatan use their reindeer for riding and for milk."),
    ("What is the weather like at Lake Khövsgöl?", "Lake Khövsgöl has mild summers and cold nights."),
    ("When is the boat ride on Lake Khövsgöl?", "The boat rides are on day 5 of the trip."),
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
