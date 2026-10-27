"""AA-784 (S253 round 3) — place-card identity / number rules from the blind 20-card spot check of cards rebuilt
under round 2 (CIS #1059; root repo docs/audits/2026-10-27-S253-aa784-spotcheck-r2): FK 3/79 = 3.8% (gate <= 3%),
6/20 cards with a bad locality or region, none of them a wrong value the round-2 rules wrote — all over-emptying:

- a locality equal to the region was cleared ('same_as_region'): D04 Kruba Srivichai Monument (Chiang Mai), Yongdusan
  Park (Busan), Ssangyong Cave (Jeju City) — now kept;
- a first-level city is a right region, also equal to the locality or the place (D20 Almaty, D13 Huế);
- a border-spanning place keeps a locality / region only as both sides 'A / B' (D03 cleared, D19 kept);
- the 3 false facts were exact numbers; life dates in parentheses after a name are dropped (D04 1878–1938).
"""
import json

import pytest

from services.acp_shared import place_cards as pc
from services.acp_shared.atom_seed import writer_facts, writer_identity
from tests.unit.test_aa772_place_card_cost import FakeDB, _atom, _ensure
from tests.unit.test_aa784_place_card_hygiene import SPOT_CHECK_BAD, SPOT_CHECK_EXPECTED, ScriptedLLM

# ── the r2 spot-check cards: (drafted identity, itinerary names) -> (locality, region) after the rules ──────────────

R2_CASES = [
    # D04: a monument in a city named like its province — both right
    ("Kruba Srivichai Monument", "Chiang Mai", "Chiang Mai", "Thailand", ("Kruba Sri Vichai Statue",),
     ("Chiang Mai", "Chiang Mai")),
    # D11: an urban park in a first-level city (the labelled locality is the district; the city is not wrong)
    ("Yongdusan Park", "Busan", "Busan", "South Korea", ("Yongdusan Park",), ("Busan", "Busan")),
    ("Yongdusan Park", "Jung-gu", "Busan", "South Korea", ("Yongdusan Park",), ("Jung-gu", "Busan")),
    # D16: a cave in a city / town
    ("Ssangyong Cave", "Jeju City", "Jeju", "South Korea", ("Sangyong Cave",), ("Jeju City", "Jeju")),
    ("Ssangyong Cave", "Hallim-eup", "Jeju", "South Korea", ("Sangyong Cave",), ("Hallim-eup", "Jeju")),
    # D13: a settlement is its own locality (empty); its first-level city is its region
    ("A Lưới", "A Lưới", "Huế", "Vietnam", ("A Luoi",), ("", "Huế")),
    # D20: a park in a first-level city: locality and region are both the city
    ("Panfilov Park", "Almaty", "Almaty", "Kazakhstan", ("Panfilov Park",), ("Almaty", "Almaty")),
    # D03: a border — one side only is cleared, legacy compound country or not
    ("Myanmar–Thailand border", "Mae Fa Luang", "Chiang Rai", "Thailand and Myanmar", ("Thai-Myanmar border",),
     ("", "")),
    ("Myanmar–Thailand border", "Mae Fa Luang", "Chiang Rai", "Thailand", ("Thai-Myanmar border",), ("", "")),
    # D19: an international bridge with both sides is right
    ("Fourth Thai–Lao Friendship Bridge", "Huay Xai / Chiang Khong", "Bokeo / Chiang Rai", "Laos and Thailand",
     ("Lao-Thai Friendship Bridge",), ("Huay Xai / Chiang Khong", "Bokeo / Chiang Rai")),
    ("Fourth Thai–Lao Friendship Bridge", "Huay Xai", "Bokeo", "Laos and Thailand", (), ("", "")),
    # remote kinds the round keeps emptying (D09, D17)
    ("Karkyra Base Camp", "Karkara", "Issyk-Kul Region", "Kyrgyzstan", ("Karkyra base camp",),
     ("", "Issyk-Kul Region")),
    ("Ghurchi Lagna Pass", "Mugu", "Karnali Province", "Nepal", ("Ghurchi Lagna pass",), ("", "Karnali Province")),
    ("Knuckles Mountain", "Matale", "Central Province", "Sri Lanka", ("Knuckles summit",), ("", "Central Province")),
]


@pytest.mark.parametrize("name,locality,region,country,names,expected", R2_CASES,
                         ids=[f"{c[0]}|{c[1]}|{c[3]}" for c in R2_CASES])
def test_r2_spot_check_identities(name, locality, region, country, names, expected):
    card = {"display_name": name, "locality": locality, "region": region}
    pc.clean_identity(card, place_names=names, country=country)
    assert (card["locality"], card["region"]) == expected


@pytest.mark.parametrize("cid", sorted(SPOT_CHECK_BAD))
def test_s252_regressions_still_hold(cid):
    """Barafu Camp -> no Moshi, Thado Koshi -> no Lukla, Old Goa keeps Goa Velha (needs a gazetteer) …"""
    card, names = SPOT_CHECK_BAD[cid]
    card = dict(card)
    pc.clean_identity(card, place_names=names, country=card["country"])
    assert (card["locality"], card["region"]) == SPOT_CHECK_EXPECTED[cid]


def test_cross_border_clear_is_reported():
    card = {"display_name": "Myanmar–Thailand border", "locality": "Mae Fa Luang", "region": "Chiang Rai",
            "country": "Thailand and Myanmar"}
    assert pc.clean_identity(card, country="Thailand") == [
        {"field": "region", "reason": "cross_border", "value": "Chiang Rai"},
        {"field": "locality", "reason": "cross_border", "value": "Mae Fa Luang"}]


# review of round 3: a border card's sides still pass the per-field rules; only '/' separates sides
BORDER_CASES = [
    # a pass on a legacy compound-country card: no town on either side; the regions are right
    ("Khunjerab Pass", "Sost / Tashkurgan", "Gilgit-Baltistan / Xinjiang", "Pakistan and China",
     ("", "Gilgit-Baltistan / Xinjiang"), [("locality", "remote_place")]),
    # same pass, single country: same locality rule
    ("Khunjerab Pass", "Sost", "Gilgit-Baltistan", "Pakistan", ("", "Gilgit-Baltistan"),
     [("locality", "remote_place")]),
    # countries are no region, on either side
    ("Myanmar–Thailand border", "", "Thailand / Myanmar", "Thailand", ("", ""), [("region", "country")]),
    # 'Town, Province' is one side, not both
    ("Myanmar–Thailand border", "Mae Sai, Chiang Rai", "", "Thailand", ("", ""), [("locality", "cross_border")]),
    ("Myanmar–Thailand border", "", "Chiang Rai, Tachileik", "Thailand and Myanmar", ("", ""),
     [("region", "cross_border")]),
    # a trek area is no region on either side
    ("Nangpa La", "", "Khumbu / Tibet", "Nepal and China", ("", ""), [("region", "trek_area")]),
    # both sides, each a right value: kept
    ("Fourth Thai–Lao Friendship Bridge", "Huay Xai / Chiang Khong", "Bokeo / Chiang Rai", "Laos and Thailand",
     ("Huay Xai / Chiang Khong", "Bokeo / Chiang Rai"), []),
]


@pytest.mark.parametrize("name,locality,region,country,expected,cleared", BORDER_CASES,
                         ids=[f"{c[0]}|{c[1]}|{c[2]}" for c in BORDER_CASES])
def test_border_sides_still_pass_the_field_rules(name, locality, region, country, expected, cleared):
    card = {"display_name": name, "locality": locality, "region": region, "country": country}
    out = pc.clean_identity(card, country=country)
    assert (card["locality"], card["region"]) == expected
    assert [(o["field"], o["reason"]) for o in out] == cleared


# ── locality: only remote kinds are emptied ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("locality,name,region,country", [
    ("Busan", "Yongdusan Park", "Busan", "South Korea"),                 # urban park
    ("Singapore", "Gardens by the Bay", "", "Singapore"),                # garden
    ("Hallim-eup", "Ssangyong Cave", "Jeju", "South Korea"),             # cave in a town
    ("Chiang Mai", "Kruba Srivichai Statue", "Chiang Mai", "Thailand"),  # statue
    ("Luang Prabang", "Wat Xieng Thong", "Luang Prabang", "Laos"),       # temple, city named like its province
    ("Patong", "Bangla Road", "Phuket", "Thailand"),                      # street
    ("Listvyanka", "Listvyanka Market", "Irkutsk Oblast", "Russia"),     # market
    ("Hanoi", "Hoan Kiem Lake", "Hanoi", "Vietnam"),                     # a lake in a first-level city
    ("Seoul", "Seokchon Lake", "Seoul", "South Korea"),
    ("Nong Khiaw", "Nam Ou Valley", "Luang Prabang", "Laos"),            # valley / gorge / desert left the list
    ("Lijiang", "Tiger Leaping Gorge", "Yunnan", "China"),
])
def test_urban_and_settled_places_keep_their_locality(locality, name, region, country):
    assert pc.locality_problem(locality, name=name, region=region, country=country) is None


@pytest.mark.parametrize("locality,name,region,country,why", [
    ("Moshi", "Barafu Camp", "Kilimanjaro", "Tanzania", "remote_place"),
    ("Arusha", "Kilimanjaro National Park", "Kilimanjaro", "Tanzania", "remote_place"),
    ("Leh", "Khardung La", "Ladakh", "India", "remote_place"),
    ("Pokhara", "Phewa Lake", "Gandaki Province", "Nepal", "remote_place"),   # Pokhara is not a first-level city
    ("Hanoi", "Ba Vi National Park", "Hanoi", "Vietnam", "remote_place"),     # a park phrase stays remote
    ("Seoul", "Bukhansan Mountain", "Seoul", "South Korea", "remote_place"),  # only lakes keep a first-level city
    ("Busan", "Busan", "Busan", "South Korea", "is_place"),
])
def test_remote_places_and_settlements_still_lose_their_locality(locality, name, region, country, why):
    assert pc.locality_problem(locality, name=name, region=region, country=country) == why


# ── region: first-level cities ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Almaty", "Astana", "Shymkent", "Bishkek", "Tashkent", "Busan", "Busan Metropolitan City", "Seoul Special City",
    "Incheon", "Daegu", "Hanoi", "Ha Noi", "Ho Chi Minh City", "Huế", "Hue City", "Da Nang", "Hai Phong", "Can Tho",
    "Bangkok", "Tokyo", "Tokyo Metropolis", "Vientiane Prefecture", "Vientiane Capital", "Phnom Penh", "Ulaanbaatar",
    "Delhi", "National Capital Territory of Delhi", "Beijing", "Shanghai", "Chongqing", "Tianjin", "Hong Kong",
    "Hong Kong SAR", "Macau", "Singapore", "Kuala Lumpur"])
def test_first_level_cities(text):
    assert pc.first_level_city(text)


@pytest.mark.parametrize("text", ["Kathmandu", "Colombo", "Chiang Mai", "Almaty Region", "Manila", "Yangon",
                                  "Bagmati", "Western Province", "", None])
def test_not_first_level_cities(text):
    assert not pc.first_level_city(text)


@pytest.mark.parametrize("region,name,country", [
    ("Almaty", "Almaty", "Kazakhstan"),
    ("Busan", "Busan", "South Korea"),
    ("Huế", "Huế", "Vietnam"),
    ("Bangkok", "Bangkok", "Thailand"),
    ("Almaty", "Panfilov Park", "Kazakhstan"),
    ("Huế", "A Lưới", "Vietnam"),
])
def test_a_first_level_city_is_a_right_region_even_for_itself(region, name, country):
    assert pc.region_problem(region, name=name, country=country) is None


def test_a_place_that_is_a_province_still_has_no_region():
    assert pc.region_problem("Arkhangai", name="Arkhangai Province", country="Mongolia") == "is_place"
    assert pc.region_problem("Pleiku", name="Pleiku", country="Vietnam") == "is_place"


# ── cross-border ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,countries,spans", [
    ("Myanmar–Thailand border", ("Thailand",), True),
    ("Wagah Border", ("India",), True),
    ("Mekong boundary", ("Laos",), True),
    ("Fourth Thai–Lao Friendship Bridge", ("Laos", "Laos and Thailand"), True),
    ("Fourth Thai–Lao Friendship Bridge", ("Laos",), False),        # one ISO country, no border word: no rule
    ("Old Bridge", ("Bosnia and Herzegovina",), False),             # one country with 'and' in its name
    ("Kodari", ("Nepal / Tibet",), True),
    ("Pashupatinath", ("Nepal", None, ""), False),
])
def test_cross_border(name, countries, spans):
    assert pc.cross_border(name, *countries) is spans


# ── exact numbers ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,out", [
    ("Kruba Srivichai (1878–1938) is remembered for leading the volunteer construction of the road to Wat Phra That "
     "Doi Suthep, completed in 1935.",
     "Kruba Srivichai is remembered for leading the volunteer construction of the road to Wat Phra That Doi Suthep, "
     "completed in 1935."),
    ("It was founded by Ōkuma Shigenobu (1838-1922).", "It was founded by Ōkuma Shigenobu."),
    ("King Mindon (born 1808) built the palace.", "King Mindon built the palace."),
    ("A statue of Admiral Yi Sun-sin (d. 1598) stands here.", "A statue of Admiral Yi Sun-sin stands here."),
    ("The poet Basho (c. 1644 – 1694) passed here.", "The poet Basho passed here."),
    # kept: a single year, a period after a lowercase word, no parentheses
    ("The Ōkuma Auditorium (1927) is a landmark.", "The Ōkuma Auditorium (1927) is a landmark."),
    ("The old hall (1905–1908) burned down.", "The old hall (1905–1908) burned down."),
    ("The park was laid out in 1872.", "The park was laid out in 1872."),
    # review of round 3: polity / period / event / building spans, reigns and non-life-span ranges keep their dates
    ("Ayutthaya Kingdom (1351–1767) was a Siamese kingdom.", "Ayutthaya Kingdom (1351–1767) was a Siamese kingdom."),
    ("Joseon Dynasty (1392-1897) built it.", "Joseon Dynasty (1392-1897) built it."),
    ("The Edo Period (1603–1868) saw the town grow.", "The Edo Period (1603–1868) saw the town grow."),
    ("The Khmer Empire (802–1431) ruled here.", "The Khmer Empire (802–1431) ruled here."),
    ("It was damaged in World War II (1939–1945).", "It was damaged in World War II (1939–1945)."),
    ("Suryavarman II (1113–1150) commissioned Angkor Wat.", "Suryavarman II (1113–1150) commissioned Angkor Wat."),
    ("The Meiji Restoration (1868–1889) changed it.", "The Meiji Restoration (1868–1889) changed it."),
    ("The Ōkuma Auditorium (1925–1927) is a landmark.", "The Ōkuma Auditorium (1925–1927) is a landmark."),
    ("Bagan Kingdom (849 to 1297) left temples.", "Bagan Kingdom (849 to 1297) left temples."),
    # still a person's life: BCE spans, a born / died year after a regnal name
    ("The Buddha (563–483 BCE) taught here.", "The Buddha taught here."),
    ("King Rama V (d. 1910) built it.", "King Rama V built it."),
])
def test_strip_life_dates(text, out):
    assert pc.strip_life_dates(text) == out


def test_writer_seed_drops_life_dates_unless_an_admin_verified_the_card():
    facts = [{"text": "Kruba Srivichai (1878–1938) led the road's construction.", "kind": "history"}]
    card = {"display_name": "Kruba Srivichai Monument", "country": "Thailand", "status": "auto_checked",
            "facts": json.dumps(facts)}
    assert writer_facts(card) == ["Kruba Srivichai led the road's construction."]
    assert writer_facts(dict(card, status="verified")) == [facts[0]["text"]]


def test_writer_seed_location_of_a_stored_border_card_drops_one_side():
    card = {"display_name": "Myanmar–Thailand border", "locality": "Mae Fa Luang", "region": "Chiang Rai",
            "country": "Thailand and Myanmar", "status": "auto_checked"}
    assert writer_identity(card, "Thai-Myanmar border") == ("", "")
    assert writer_identity(dict(card, status="verified"), "Thai-Myanmar border") == ("Mae Fa Luang", "Chiang Rai")


def test_prompts_carry_the_round_3_rules():
    assert "first-level unit" in pc._CARD_SYSTEM and "Almaty" in pc._CARD_SYSTEM
    assert "park, garden" in pc._CARD_SYSTEM and '"A / B"' in pc._CARD_SYSTEM
    assert "exact number" in pc._CARD_SYSTEM and "birth and death years" in pc._CARD_SYSTEM
    assert "first-level unit" in pc._CHECK_SYSTEM and '"A / B"' in pc._CHECK_SYSTEM
    assert "Check every exact number" in pc._CHECK_SYSTEM and "park, garden" in pc._CHECK_SYSTEM


# ── build path ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_build_of_a_border_drafted_with_a_compound_country_stores_no_side():
    draft = {"display_name": "Myanmar–Thailand border", "locality": "Mae Fa Luang", "region": "Chiang Rai",
             "country": "Thailand and Myanmar", "naming_note": "", "aliases": [],
             "facts": [{"text": "It is the international boundary between Myanmar and Thailand.",
                        "kind": "what_it_is"}]}
    db = FakeDB([_atom("a1", "Thai-Myanmar border", "Thailand")])
    llm = ScriptedLLM(draft)
    assert _ensure(db, llm, ["a1"])["linked_new"] == 1
    card = db.cards[db.links["a1"]["card_id"]]
    assert card["locality"] is None and card["region"] is None
    assert "locality: -, region: -" in llm.check_prompts[0]


def test_build_drops_life_dates_before_the_check_and_keeps_a_city_locality():
    draft = {"display_name": "Kruba Srivichai Monument", "locality": "Chiang Mai", "region": "Chiang Mai",
             "country": "Thailand", "naming_note": "", "aliases": [],
             "facts": [{"text": "Kruba Srivichai (1878–1938) was a revered Lanna monk.", "kind": "what_it_is"}]}
    db = FakeDB([_atom("a1", "Kruba Sri Vichai Statue", "Thailand")])
    llm = ScriptedLLM(draft)
    assert _ensure(db, llm, ["a1"])["linked_new"] == 1
    card = db.cards[db.links["a1"]["card_id"]]
    assert (card["locality"], card["region"]) == ("Chiang Mai", "Chiang Mai")
    assert [f["text"] for f in card["facts"]] == ["Kruba Srivichai was a revered Lanna monk."]
    assert "1878" not in llm.check_prompts[0]
