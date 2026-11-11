"""AA-197 [AA-193·F2]: seed builder, buyer-market resolver, DFS fetch_all shape."""

from unittest.mock import AsyncMock

import pytest

from services.seo_intelligence.seed_builder import (
    normalize_country, first_activity, build_seed, resolve_buyer_market, detect_region_landmark,
)
from services.seo_intelligence.dataforseo_client import DataForSEOClient


# ── normalize_country ─────────────────────────────────────────────────────────

def test_normalize_country_dirty_map():
    assert normalize_country("SRI-LANDKA") == "Sri Lanka"


def test_normalize_country_titlecase():
    assert normalize_country("south korea") == "South Korea"


def test_normalize_country_empty():
    assert normalize_country("") == ""
    assert normalize_country(None) == ""


# ── AA-571 Việc 1: landmark detection off tour title, independent of country column ───────────

def test_detect_region_landmark_okinawa_title():
    assert detect_region_landmark("Discover Okinawa 2026") == "Okinawa, Japan"


def test_detect_region_landmark_sublocation_naha():
    assert detect_region_landmark("Port Pick-Up: Naha Private Tour") == "Okinawa, Japan"


def test_detect_region_landmark_spelling_variant_yambaru():
    # real spelling variant found live in an actual title ("Yambaru", with b, vs "Yanbaru")
    assert detect_region_landmark("Family Fun in Yambaru National Park") == "Okinawa, Japan"


def test_detect_region_landmark_no_match():
    assert detect_region_landmark("Classic Tokyo-Kyoto-Osaka Highlights") is None


def test_detect_region_landmark_empty():
    assert detect_region_landmark("") is None
    assert detect_region_landmark(None) is None


def test_normalize_country_landmark_overrides_clean_country():
    # country column is already clean ("Japan") -- title alone drives the more specific result
    assert normalize_country("Japan", "Discover Okinawa 2026") == "Okinawa, Japan"


def test_normalize_country_no_landmark_falls_back_to_country():
    assert normalize_country("Japan", "Classic Tokyo-Kyoto-Osaka Highlights") == "Japan"


def test_build_seed_okinawa_landmark_from_title():
    # AA-571: country column is clean ("Japan"), landmark comes from tour_name, not country
    assert build_seed("Japan", None, "Discover Okinawa 2026") == "Discover Okinawa 2026 Okinawa, Japan"


def test_build_seed_okinawa_landmark_with_activity():
    assert build_seed("Japan", ["Snorkeling"], "Okinawa SUP & Snorkel Tour") == "Snorkeling in Okinawa, Japan"


# ── first_activity ────────────────────────────────────────────────────────────

def test_first_activity_comma():
    assert first_activity(["Cycling, Hiking, Trekking"]) == "Cycling"


def test_first_activity_pipe():
    # U+2502 box-drawing pipe, as seen in raw_tours
    assert first_activity(["Wildlife Safari│Cultural Tour│Beach"]) == "Wildlife Safari"


def test_first_activity_newline():
    assert first_activity(["Hiking \nkayaking \nCanyoning"]) == "Hiking"


def test_first_activity_none_and_empty():
    assert first_activity(None) == ""
    assert first_activity([]) == ""


# ── build_seed (incl. double-tours regression) ────────────────────────────────

def test_build_seed_country_and_activity():
    assert build_seed("South Korea", ["Cycling, Hiking"]) == "Cycling in South Korea"


def test_build_seed_country_only():
    assert build_seed("South Korea", None) == "South Korea tours"


def test_build_seed_no_double_tours():
    # dirty country + no activity must NOT yield "... tours tours"
    assert build_seed("SRI-LANDKA", None) == "Sri Lanka tours"


def test_build_seed_empty():
    assert build_seed("", None) == ""


# ── resolve_buyer_market ──────────────────────────────────────────────────────

def test_resolve_market_takes_the_first_declared_supported_market():
    # AA-1080: the agency's own order wins, not MARKET_RANK (before: US for every list that held it).
    code, name, lang = resolve_buyer_market(
        {"language": "en", "countries": ["AU", "UK", "US"]})
    assert code == 2036 and name == "Australia" and lang == "en"


def test_resolve_market_single_au():
    code, name, lang = resolve_buyer_market({"language": "en", "countries": ["AU"]})
    assert code == 2036 and name == "Australia"


def test_resolve_market_empty_defaults_us():
    code, _, _ = resolve_buyer_market({"countries": []})
    assert code == 2840


def test_resolve_market_language_passthrough():
    _, _, lang = resolve_buyer_market({"language": "en-GB", "countries": ["UK"]})
    assert lang == "en-GB"


# ── fetch_all shape (mocked HTTP) ─────────────────────────────────────────────

_FAKE_SERP = {"tasks": [{"result": [{"items": [
    {"type": "people_also_ask", "items": [{"title": "Q1?"}, {"title": "Q2?"}]},
    {"type": "related_searches", "items": ["related one", "related two"]},
]}]}]}


# keywords_for_keywords live shape: flat result[] of idea objects (incl. case near-dups)
_FAKE_IDEAS = {"tasks": [{"result": [
    {"keyword": "hiking in South Korea", "search_volume": 320, "competition": "LOW",
     "competition_index": 5, "cpc": 0.93},
    {"keyword": "hiking in south korea", "search_volume": 320, "competition": "LOW",
     "competition_index": 5, "cpc": 0.93},          # case dup of #1 → deduped
    {"keyword": "best hikes seoul", "search_volume": 110, "competition": "MEDIUM",
     "competition_index": 33, "cpc": 1.2},
    {"keyword": "", "search_volume": 0},             # empty → dropped
]}]}


# ── _parse_keyword_ideas (parse + dedupe casefold) ────────────────────────────

def test_parse_keyword_ideas_fields_and_dedupe():
    client = DataForSEOClient(login="x", password="y")
    ideas = client._parse_keyword_ideas(_FAKE_IDEAS)

    assert [i["keyword"] for i in ideas] == ["hiking in South Korea", "best hikes seoul"]
    first = ideas[0]
    assert first["search_volume"] == 320 and first["competition"] == "LOW"
    assert first["competition_index"] == 5 and first["cpc"] == 0.93


def test_parse_keyword_ideas_limit_25():
    client = DataForSEOClient(login="x", password="y")
    big = {"tasks": [{"result": [{"keyword": f"kw {i}"} for i in range(40)]}]}
    assert len(client._parse_keyword_ideas(big)) == 25


def test_parse_keyword_ideas_empty_on_bad_shape():
    client = DataForSEOClient(login="x", password="y")
    assert client._parse_keyword_ideas({}) == []


# ── fetch_all (3 calls; ideas = full dicts; promote fallback) ─────────────────

@pytest.mark.asyncio
async def test_fetch_all_preserves_consumer_shape_and_full_dict_ideas():
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keywords = AsyncMock(
        return_value={"top_keywords": ["korea tours"], "search_volumes": {}})
    client._serp_advanced = AsyncMock(return_value=_FAKE_SERP)
    client.fetch_keyword_ideas = AsyncMock(
        return_value=client._parse_keyword_ideas(_FAKE_IDEAS))

    out = await client.fetch_all("Cycling in South Korea", 2840, "United States", "en")

    # consumer keys (prompts.py L61-62) preserved
    assert out["keywords"]["top_keywords"] == ["korea tours"]  # search_volume kept, not overwritten
    assert out["people_also_ask"] == ["Q1?", "Q2?"]
    assert out["related_keywords"] == ["related one", "related two"]
    # keyword_ideas are FULL DICTS (volume/competition/cpc), not flattened strings
    assert isinstance(out["keyword_ideas"][0], dict)
    assert out["keyword_ideas"][0]["search_volume"] == 320
    assert "cpc" in out["keyword_ideas"][0]


@pytest.mark.asyncio
async def test_fetch_all_promotes_ideas_when_top_keywords_empty():
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keywords = AsyncMock(return_value={})  # no search volume data
    client._serp_advanced = AsyncMock(return_value=_FAKE_SERP)
    client.fetch_keyword_ideas = AsyncMock(
        return_value=client._parse_keyword_ideas(_FAKE_IDEAS))

    out = await client.fetch_all("Sri Lanka tours", 2840, "United States", "en")

    # promoted: top_keywords filled from ideas[:10] so prompt always has a keyword
    assert out["keywords"]["top_keywords"] == ["hiking in South Korea", "best hikes seoul"]
    # keyword_ideas remain full dicts
    assert isinstance(out["keyword_ideas"][0], dict)
