"""S207 pre-rerun fixes: source day parser formats, lodging ideas dropped, Jev sees research-cache
ideas, single-seed S1 titles still reuse / skip the search_volume task."""
import os
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from services.content_generation.prompts import parse_source_day_word_counts as parse
from services.seo_intelligence import s1_prefetch as P
from services.seo_intelligence.dataforseo_client import DataForSEOClient
from services.seo_intelligence.seed_builder import is_lodging_search, rank_keyword_ideas

structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=open(os.devnull, "w")))


@pytest.mark.parametrize("text,days", [
    ("Day One: Taipei (Arrival Day)\nArrive.\n\nDay Two: Taipei | Jiaoxi\nRide out.", [1, 2]),
    ("► Day 01: Arrival – Negombo\nUpon arrival.\n► Day 02: Kandy\nTemple.", [1, 2]),
    ("Days 1–3: Paro arrival\nAcclimatise.\nDay 4: Trek begins.", [1, 4]),
    ("**Day 3** Hike\ntext\n**Day 4** Rest", [3, 4]),
])
def test_day_parser_formats(text, days):
    r = parse(text, "5 days")
    assert not r["used_fallback"] and sorted(r["day_text"]) == days


def test_day_parser_ignores_mid_text_and_daylight():
    r = parse("Easy Day 82 km Tarmac Road\nDaylight ride\nDay 1 Start\nDay 2 End", "2 days")
    assert sorted(r["day_text"]) == [1, 2]


def test_lodging_ideas_dropped_from_ranking():
    assert is_lodging_search("druk hotel paro") and not is_lodging_search("paro airport to hotel")
    ideas = [{"keyword": "druk hotel paro", "search_volume": 500}, {"keyword": "druk path trek", "search_volume": 300}]
    assert [i["keyword"] for i in rank_keyword_ideas(ideas, ["druk"])] == ["druk path trek"]


class _Conn:
    def __init__(self, demand):
        self.demand, self.inserted = demand, []

    async def fetch(self, sql, *a):
        return [] if "seo_context" in sql else self.demand

    async def fetchrow(self, sql, *a):
        self.inserted.append(a)
        return {"id": "x"}


@pytest.mark.asyncio
async def test_jev_sees_research_cache_candidates_without_lodging():
    rows = [{"tour_id": "00000000-0000-0000-0000-00000000000a", "src_name": "THE DRUK PATH",
             "country": "Bhutan", "activities": None}]
    demand = [{"keyword": f"druk path trek {i}", "search_volume": 100 + i, "people_also_ask": []} for i in range(6)]
    demand.append({"keyword": "druk hotel paro", "search_volume": 900, "people_also_ask": []})
    client = DataForSEOClient(login="x", password="y")
    client._serp_advanced = AsyncMock(return_value={})
    seen = {}

    async def fake_review(spec, cands, kept, pool=None):
        seen["cands"] = [c["keyword"] for c in cands]
        return kept
    with patch.object(P, "jev_review", side_effect=fake_review):
        await P.prefetch(_Conn(demand), rows, tenant_id="t", location_code=2840, language_code="en",
                         client=client, jev=True)
    assert "druk hotel paro" not in seen["cands"] and len(seen["cands"]) == 6


@pytest.mark.asyncio
async def test_single_seed_title_uses_multi_path_without_search_volume():
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keywords = AsyncMock(side_effect=AssertionError("search_volume not bought"))
    client._serp_advanced = AsyncMock(return_value={})
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[])
    out = await client.fetch_all("LAYA-GASA TREK Bhutan", extra_seeds=[], place_terms=["laya-gasa"])
    assert out["keywords"]["top_keywords"] == ["LAYA-GASA TREK Bhutan"]
    client.fetch_keyword_ideas_multi.assert_awaited_once()


# ── S207 pilot 2: seeds for hyphenated / all-caps / country-in-title tour names ──

def test_hyphenated_title_splits_into_place_terms_and_matches_spaced_ideas():
    from services.seo_intelligence.seed_builder import rank_keyword_ideas, title_place_terms
    places = title_place_terms("LAYA-GASA TREK", "Bhutan")
    assert places[:2] == ["laya", "gasa"]
    kept = rank_keyword_ideas([{"keyword": "laya gasa trek", "search_volume": 90},
                               {"keyword": "bhutan trek", "search_volume": 900}], places)
    assert [i["keyword"] for i in kept] == ["laya gasa trek"]


def test_all_caps_title_is_lowercased_and_country_not_doubled():
    from services.seo_intelligence.seed_builder import build_seed, idea_seeds
    assert build_seed("Bhutan", None, "CULINARY GEMS OF BHUTAN") == "culinary gems of bhutan"
    assert build_seed("Bhutan", None, "LAYA-GASA TREK") == "laya-gasa trek Bhutan"
    assert "laya gasa trek" in idea_seeds("Bhutan", None, "LAYA-GASA TREK")


def test_mixed_case_title_unchanged():
    from services.seo_intelligence.seed_builder import build_seed
    assert build_seed("South Korea", None, "Korea's Coast-to-Coast Ride") == \
        "Korea's Coast-to-Coast Ride South Korea"


# ── S207 Korea trial: wrong keywords that reached S1 ──

def _kept(name, country, kws, acts=None):
    from services.seo_intelligence import s1_prefetch as P
    from services.seo_intelligence.seed_builder import rank_keyword_ideas
    s = P.tour_spec({"tour_id": "x", "src_name": name, "country": country, "activities": acts})
    return [i["keyword"] for i in rank_keyword_ideas([{"keyword": k, "search_volume": 100} for k in kws],
                                                     s["places"], activity_words=s["activity"], country=country)]


def test_life_is_generic_and_whole_words_only():
    assert _kept("12 Day Life & Culture Of South Korea - Wednesday Departure", "South Korea",
                 ["wildlife rescue center", "Jeju island nightlife", "night life in Jeju"]) == []


def test_other_country_keyword_dropped_unless_title_names_it():
    assert _kept("Bukchon Hanok Village Walk", "South Korea",
                 ["bukchon hanok village", "village life bhutan bukchon"]) == ["bukchon hanok village"]
    assert _kept("Nepal Tibet Bhutan Tour", "Nepal", ["tibet tour", "india tour packages"]) == ["tibet tour"]


def test_departure_city_and_transfers_are_not_the_tour():
    assert _kept("3D2N Gangwon Biking, Hiking & Surfing Tour from Seoul", "South Korea",
                 ["jeju island from seoul", "seoul airport transfer", "surfing in gangwon"]) == ["surfing in gangwon"]


def test_activity_matches_inflected_form():
    kept = _kept("Kang Yatse", "India", ["mountaineering in india"], acts='["Mountaineer"]')
    assert kept == ["mountaineering in india"]


# ── S207 Taiwan trial: other countries' places from the research cache, seed tails ──

def test_foreign_place_keywords_dropped():
    from services.seo_intelligence import s1_prefetch as P
    from services.seo_intelligence.seed_builder import rank_keyword_ideas
    s = P.tour_spec({"tour_id": "x", "src_name": "Ancient Trails & Hot Springs", "country": "Taiwan",
                     "activities": None})
    ideas = [{"keyword": k, "search_volume": 100} for k in
             ("ancient city of polonnaruwa", "paro hot stone bath", "gasa hot springs", "beitou hot springs")]
    kept = rank_keyword_ideas(ideas, s["places"] + ["beitou"], country="Taiwan",
                              foreign_places=frozenset({"polonnaruwa", "paro", "gasa"}))
    assert [i["keyword"] for i in kept] == ["beitou hot springs"]


def test_generic_title_words_alone_do_not_make_a_keyword_relevant():
    from services.seo_intelligence import s1_prefetch as P
    from services.seo_intelligence.seed_builder import rank_keyword_ideas
    s = P.tour_spec({"tour_id": "x", "src_name": "Ancient Trails & Hot Springs", "country": "Taiwan",
                     "activities": None})
    assert rank_keyword_ideas([{"keyword": "ancient city wall", "search_volume": 50}], s["places"],
                              country="Taiwan") == []


def test_seed_drops_duration_and_guided_tail():
    from services.seo_intelligence.seed_builder import build_seed
    name = "Alishan Indigenous Culture and Tea Experiential Tour – 3-Days / 2-Nights (Guided)"
    seed = build_seed("Taiwan", None, name)
    assert seed == "Alishan Indigenous Culture and Tea Experiential Tour Taiwan"


@pytest.mark.asyncio
async def test_foreign_places_excludes_own_country_names():
    from services.seo_intelligence import s1_prefetch as P

    class C:
        async def fetch(self, sql, *a):
            return [{"n": "paro", "country": "Bhutan"}, {"n": "taipei", "country": "Taiwan"},
                    {"n": "jiufen", "country": "Taiwan"}, {"n": "temple", "country": "Bhutan"}]
    fp = await P.foreign_places(C(), "Taiwan")
    assert fp == frozenset({"paro"})
