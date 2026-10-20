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
    "Does it ever get warm in Mongolia?",
]
KHOVSGOL_DROPPED = [
    "What US state is most like Mongolia?",  # an off-topic comparison
    # Review round 1: no traveller frame and no practical travel topic (an environment question; a
    # shared seed word alone no longer ties a country-level question to the moment).
    "Is there a water shortage in Mongolia?",
]
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


def test_the_live_batch_keeps_six_of_nine():
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


def test_a_country_question_off_the_practical_list_needs_a_frame_and_a_source_word():
    q = "Can you swim in lakes in Mongolia?"  # traveller frame 'you'; tied by 'lake'
    assert rq.country_travel_question(q, ["Mongolia"], seed_words={"lake"})
    assert not rq.country_travel_question(q, ["Mongolia"])
    # no traveller frame: a shared source word alone is not enough
    assert not rq.country_travel_question("Is there a water shortage in Mongolia?", ["Mongolia"],
                                          seed_words={"water"})


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
    many = KHOVSGOL_KEPT * 3 + ["Is Hovsgol Lake deep?"] * 5 + ["What is Mongolia known for?", "Is Mongolia worth it?"]
    assert len(qg.paa_for_scope(many, KHOVSGOL_SEED)) == qg.PAA_MAX_QUESTIONS


# ================================================================ review round 1 (S245, 20/10)
# Every question the adversarial review of e41aea4d cited, with its seed, as a regression test. Precision
# first: a kept question can reach a customer-facing FAQ, so a false keep is worse than a false drop.

def _seed(title: str, loc: str, src: str, *facts: str) -> str:
    return (f"{title}\nLocation: {loc} (do not place it in any other town, region or country)\n"
            f"Source itinerary: {src}\nPlace background (checked facts about the place itself — you may use these):\n"
            + "".join(f"- {f}\n" for f in facts))


ERDENE_ZUU = _seed("Erdene Zuu Monastery — Karakorum",
                   "Erdene Zuu Monastery, Kharkhorin, Övörkhangai Province, Mongolia",
                   "Visit Erdene Zuu Monastery near the ruins of ancient Karakorum.",
                   "Erdene Zuu was founded in 1585 beside the ruins of Karakorum.")
DARKHAD = _seed("Darkhad Valley — horse trek", "Darkhad Valley, Khövsgöl Province, Mongolia",
                "Horse trek through the Darkhad Valley with reindeer herders.",
                "The Darkhad Valley is home to Tsaatan reindeer herders.")
ANGKOR = _seed("Angkor Wat — sunrise", "Angkor Wat, Siem Reap, Cambodia",
               "Sunrise at Angkor Wat, then explore the temple galleries.",
               "Angkor Wat was built in the early 12th century.")
BUKHARA = _seed("Bukhara old town — walking tour", "Bukhara, Bukhara Region, Uzbekistan",
                "Walk the old town of Bukhara and the Kalyan Minaret.", "Bukhara was a major stop on the Silk Road.")
MINGUN = _seed("Mingun — boat trip", "Mingun, Sagaing Region, Myanmar",
               "Boat up the Irrawaddy to Mingun to see the unfinished pagoda.", "Mingun Pahtodawgyi was begun in 1790.")
TERELJ = _seed("Terelj National Park — ger stay", "Gorkhi-Terelj National Park, Töv Province, Mongolia",
               "Overnight in a ger camp in Terelj and hike to Turtle Rock.",
               "Terelj National Park lies about 70 km from the capital.")
MANDALAY_HILL = _seed("Mandalay Hill — sunset", "Mandalay Hill, Mandalay, Myanmar",
                      "Climb Mandalay Hill for sunset over the city.", "Mandalay Hill rises 240 metres above the city.")
POTALA = _seed("Potala Palace — morning visit", "Potala Palace, Lhasa, Tibet",
               "Climb the steps of the Potala Palace in Lhasa with a local guide.",
               "The Potala Palace was the winter residence of the Dalai Lama.")
MUTIANYU = _seed("Mutianyu — Great Wall hike", "Mutianyu, Huairou, China",
                 "Hike the restored towers of the Great Wall at Mutianyu.", "The Mutianyu section was rebuilt in 1569.")
GOBI = _seed("Khongoryn Els — sand dunes at sunset", "Khongoryn Els, Gobi Gurvansaikhan National Park, Mongolia",
             "Climb the dunes at sunset; camel riding with a nomad family. Overnight in a ger camp.",
             "Khongoryn Els dunes rise up to 300 metres and stretch about 100 kilometres.")
HANOI = ("Hoan Kiem Lake — morning walk\n"
         "Location: Hoan Kiem Lake, Hanoi, Vietnam (do not place it in any other town, region or country)\n"
         "Source itinerary: Walk around the lake at dawn; watch locals practise tai chi. Street food breakfast.\n")
HUE = _seed("Imperial City — cyclo morning", "Imperial City, Huế, Vietnam",
            "Explore the Imperial City of Huế by cyclo.", "Huế was the capital of the Nguyễn dynasty.")
HOI_AN = _seed("Ancient Town — lantern night", "Ancient Town, Hội An, Vietnam",
               "Walk the Ancient Town of Hội An at night when the lanterns are lit.",
               "Hội An was a trading port from the 15th century.")
DA_NANG = _seed("My Khe Beach — free morning", "My Khe Beach, Đà Nẵng, Vietnam",
                "A free morning on My Khe Beach in Đà Nẵng.", "My Khe Beach runs for several kilometres.")
PARO = _seed("Paro Taktsang — Tiger's Nest hike", "Paro Taktsang, Paro, Bhutan",
             "Hike up to Paro Taktsang above the Paro valley.",
             "Paro Taktsang clings to a cliff 900 metres above the valley.")
ULAANBAATAR = _seed("Ulaanbaatar — city tour", "Ulaanbaatar, Mongolia",
                    "Visit Gandan Monastery and Sukhbaatar Square in Ulaanbaatar.",
                    "Ulaanbaatar is the coldest capital city in the world.")
LUANG_PRABANG = _seed("Luang Prabang — alms giving at dawn", "Luang Prabang, Laos",
                      "Watch the alms giving in Luang Prabang, then visit the night market.",
                      "Luang Prabang is a UNESCO town on the Mekong.")
HA_LONG = _seed("Ha Long Bay — overnight cruise", "Ha Long Bay, Vietnam",
                "Cruise Ha Long Bay overnight and kayak in the limestone karsts.",
                "Ha Long Bay has about 1,600 limestone islands.", "The bay is calmest in spring and autumn.")
THIMPHU_LINE_START = ("Thimphu — weekend market\nLocation: Thimphu, Bhutan\n"
                      "Source itinerary: Stroll the weekend market and visit the Memorial Chorten.")
THIMPHU_MID = ("Thimphu — a walk through the capital\n"
               "Location: Thimphu, Bhutan (do not place it in any other town, region or country)\n"
               "Source itinerary: Walk through Thimphu, visit the weekend market and the Memorial Chorten.\n")
UB_LINE_START = ("Ulaanbaatar — city day\nLocation: Ulaanbaatar, Mongolia\n"
                 "Source itinerary: Visit Gandan Monastery and the Sukhbaatar Square.")
KHOVSGOL_LINE_START = ("Khovsgol — lake day\nLocation: Khovsgol, Mongolia\n"
                       "Source itinerary: Drive to the lake shore and stay in a ger camp.")
KHOVSGOL_LABEL_ONLY = ("Khövsgöl — trekking along the shore\nLocation: Khövsgöl, Mongolia (northern)\n"
                       "Source itinerary: Trek the lakeshore; boat trips and horse riding are offered.\n")
KHARKHORIN = ("Kharkhorin — Erdene Zuu Monastery\n"
              "Location: Kharkhorin, Övörkhangai Province, Mongolia "
              "(do not place it in any other town, region or country)\n"
              "Source itinerary: Visit Erdene Zuu Monastery in Kharkhorin, the old capital.\n"
              "Place background (checked facts about the place itself — you may use these):\n"
              "- Kharkhorin was the capital of the Mongol Empire in the 13th century.\n")
MORON = ("Mörön — flight to the north\n"
         "Location: Mörön, Khövsgöl Province, Mongolia (do not place it in any other town, region or country)\n"
         "Source itinerary: Fly from Ulaanbaatar to Mörön and drive to the lake.\n"
         "Place background (checked facts about the place itself — you may use these):\n"
         "- Mörön has a small domestic airport with flights from Ulaanbaatar.\n")
HUE_PLAIN = ("Location: Hue, Vietnam\n"
             "Visit the Imperial City in Huế and walk along the Perfume River with a local guide.")
HUE_VIET_NAM = HUE_PLAIN.replace("Hue, Vietnam", "Hue, Viet Nam")
CAMBODIA_DEMONYM = ("Angkor Wat — sunrise visit\nLocation: Angkor Wat, Siem Reap, Cambodia (temple)\n"
                    "Source itinerary: Watch sunrise over Angkor Wat with a Cambodian guide, "
                    "then breakfast near the moat.\n")
MALAYSIA_DEMONYM = ("Batu Caves — morning\nLocation: Batu Caves, Kuala Lumpur, Malaysia (cave)\n"
                    "Source itinerary: Climb the steps with a Malaysian guide and see the temple caves.\n")
INDONESIA_DEMONYM = ("Borobudur — sunrise\nLocation: Borobudur, Magelang, Indonesia (temple)\n"
                     "Source itinerary: Sunrise at Borobudur with an Indonesian guide.\n")
PHILIPPINES_DEMONYM = ("Chocolate Hills — viewpoint\nLocation: Chocolate Hills, Bohol, Philippines (hill)\n"
                       "Source itinerary: View the hills with a Philippine guide.\n")
VIETNAM_DEMONYM = ("Halong Bay — cruise\nLocation: Halong Bay, Quang Ninh, Vietnam (bay)\n"
                   "Source itinerary: Cruise with a Vietnamese guide.\n")


def _city_walk(name: str, region: str, country: str) -> str:
    return (f"{name} — city walk\nLocation: {name}, {region}, {country} (do not place it in any other town, region or "
            f"country)\nSource itinerary: Walk the old streets of {name} with a guide and try local food.\n")


def _kept(q: str, seed: str) -> bool:
    return qg.paa_for_scope([q], seed, None) == [q]


# ---------------------------------------------------------------- F0 / F15: other real places are not variants

@pytest.mark.parametrize("word,name", [
    ("Karakoram", "karakorum"), ("Darkhan", "darkhad"), ("Angkar", "angkor"), ("Bukhari", "bukhara"),
    ("Mingus", "mingun"), ("Terrell", "terelj"), ("Mandala", "mandalay"), ("Khaling", "halong"),
    ("Yanggu", "yangon"), ("Hunger", "khongor"), ("Chengde", "chengdu"), ("Nanning", "nanjing"),
    ("Erdenet", "erdene"), ("Mannar", "munnar"),
])
def test_distinct_places_one_consonant_apart_never_match(word, name):
    assert not rq.place_variant_match(word, name)


@pytest.mark.parametrize("seed,q", [
    (ERDENE_ZUU, "Is the Karakoram Highway dangerous?"), (ERDENE_ZUU, "How high is the Karakoram range?"),
    (DARKHAD, "What is Darkhan known for?"),
    (ANGKOR, "What was the Angkar in Cambodia?"), (ANGKOR, "Who led the Angkar?"),
    (BUKHARA, "Who wrote Sahih Bukhari?"), (BUKHARA, "How many hadith are in Sahih al-Bukhari?"),
    (MINGUN, "What is Charles Mingus famous for?"),
    (TERELJ, "Where is Terrell, Texas?"),
    (MANDALAY_HILL, "What does a Mandala symbolize?"),
])
def test_f0_near_spelled_other_places_are_dropped(seed, q):
    assert not _kept(q, seed)


@pytest.mark.parametrize("own,other,region,country", [
    ("Chengdu", "Chengde", "Sichuan", "China"), ("Nanjing", "Nanning", "Jiangsu", "China"),
    ("Erdene Zuu", "Erdenet", "Övörkhangai", "Mongolia"), ("Munnar", "Mannar", "Kerala", "India"),
    ("Pokhara", "Kathmandu", "Gandaki", "Nepal"),
])
def test_f15_another_city_of_the_country_is_dropped(own, other, region, country):
    seed = _city_walk(own, region, country)
    for q in (f"How far is {other} from the airport?", f"Is {other} worth visiting?"):
        assert not _kept(q, seed)


def test_f15_control_the_own_city_is_kept():
    seed = _city_walk("Hanoi", "Red River Delta", "Vietnam")
    assert _kept("How far is Hanoi from the airport?", seed)
    assert _kept("Is Hanoi worth visiting?", seed)


# ---------------------------------------------------------------- F1: demonym and lowercase homonyms

@pytest.mark.parametrize("seed,q", [
    (KHOVSGOL_SEED, "Is Mongolian beef healthy to eat?"),
    (KHOVSGOL_SEED, "What restaurant has the best Mongolian beef?"),
    (KHOVSGOL_SEED, "Is Mongolian BBQ healthy to eat?"),
    (KHOVSGOL_SEED, "Is Mongolian hot pot healthy?"),
    (KHOVSGOL_SEED, "Are Mongolian gerbils friendly?"),
    (KHOVSGOL_SEED, "Do Mongolian gerbils need a lot of food?"),
    (KHOVSGOL_SEED, "Are Mongolian spots dangerous?"),
    (KHOVSGOL_SEED, "Is a Mongolian blue spot safe?"),
    (KHOVSGOL_SEED, "Is the Mongolian death worm dangerous?"),
    (MANDALAY_HILL, "Is a Burmese python dangerous?"),
    (MANDALAY_HILL, "Are Burmese cats friendly?"),
    (POTALA, "Is a Tibetan mastiff dangerous?"),
    (POTALA, "Are Tibetan mastiffs friendly to strangers?"),
    (MUTIANYU, "Is china safe to put in the dishwasher?"),
    (MUTIANYU, "Is bone china safe in the microwave?"),
])
def test_f1_homonyms_of_the_country_are_dropped(seed, q):
    assert not _kept(q, seed)


# ---------------------------------------------------------------- F2: people, politics, economy, history

@pytest.mark.parametrize("seed,q", [(KHOVSGOL_SEED, q) for q in (
    "Who is the most famous Mongolian sumo wrestler?", "Who is the most beautiful Mongolian actress?",
    "Who is the most famous person from Mongolia?", "Did Mongolians drink horse blood?",
    "Why did Mongolian warriors ride horses?", "Is Mongolia a safe country to invest in?",
    "Is Mongolia friendly to the US?", "What percent of Mongolia is Buddhist?",
    "What percent of Mongolians live in poverty?", "Which province of Mongolia is the richest?",
    "Is Mongolia a poor country?", "What religion is Mongolia?", "Who is the president of Mongolia?",
    "What is the population of Mongolia?")]
    + [(KHAIPHAEN_SEED, "Who is the most famous Lao singer?"), (KHAIPHAEN_SEED, "Is Laos a communist country?")])
def test_f2_non_travel_country_questions_are_dropped(seed, q):
    assert not _kept(q, seed)


# ---------------------------------------------------------------- F3 / F18: comparisons and other places

@pytest.mark.parametrize("seed,q", [(KHOVSGOL_SEED, q) for q in (
    "Russia or Mongolia: which is colder in winter?", "Inner Mongolia: is it worth visiting?",
    "Is Inner Mongolia worth visiting?", "Is Mongolia as safe as the UK?", "Is Mongolia as safe as the USA?",
    "Is Mongolia as safe as Japan?", "Is Mongolia as safe as japan?", "Is Mongolian food spicier than Korean food?",
    "Is North Korean food like Mongolian food?", "Is Mongolia colder than Canada?", "Is Mongolia colder than Russia?",
    "Is Mongolia safer than the US?", "Is Mongolia safer than America?", "Is Mongolia colder than the UK?",
    "Is Mongolian food better than Chinese food?", "Is Mongolia colder than Kazakh steppe?",
    "Is Mongolia safer than China?", "Is Mongolian food better than Thai food?", "Is Mongolia cheaper than Kazakhstan?",
    "Do Mongolian and Chinese people eat the same food?", "Is Mongolia more expensive than Vietnam?",
    "Is Mongolian cuisine influenced by Chinese cuisine?", "Is Mongolian food similar to Chinese food?",
    "What is the difference between Mongolian and Chinese food?", "Do Mongolians speak Russian?")]
    + [(KHAIPHAEN_SEED, q) for q in (
        "Is Laos as safe as the USA?", "Vietnam or Laos: which is better to visit?",
        "Is Laos or Vietnam better to visit?",
        "Is Laos cheaper than Thailand?", "Is Lao food like Thai food?", "Is Laos safer than the US?",
        "Is Lao food spicier than Thai food?", "Is Thai food spicier than Lao food?",
        "Is Lao food better than Thai food?",
        "Is Lao food the same as Thai food?", "Is Lao food like Vietnamese food?")]
    + [(HANOI, q) for q in (
        "Vietnam or Laos: which is better to visit?", "Is Vietnam as safe as the USA?",
        "Thailand or Vietnam: which is cheaper to visit?")])
def test_f3_f18_comparisons_and_other_places_are_dropped(seed, q):
    assert not _kept(q, seed)


@pytest.mark.parametrize("seed,q", [
    (KHOVSGOL_SEED, "Is Mongolia safe for tourists?"),
    (KHOVSGOL_SEED, "Is Mongolia safe for US citizens?"),  # a traveller nationality
    (KHAIPHAEN_SEED, "Is Laos safe for UK tourists?"),
    (KHAIPHAEN_SEED, "Is Laos friendly to Chinese tourists?"),
])
def test_f3_traveller_nationalities_stay(seed, q):
    assert _kept(q, seed)


# ---------------------------------------------------------------- F4: tied to the moment

@pytest.mark.parametrize("seed,q", [(GOBI, q) for q in (
    "What is the most famous lake in Mongolia?", "Can you swim in lakes in Mongolia?",
    "What is the most famous river in Mongolia?", "What is the highest mountain in Mongolia?",
    "Is there a famous temple in Mongolia?", "What is the most famous monastery in Mongolia?")]
    + [(KHOVSGOL_SEED, q) for q in (
        "What is the biggest desert in Mongolia?", "Can you ride camels in Mongolia?",
        "How deep is Lake Baikal compared to Khuvsgul?", "Is Lake Khuvsgul bigger than Lake Baikal?",
        # the exact spelling too (kept before the variant change; a comparison with somewhere else)
        "Is Lake Khövsgöl bigger than Lake Baikal?", "How deep is Lake Baikal compared to Khövsgöl?")])
def test_f4_another_kind_of_place_or_a_comparison_is_dropped(seed, q):
    assert not _kept(q, seed)


@pytest.mark.parametrize("q", ["What is the most famous lake in Mongolia?", "Can you swim in lakes in Mongolia?"])
def test_f4_the_same_kind_of_place_as_the_moment_stays(q):
    assert _kept(q, KHOVSGOL_SEED)


# ---------------------------------------------------------------- F5 / F16: possessive country

@pytest.mark.parametrize("seed,q", [
    (KHOVSGOL_SEED, "What is Mongolia's most famous lake?"), (KHOVSGOL_SEED, "What is Mongolia’s most famous lake?"),
    (KHOVSGOL_SEED, "Is Mongolia's weather good in summer?"),
    (KHOVSGOL_SEED, "What is Mongolia's best season to visit?"),
    (KHOVSGOL_SEED, "What is Mongolia's most famous food?"), (KHOVSGOL_SEED, "What is Mongolia’s national dish?"),
    (KHOVSGOL_SEED, "Is Mongolia's weather cold in July?"), (KHOVSGOL_SEED, "What food is Mongolia famous for?"),
    (KHAIPHAEN_SEED, "What is Laos's national dish?"), (KHAIPHAEN_SEED, "What is Laos' national dish?"),
    (HUE, "What is Vietnam's national dish?"),
])
def test_f5_possessive_country_questions_are_kept(seed, q):
    assert _kept(q, seed)


def test_f5_a_possessive_of_another_country_still_drops():
    assert not _kept("What is China's national dish?", KHOVSGOL_SEED)


# ---------------------------------------------------------------- F6: Vietnamese diacritics

@pytest.mark.parametrize("seed,q", [
    (HUE, "What is Hue famous for?"), (HUE, "What is Huế famous for?"), (HUE, "Is Hue worth visiting?"),
    (HUE, "How many days do you need in Hue?"),
    (HOI_AN, "Is Hoi An worth visiting?"), (HOI_AN, "What is Hội An famous for?"), (HOI_AN, "How many days in Hoi An?"),
    (HOI_AN, "Is Hoian worth visiting?"),
    (DA_NANG, "Is Da Nang worth visiting?"), (DA_NANG, "Is Danang worth visiting?"), (DA_NANG, "Is Đà Nẵng safe?"),
])
def test_f6_vietnamese_place_names_with_diacritics_match(seed, q):
    assert _kept(q, seed)


def test_f6_diacritics_fold_including_d_stroke():
    assert rq.fold("Đà Nẵng") == "da nang" and rq.deaccent("Hội An") == "Hoi An" and rq.fold("Huế") == "hue"
    assert rq.name_skeleton("Đà Nẵng") == rq.name_skeleton("Danang")


# ---------------------------------------------------------------- F7: practical travel words

@pytest.mark.parametrize("seed,q", [
    (KHOVSGOL_SEED, "What is Mongolia known for?"), (KHOVSGOL_SEED, "Is Mongolia worth it?"),
    (KHOVSGOL_SEED, "Is Mongolia expensive?"), (KHOVSGOL_SEED, "What should I pack for Mongolia?"),
    (KHOVSGOL_SEED, "What is Mongolia famous for?"), (KHOVSGOL_SEED, "Is Mongolia worth visiting?"),
    (KHOVSGOL_SEED, "Is Mongolia cheap to visit?"), (KHOVSGOL_SEED, "Is Mongolia expensive for tourists?"),
    (KHAIPHAEN_SEED, "Is Laos cheap?"), (KHAIPHAEN_SEED, "What is Laos known for?"),
    (KHAIPHAEN_SEED, "What is Laos famous for?"),
    (PARO, "What is Bhutan known for?"), (HUE, "What is Vietnam known for?"),
])
def test_f7_practical_country_questions_are_kept(seed, q):
    assert _kept(q, seed)


@pytest.mark.parametrize("q", ["Do you need a guide in Mongolia?", "What vaccinations do I need for Mongolia?"])
def test_f7_left_dropped_guide_and_health_questions(q):
    # deliberately left (precision first): not on the practical list and not tied to the moment
    assert not _kept(q, KHOVSGOL_SEED)


# ---------------------------------------------------------------- F8: festival names, brands

@pytest.mark.parametrize("q", [
    "Is Mongolia worth visiting during Naadam?", "What do you eat during Naadam festival in Mongolia?",
    "Is it safe to travel to Mongolia?", "Is Mongolia safe for tourists?", "What is the best month to visit Mongolia?",
])
def test_f8_capitalised_festival_names_no_longer_read_as_places(q):
    assert _keep(q, KHOVSGOL_SEED)


@pytest.mark.parametrize("q", ["Can you use Visa cards in Mongolia?", "Can I use Visa in Mongolia?"])
def test_f8_visa_the_card_is_no_place_but_answerability_still_runs(q):
    assert rq.country_travel_question(q, ["Mongolia"])
    assert qg.unanswerable_faq_topics(q, KHOVSGOL_SEED) == ["booking"]  # 'visa' needs the source to speak of it
    assert not _keep(q, KHOVSGOL_SEED)


@pytest.mark.parametrize("q", [
    "When is Naadam festival in Mongolia?", "When is the Naadam festival in Mongolia?", "When is Naadam in Mongolia?",
    "What is Tsagaan Sar in Mongolia?", "Can you see the Northern Lights in Mongolia?",
    "Can you see the Milky Way in Mongolia?", "Is Uber available in Mongolia?", "Do I need a visa for Mongolia?",
])
def test_f8_left_dropped_off_list_or_unanswerable(q):
    # deliberately left: festival dates / sky sights are not on the practical list and not tied to a lake
    # moment; visa needs the source (answerability)
    assert not _keep(q, KHOVSGOL_SEED)


# ---------------------------------------------------------------- F9: multi-word romanisations

@pytest.mark.parametrize("seed,q", [
    (ULAANBAATAR, "Is Ulan Bator safe for tourists?"), (ULAANBAATAR, "What is Ulan Bator famous for?"),
    (ULAANBAATAR, "Is Ulanbator worth visiting?"),
    (LUANG_PRABANG, "What is Louangphabang famous for?"), (LUANG_PRABANG, "Is Luangprabang worth visiting?"),
    (HA_LONG, "Is Halong Bay worth visiting?"), (HA_LONG, "What is the best time to visit Halong Bay?"),
    (HA_LONG, "Can you swim in Halong Bay?"), (HA_LONG, "Is Ha Long Bay worth it?"),
])
def test_f9_joined_split_and_exonym_spellings_match(seed, q):
    assert _kept(q, seed)


def test_f9_common_english_word_of_a_name_is_no_own_place():
    # 'long' of Ha Long Bay and a 'Trek …' line opener are no own place when written lowercase
    assert not _kept("How long is the Great Wall?", HA_LONG)
    assert not _kept("Can you trek in Nepal?", KHOVSGOL_SEED)


# ---------------------------------------------------------------- F10 / F17: names only at a line start

@pytest.mark.parametrize("seed,q", [
    (THIMPHU_LINE_START, "Is Thimpu worth visiting?"), (THIMPHU_LINE_START, "What is Thimpu known for?"),
    (THIMPHU_LINE_START, "Is Thimpu safe for tourists?"), (THIMPHU_LINE_START, "Is Thimphu worth visiting?"),
    (UB_LINE_START, "Is Ulaanbataar worth visiting?"), (UB_LINE_START, "What is Ulaanbataar famous for?"),
    (UB_LINE_START, "Is Ulan Bator safe?"), (UB_LINE_START, "Is Ulaanbaatar worth visiting?"),
    (KHOVSGOL_LINE_START, "Can you swim in Lake Khuvsgul?"), (KHOVSGOL_LINE_START, "Is Khuvsgul worth visiting?"),
    (KHOVSGOL_LINE_START, "How cold is Hovsgol lake?"),
    (KHOVSGOL_LABEL_ONLY, "Can you swim in Lake Khuvsgul?"), (KHOVSGOL_LABEL_ONLY, "Is Hovsgol worth visiting?"),
    (KHOVSGOL_LABEL_ONLY, "How deep is Khuvsgul Lake?"), (KHOVSGOL_LABEL_ONLY, "Can you swim in Lake Khovsgol?"),
])
def test_f10_f17_variants_of_a_label_or_location_name_match(seed, q):
    assert _kept(q, seed)


def test_f17_best_time_variant_passes_scope_and_drops_only_on_answerability():
    q = "What is the best time to visit Khuvsgul?"
    _, _, countries = qg.seed_place_names(KHOVSGOL_LABEL_ONLY)
    assert rq.variant_place_question(q, qg.seed_variant_names(KHOVSGOL_LABEL_ONLY, countries), countries)
    assert qg.unanswerable_faq_topics(q, KHOVSGOL_LABEL_ONLY) == ["best_time"]


def test_f10_a_sentence_label_is_no_place_name():
    assert "sunrise" not in qg.seed_variant_names("Sunrise hike — the ridge\nLocation: Paro, Bhutan", {"bhutan"})


# ---------------------------------------------------------------- F11: exonyms and other first letters

@pytest.mark.parametrize("seed,q", [
    (KHARKHORIN, "Is Karakorum worth visiting?"), (KHARKHORIN, "What is Karakorum famous for?"),
    (KHARKHORIN, "Is Kharkhorum worth visiting?"),
    (MORON, "Does Muren have an airport?"), (MORON, "Is Murun worth visiting?"), (MORON, "Does Moron have an airport?"),
])
def test_f11_listed_exonyms_match(seed, q):
    assert _kept(q, seed)


# ---------------------------------------------------------------- F12: lowercase variants (left)

@pytest.mark.parametrize("seed,q", [
    (THIMPHU_MID, "is thimpu worth visiting?"), (THIMPHU_MID, "What to do in thimpu?"),
    (THIMPHU_MID, "Is thimpu safe at night?"),
    (KHOVSGOL_SEED, "Can you swim in lake khuvsugul?"), (KHOVSGOL_SEED, "How deep is khuvsugul lake?"),
    (KHOVSGOL_SEED, "How cold is lake khuvsugul?"),
])
def test_f12_left_lowercase_exonym_or_inserted_vowel_needs_a_capital(seed, q):
    # deliberately left: an exonym or an inserted vowel counts only for a capitalised word
    assert not _kept(q, seed)


@pytest.mark.parametrize("seed,q", [
    (THIMPHU_MID, "Is Thimpu worth visiting?"), (KHOVSGOL_SEED, "Can you swim in Lake Khuvsugul?"),
    (KHOVSGOL_SEED, "can you swim in lake khuvsgul?"),  # same skeleton: kept in lowercase too
])
def test_f12_capitalised_or_same_skeleton_variants_stay(seed, q):
    assert qg.paa_for_scope([q], seed, None)


# ---------------------------------------------------------------- F13: 'Viet Nam', Burma

@pytest.mark.parametrize("seed", [HUE_PLAIN, HUE_VIET_NAM])
@pytest.mark.parametrize("q", ["Is Vietnam safe for tourists?", "Is Viet Nam safe for tourists?",
                               "What is the best food in Vietnam?", "What is the best food in Viet Nam?",
                               "What is Viet Nam famous for?"])
def test_f13_viet_nam_in_two_words_is_the_country(seed, q):
    assert _kept(q, seed)


@pytest.mark.parametrize("q", ["Is Myanmar safe to visit?", "Is Burma safe to visit?"])
def test_f13_burma_is_myanmar(q):
    assert not _kept(q, HUE_PLAIN)
    assert _kept(q, MANDALAY_HILL)


def test_f13_a_month_is_no_place():
    assert rq.country_travel_question("What is the weather like in Vietnam in March?", ["Vietnam"])


# ---------------------------------------------------------------- F14: a demonym in the seed is no own place

@pytest.mark.parametrize("seed,q", [
    (CAMBODIA_DEMONYM, "Who is the president of Cambodia?"), (CAMBODIA_DEMONYM, "Is Cambodia a poor country?"),
    (CAMBODIA_DEMONYM, "What is the GDP of Cambodia?"), (CAMBODIA_DEMONYM, "What language is spoken in Cambodia?"),
    (MALAYSIA_DEMONYM, "Who is the king of Malaysia?"), (MALAYSIA_DEMONYM, "Is Malaysia a rich country?"),
    (MALAYSIA_DEMONYM, "What is the GDP of Malaysia?"),
    (INDONESIA_DEMONYM, "Who is the president of Indonesia?"), (INDONESIA_DEMONYM, "What is the GDP of Indonesia?"),
    (PHILIPPINES_DEMONYM, "Who is the president of Philippines?"),
    (PHILIPPINES_DEMONYM, "What is the GDP of the Philippines?"),
    (VIETNAM_DEMONYM, "Who is the president of Vietnam?"), (VIETNAM_DEMONYM, "What is the GDP of Vietnam?"),
])
def test_f14_seed_demonyms_never_make_the_country_an_own_place(seed, q):
    assert not _kept(q, seed)


@pytest.mark.parametrize("word,name", [("Cambodia", "cambodian"), ("Malaysia", "malaysian"),
                                       ("Indonesia", "indonesian"), ("Philippines", "philippine")])
def test_f14_country_and_demonym_are_no_variants(word, name):
    assert not rq.name_variants_in(f"Is {word} safe?", {name})


# ================================================================ review round 2 (S245, 20/10)
# The still-open HIGH findings of the re-review of 3bc6c922, every cited question with its seed. Precision
# first again: each question below was KEPT at 3bc6c922 and must be dropped.

HCMC = _seed("Ben Thanh Market — street food tour", "Ben Thanh Market, Ho Chi Minh City, Vietnam",
             "Walk through Ben Thanh Market in Ho Chi Minh City and taste street food.",
             "Ben Thanh Market opened in 1914.")
YANGON = _seed("Shwedagon Pagoda — sunset", "Shwedagon Pagoda, Yangon, Myanmar",
               "Visit Shwedagon Pagoda in Yangon at sunset.", "Yangon was the capital of Myanmar until 2006.")
CAN_THO = _seed("Cai Rang — floating market at dawn", "Cai Rang Floating Market, Can Tho, Vietnam",
                "Boat to the Cai Rang floating market at dawn from Can Tho.",
                "Cai Rang is the largest floating market in the Mekong Delta.")
DON_DET = _seed("Don Det — bicycle loop", "Don Det, Si Phan Don, Laos",
                "Cycle around Don Det and cross the old railway bridge to Don Khon.",
                "Don Det is one of the 4,000 Islands of the Mekong.")
WAR_MUSEUM = _seed("War Remnants Museum — morning visit", "War Remnants Museum, Ho Chi Minh City, Vietnam",
                   "Visit the War Remnants Museum in Ho Chi Minh City with a local guide.",
                   "The War Remnants Museum opened in 1975.")


# ---------------------------------------------------------------- R2-1: exonyms that are homonyms

@pytest.mark.parametrize("seed,q", [
    (KHARKHORIN, "How high is the Karakorum range?"), (KHARKHORIN, "Where is the Karakorum mountain range?"),
    (KHARKHORIN, "Is K2 in the Karakorum?"),
    (YANGON, "How many calories are in crab Rangoon?"), (YANGON, "What is crab Rangoon made of?"),
    (YANGON, "Is crab Rangoon healthy?"),
])
def test_r2_1_an_exonym_alone_needs_a_travel_word_and_no_homonym(seed, q):
    assert not _kept(q, seed)


@pytest.mark.parametrize("seed,q", [
    (KHARKHORIN, "Is Karakorum worth visiting?"), (YANGON, "Is Rangoon safe for tourists?"),
    (HCMC, "Is Saigon worth visiting?"), (HCMC, "What is the best time to visit Saigon?"),
])
def test_r2_1_control_an_exonym_with_a_travel_word_stays(seed, q):
    assert qg.paa_for_scope([q], seed, "Season: November to April is dry.") == [q]


def test_r2_1_a_code_name_is_another_place():
    assert rq.names_other_place("Is K2 in the Karakorum?", {"karakorum"})
    assert rq.place_variant_kind("Karakorum", "kharkhorin") == "alias"
    assert rq.place_variant_kind("Khuvsgul", "khovsgol") == "skeleton"


# ---------------------------------------------------------------- R2-2: war and politics on the variant path

@pytest.mark.parametrize("seed,q", [
    (HCMC, "When did Saigon fall?"), (HCMC, "What happened at the fall of Saigon?"),
    (HCMC, "How many people died in the fall of Saigon?"), (HCMC, "Who won the battle of Saigon?"),
    (YANGON, "Who was the president when Rangoon was bombed?"),
])
def test_r2_2_war_and_politics_are_dropped_on_the_variant_path(seed, q):
    assert not _kept(q, seed)


def test_r2_2_harm_words_of_the_own_place_name_do_not_count():
    assert _kept("Is the War Remnants Museum worth visiting?", WAR_MUSEUM)
    assert not _kept("How many people died in the war in Vietnam?", WAR_MUSEUM)
    assert rq.harm_question("When did Saigon fall?")
    assert not rq.harm_question("Is the War Remnants Museum worth visiting?", {"war", "remnants", "museum"})


# ---------------------------------------------------------------- R2-3: diet, health and leftovers of a cuisine

@pytest.mark.parametrize("seed,q", [(MUTIANYU, q) for q in (
    "How long does Chinese food last in the fridge?", "Can you eat Chinese food left out overnight?",
    "Why does Chinese food make me sleepy?", "Is Chinese food bad for weight loss?",
    "Why do I get a headache after Chinese food?", "Is Chinese food safe during pregnancy?")]
    + [(HUE, "How long does Vietnamese food last in the fridge?"), (HUE, "Is Vietnamese food good for diabetics?"),
       (ANGKOR, "Is Cambodian food good for weight loss?")])
def test_r2_3_food_health_questions_are_dropped(seed, q):
    assert not _kept(q, seed)


@pytest.mark.parametrize("seed,q", [
    (HUE, "What food is Vietnam famous for?"), (HUE, "What is Vietnam's national dish?"),
    (MUTIANYU, "What food should I try in China?"), (ANGKOR, "What is Cambodia known for?"),
])
def test_r2_3_control_food_travel_questions_stay(seed, q):
    assert _kept(q, seed)


# ---------------------------------------------------------------- R2-4: past tense, war and history

@pytest.mark.parametrize("seed,q", [
    (HUE, "What did soldiers wear in Vietnam?"), (HUE, "What food did soldiers eat in Vietnam?"),
    (HUE, "How cold did it get for soldiers in Vietnam?"),
    (KHOVSGOL_SEED, "What food did Mongolians eat in the 13th century?"),
    (KHOVSGOL_SEED, "What currency did Mongolia use before the tugrik?"),
    (ANGKOR, "What is Cambodia known for historically?"),
])
def test_r2_4_past_tense_and_history_country_questions_are_dropped(seed, q):
    assert not _kept(q, seed)


# ---------------------------------------------------------------- R2-5: lowercase Vietnamese name tokens

@pytest.mark.parametrize("seed,q", [
    (HUE, "What is hue in color theory?"), (HUE, "What is hue in art?"),
    (HUE, "How do you change the hue of an image?"),
    (HUE, "Why is Nguyen such a common last name?"), (HUE, "Who is the richest Nguyen in America?"),
    (HOI_AN, "Who are the hoi polloi?"),
])
def test_r2_5_lowercase_name_tokens_and_seed_surnames_are_no_own_place(seed, q):
    assert not _kept(q, seed)


# ---------------------------------------------------------------- R2-6: the exact path checks everything

@pytest.mark.parametrize("seed,q", [
    (TERELJ, "Is The Rock still married to Lauren?"), (TERELJ, "What does a turtle eat?"),
    (TERELJ, "How long can a turtle live without water?"),
    (PARO, "Is Tiger Woods still playing golf?"), (MUTIANYU, "Is Wall Street open today?"),
    (HA_LONG, "Is Long Island worth visiting?"), (HA_LONG, "Is Long Beach safe at night?"),
    (HA_LONG, "Are Carnival cruise ships safe?"), (KHOVSGOL_SEED, "Is Star Trek on Netflix?"),
    (GOBI, "Is Ernie Els still playing golf?"), (LUANG_PRABANG, "Is the Apple Watch waterproof?"),
    (BUKHARA, "How is silk made?"), (BUKHARA, "Is a silk pillowcase good for your hair?"),
    (POTALA, "Where does the Dalai Lama live now?"), (POTALA, "Is the Dalai Lama allowed in Tibet?"),
    (POTALA, "Who will be the next Dalai Lama?"), (POTALA, "Is it safe to talk about the Dalai Lama in Tibet?"),
    (KHARKHORIN, "How tall is the Empire State Building?"), (KHARKHORIN, "Why did the Roman empire fall?"),
    (MANDALAY_HILL, "How long does it take to climb Mount Everest?"),
    (MANDALAY_HILL, "Can you climb Mount Fuji in winter?"),
    (ANGKOR, "Do you reap what you sow?"), (HOI_AN, "Who built the pyramids in Ancient Egypt?"),
])
def test_r2_6_seed_words_that_are_no_place_never_make_an_own_place(seed, q):
    assert not _kept(q, seed)


@pytest.mark.parametrize("seed,q", [
    (MUTIANYU, "Is the Great Wall worth visiting?"), (TERELJ, "Is Turtle Rock worth visiting?"),
    (PARO, "Is Paro Taktsang worth visiting?"), (GOBI, "Is Khongoryn Els worth visiting?"),
    (TERELJ, "Is Terelj National Park worth visiting?"), (ANGKOR, "Is Siem Reap worth visiting?"),
])
def test_r2_6_control_own_places_and_landmarks_stay(seed, q):
    assert _kept(q, seed)


def test_r2_6_own_place_forms_are_location_label_and_landmarks():
    forms = qg.seed_own_place_forms(POTALA)
    assert ("dalai", "lama") not in forms and ("potala", "palace") in forms and ("lhasa",) in forms
    assert not rq.own_place_named("What is hue in color theory?", qg.seed_own_place_forms(HUE))
    assert rq.own_place_named("Is Hue worth visiting?", qg.seed_own_place_forms(HUE)) == {"hue"}


# ---------------------------------------------------------------- R2-7: a Location token that is an English word

@pytest.mark.parametrize("seed,q", [
    (CAN_THO, "Can you see the Northern Lights in Iceland?"), (CAN_THO, "Can dogs eat grapes?"),
    (CAN_THO, "Can I bring my cat to Japan?"), (CAN_THO, "Can you drink alcohol on the beach in Thailand?"),
    (CAN_THO, "Can I use my phone abroad?"),
    (DON_DET, "Why don't Japanese people tip?"), (DON_DET, "Why don't you put ice in drinks in China?"),
])
def test_r2_7_an_english_word_of_a_location_name_is_no_own_place(seed, q):
    assert not _kept(q, seed)


@pytest.mark.parametrize("seed,q", [(CAN_THO, "Is Can Tho worth visiting?"), (DON_DET, "What is Don Det known for?")])
def test_r2_7_control_the_whole_name_stays(seed, q):
    assert _kept(q, seed)


# ---------------------------------------------------------------- R2: probes of the same review (not HIGH)

@pytest.mark.parametrize("seed,q", [
    (MUTIANYU, "Why is China so cheap?"), (MUTIANYU, "How does China make money?"),
    (MUTIANYU, "Is it safe for Americans to criticize China?"), (KHOVSGOL_SEED, "How does Mongolia make money?"),
    (KHOVSGOL_SEED, "Is Mongolia expensive to live in?"), (KHAIPHAEN_SEED, "Is Laos cheap to retire?"),
    (PARO, "How does Bhutan make money?"), (PARO, "Is Bhutan cold for a refugee?"),
    (GOBI, "What is the most popular Mongolian family name?"),
    (TERELJ, "What is the most famous rock band in Mongolia?"),
    (KHAIPHAEN_SEED, "What is the best youth football team in Laos?"),
    (MUTIANYU, "What is the most famous tower in China?"),
    (KHOVSGOL_SEED, "What is the most famous horse in Mongolia?"),
    (KHOVSGOL_SEED, "Can you buy a horse in Mongolia?"), (KHOVSGOL_SEED, "What is the most popular boat in Mongolia?"),
    (ULAANBAATAR, "What is the best city to live in Mongolia?"),
])
def test_r2_economy_living_and_off_kind_country_questions_are_dropped(seed, q):
    assert not _kept(q, seed)


@pytest.mark.parametrize("seed,q", [
    (KHOVSGOL_SEED, "Is Khuvsgul Nuur worth visiting?"), (KHOVSGOL_SEED, "Is Mongolia a good place to visit?"),
])
def test_r2_mongolian_kind_words_and_a_good_place_to_visit_stay(seed, q):
    assert _kept(q, seed)
