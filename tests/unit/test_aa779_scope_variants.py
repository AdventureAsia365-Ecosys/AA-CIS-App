"""AA-779 (S245, 20/10) — the real-question scope filter keeps spelling variants of the moment's places
and travel questions about the scope's country.

Live evidence: the admin SERP top-up stored good questions (`search_demand`, market US-PAA2) for
'khovsgol lake mongolia' and 'khaiphaen restaurant laos', and `paa_for_scope` kept 0 of 12: Google spells
the lake Khuvsgul / Hovsgol, and the country-level questions name no place of the moment. Every question
below is a live one from that run (plus the variants named in the issue).
"""
import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_shared import real_questions as rq

KHOVSGOL_SEED = (
    "Lake Khövsgöl — trekking along the shore\n"
    "Location: Khövsgöl Lake, Khövsgöl Province, Mongolia (do not place it in any other town, region or country)\n"
    "Source itinerary: Trek the lakeshore; boat trips and horse riding are offered. Overnight in a ger camp.\n"
    "Place background (checked facts about the place itself — you may use these):\n"
    "- Lake Khövsgöl holds an estimated 70 percent of Mongolia's fresh water.\n"
    "- The lake freezes solidly in winter, typically from January through April.\n"
)
KHAIPHAEN_SEED = (
    "Khaiphaen — dinner at a training restaurant\n"
    "Location: Khaiphaen, Luang Prabang, Laos (do not place it in any other town, region or country)\n"
    "Source itinerary: Dinner at Khaiphaen, a training restaurant in Luang Prabang.\n"
    "Place background (checked facts about the place itself — you may use these):\n"
    "- Khaiphaen trains former street youth in cooking and service, serving modern Lao food.\n"
)

# Live serp_topup rows, 20/10 ('khovsgol lake mongolia', 'khaiphaen restaurant laos').
KHOVSGOL_KEPT = [
    "Can you swim in Lake Khuvsgul?",
    "What is the most famous lake in Mongolia?",
    "Do Mongolian gers have toilets?",
    "Is there a water shortage in Mongolia?",  # the source speaks of fresh water; the FAQ step decides
    "Does it ever get warm in Mongolia?",
]
KHOVSGOL_DROPPED = ["What US state is most like Mongolia?"]  # an off-topic comparison
KHAIPHAEN_KEPT = ["What is considered the national dish of Laos?", "What are the best restaurants in Laos?"]
KHAIPHAEN_DROPPED = ["Which restaurant is the most famous in the world?"]  # not about the scope


def _keep(q: str, seed: str) -> bool:
    return qg.paa_for_scope([q], seed, None, out_of_scope=("gobi", "ulaanbaatar", "vientiane")) == [q]


# ---------------------------------------------------------------- the live examples

@pytest.mark.parametrize("q", KHOVSGOL_KEPT)
def test_live_khovsgol_topup_questions_are_kept(q):
    assert _keep(q, KHOVSGOL_SEED)


@pytest.mark.parametrize("q", KHAIPHAEN_KEPT)
def test_live_khaiphaen_topup_questions_are_kept(q):
    assert _keep(q, KHAIPHAEN_SEED)


@pytest.mark.parametrize("q,seed", [(q, KHOVSGOL_SEED) for q in KHOVSGOL_DROPPED]
                         + [(q, KHAIPHAEN_SEED) for q in KHAIPHAEN_DROPPED])
def test_live_off_topic_topup_questions_are_dropped(q, seed):
    assert not _keep(q, seed)


def test_the_live_batch_keeps_seven_of_nine():
    out = (qg.paa_for_scope(KHOVSGOL_KEPT + KHOVSGOL_DROPPED, KHOVSGOL_SEED, None)
           + qg.paa_for_scope(KHAIPHAEN_KEPT + KHAIPHAEN_DROPPED, KHAIPHAEN_SEED, None))
    assert out == KHOVSGOL_KEPT + KHAIPHAEN_KEPT


# ---------------------------------------------------------------- (1) spelling variants

@pytest.mark.parametrize("spelling", ["Khuvsgul", "Hovsgol", "Khovsgol", "Khövsgöl", "Hövsgöl", "Khuvsugul"])
def test_spelling_variants_match_the_scope_place(spelling):
    assert rq.place_variant_match(spelling, "khovsgol")
    assert rq.names_own_place(f"How deep is Lake {spelling}?", {"khovsgol"})
    assert _keep(f"Is {spelling} Lake worth visiting?", KHOVSGOL_SEED)


def test_skeleton_folds_kh_vowels_and_doubled_letters():
    assert {rq.name_skeleton(w) for w in ("Khuvsgul", "Hovsgol", "Khovsgol", "Khövsgöl")} == {"hovsgol"}
    assert rq.name_skeleton("Ulaanbaatar") == "olanbatar"


@pytest.mark.parametrize("word,name", [
    ("Moron", "muron"),       # 5 letters: short names match only exactly
    ("Hue", "hoe"), ("Paro", "pora"),
    ("Halong", "belong"),     # different first letter
    ("Orphan", "orkhon"),     # skeleton distance 2 on a 5-letter skeleton: over the cap of 1
    ("travel", "terelj"),
    ("Mongol", "mongolia"),
])
def test_no_fuzzy_match_for_short_or_distant_words(word, name):
    assert not rq.place_variant_match(word, name)


def test_a_lowercase_word_matches_only_by_skeleton():
    assert rq.place_variant_match("hovsgol", "khovsgol", capitalised=False)       # same skeleton
    assert not rq.place_variant_match("khuvsugul", "khovsgol", capitalised=False)  # edit distance needs a capital
    assert rq.place_variant_match("Khuvsugul", "khovsgol", capitalised=True)


def test_variants_match_real_names_only_not_line_openers():
    # 'Source' / 'Trek' open seed lines (keep_initial name set) — a near spelling of them is no place
    assert not _keep("Is the Sourse of the river far?", KHOVSGOL_SEED)


# ---------------------------------------------------------------- (2) country-level travel questions

@pytest.mark.parametrize("q", [
    "Is Mongolia friendly to American tourists?",
    "Is Mongolia safe for tourists?",
    "Do Mongolian gers have toilets?",
])
def test_country_level_travel_questions_are_in_scope(q):
    assert rq.country_travel_question(q, ["Mongolia"])


def test_demonyms_count_for_their_country():
    assert rq.country_travel_question("Is Bhutanese food spicy?", ["Bhutan"])
    assert rq.country_travel_question("What is Lao food like?", ["Laos"])
    assert not rq.country_travel_question("Is Bhutanese food spicy?", ["Laos"])  # not the scope's country


@pytest.mark.parametrize("q", [
    "What US state is most like Mongolia?",      # comparison with somewhere else
    "Is Mongolia similar to Kazakhstan?",
    "Is Mongolia part of China?",                # names another place
    "Can you visit the Gobi Desert from Mongolia?",  # another place of the country (not in the gazetteer)
    "Who is the president of Mongolia?",         # not a travel question
    "Is Mongolia a poor country?",
    "How many people live in Mongolia?",
    "Which country has the most famous lakes in the world?",
])
def test_off_topic_country_questions_are_dropped(q):
    assert not rq.country_travel_question(q, ["Mongolia"])
    assert not _keep(q, KHOVSGOL_SEED)


def test_a_country_question_with_no_travel_word_needs_a_source_word():
    q = "Is there a water shortage in Mongolia?"
    assert rq.country_travel_question(q, ["Mongolia"], seed_words={"water"})
    assert not rq.country_travel_question(q, ["Mongolia"])
    assert not _keep(q, KHOVSGOL_SEED.replace("fresh water", "fresh supply"))


def test_a_country_question_for_another_country_is_dropped():
    assert not _keep("What is the most famous lake in Mongolia?", KHAIPHAEN_SEED)
    assert not _keep("What is considered the national dish of Laos?", KHOVSGOL_SEED)


def test_answerability_still_runs_before_the_country_tier():
    # toilets = accommodation topic: kept only when the moment's own lines speak of a camp / ger
    assert not _keep("Do Mongolian gers have toilets?", KHOVSGOL_SEED.replace(" Overnight in a ger camp.", ""))


def test_world_superlatives_are_dropped_even_with_two_seed_words():
    assert not _keep("Which training restaurant serves the best Lao food in the world?", KHAIPHAEN_SEED)
    assert rq.is_world_question("Which restaurant is the most famous in the world?")
    assert rq.is_world_question("What is the world's deepest lake?")
    assert not rq.is_world_question("What is the most famous lake in Mongolia?")


# ---------------------------------------------------------------- (3) off-meaning junk never passes

@pytest.mark.parametrize("q", [
    "Is Khuvsgul a British word?",
    "What is Khuvsgul in German?",
    "How do you spell Hovsgol?",
    "What does Mongolia mean?",
    "What is a moron?",
    "Is moron a British word?",
])
def test_off_meaning_questions_stay_out_with_variants_and_countries(q):
    assert not _keep(q, KHOVSGOL_SEED)
    assert not qg.paa_for_scope([q], KHOVSGOL_SEED.replace("Lake Khövsgöl —", "Mörön and Lake Khövsgöl —"), None)


def test_off_meaning_reads_a_variant_as_the_name():
    assert rq.is_off_meaning("Is Khuvsgul a British word?", ["khovsgol"])
    assert not rq.is_off_meaning("Can you swim in Lake Khuvsgul?", ["khovsgol"])


def test_chain_merge_drops_junk_and_keeps_variants_for_the_topup_tier():
    out = rq.merge([(rq.SOURCE_SERP_TOPUP, ["Is Khuvsgul a British word?", "Can you swim in Lake Khuvsgul?",
                                            "What is moron in German?", "Does Mörön have an airport?"])],
                   names=["khovsgol", "moron"], lowercase_names=["moron"], countries=["Mongolia"])
    assert [r.question for r in out] == ["Can you swim in Lake Khuvsgul?", "Does Mörön have an airport?"]
    assert {r.source for r in out} == {rq.SOURCE_SERP_TOPUP}


def test_output_shape_is_unchanged_plain_question_strings():
    out = qg.paa_for_scope(KHOVSGOL_KEPT, KHOVSGOL_SEED, None)
    assert out and all(isinstance(q, str) and q.endswith("?") for q in out)
    assert len(qg.paa_for_scope(KHOVSGOL_KEPT * 3 + ["Is Hovsgol Lake deep?"] * 5, KHOVSGOL_SEED)) <= \
        qg.PAA_MAX_QUESTIONS
