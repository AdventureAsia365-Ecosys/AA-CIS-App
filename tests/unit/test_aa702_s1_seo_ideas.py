"""AA-702 — S1 DataForSEO keyword ideas from several seeds in one task, ranked by real volume with
tour-specific ideas first; volume-less search_volume rows are not "top keywords"; superseded
sources are excluded from v_trip_registry (migration 195)."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from services.seo_intelligence.dataforseo_client import DataForSEOClient
from services.seo_intelligence.seed_builder import (
    activity_terms, build_seed, first_activity, idea_seeds, rank_keyword_ideas, title_place_terms,
)


def test_title_place_terms_strip_generic_words_numbers_and_country():
    assert title_place_terms("Kang Yatse 2 and the Lhato Valley — 15 Days", "India") == \
        ["kang", "yatse", "lhato", "valley"]
    assert title_place_terms("South Korea: Seoul to Jeju — 13 Days", "South Korea") == ["seoul", "jeju"]
    assert title_place_terms("Classic Exploration", "Sri Lanka") == []


def test_idea_seeds_specific_then_places_no_generic_country_seed():
    assert idea_seeds("India", None, "Kang Yatse 2 and the Lhato Valley") == [
        "Kang Yatse 2 and the Lhato Valley India", "kang yatse lhato valley India"]
    # no place words in the title and no activity -> only the specific seed
    assert idea_seeds("Sri Lanka", None, "Classic Exploration") == ["Classic Exploration Sri Lanka"]


def test_activities_json_string_reaches_the_seed():
    # S1 passes raw_tours.activities as JSON text (no jsonb codec on the pool)
    assert first_activity('["Mountaineer"]') == "Mountaineer"
    assert build_seed("India", '["Mountaineer"]', "Kang Yatse") == "Mountaineer in India"
    assert activity_terms('["Wildlife Safari"]') == ["wildlife", "safari"]
    assert idea_seeds("India", '["Mountaineer"]', "Kang Yatse 2 and the Lhato Valley") == [
        "Mountaineer in India", "kang yatse lhato valley India", "India Mountaineer"]


def test_rank_keyword_ideas_drops_generic_country_ideas():
    # real Dev result for Kang Yatse (Ladakh mountaineering) before the relevance filter
    ideas = [
        {"keyword": "india tours", "search_volume": 1600},
        {"keyword": "golden triangle india", "search_volume": 1600},
        {"keyword": "kerala trip package", "search_volume": 720},
        {"keyword": "mountaineering in india", "search_volume": 90},
        {"keyword": "kang yatse peak", "search_volume": 40},
        {"keyword": "kang yatse 2 climb", "search_volume": None},
        {"keyword": "markha valley trek", "search_volume": 700},
    ]
    ranked = [i["keyword"] for i in rank_keyword_ideas(
        ideas, ["kang", "yatse", "lhato", "valley"], activity_words=["mountaineer"])]
    # place-named first, then activity-only; "valley" alone is too weak to keep "markha valley trek"
    assert ranked == ["kang yatse peak", "mountaineering in india", "kang yatse 2 climb"]


def test_parse_keywords_drops_volume_less_rows():
    client = DataForSEOClient(login="x", password="y")
    data = {"tasks": [{"result": [
        {"keyword": "Kang Yatse 2 and the Lhato Valley India", "search_volume": None},
        {"keyword": "ladakh trek", "search_volume": 50},
        {"keyword": "markha valley trek", "search_volume": 700},
    ]}]}
    out = client._parse_keywords(data)
    assert out["top_keywords"] == ["markha valley trek", "ladakh trek"]
    assert client._parse_keywords({"tasks": [{"result": [{"keyword": "x", "search_volume": None}]}]}) == {}


@pytest.mark.asyncio
async def test_fetch_all_multi_seed_one_ideas_task_and_ranked_promotion():
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keywords = AsyncMock(return_value={})  # tour-name seed: no volume
    client._serp_advanced = AsyncMock(return_value={})
    client.fetch_keyword_ideas = AsyncMock(side_effect=AssertionError("single-seed path not expected"))
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[
        {"keyword": "india tours", "search_volume": 9000},
        {"keyword": "ladakh cycling tour", "search_volume": 90},
        {"keyword": "siachen glacier", "search_volume": None},
    ])
    out = await client.fetch_all("Ladakh Siachen India", 2840, "United States", "en",
                                 extra_seeds=["ladakh siachen India", "siachen India", "India tours"],
                                 place_terms=["ladakh", "siachen"])
    client.fetch_keyword_ideas_multi.assert_awaited_once()
    seeds = client.fetch_keyword_ideas_multi.await_args.args[0]
    assert seeds == ["Ladakh Siachen India", "siachen India", "India tours"]  # case-insensitive dedup
    # generic "india tours" dropped; volume-less place idea kept after
    assert [i["keyword"] for i in out["keyword_ideas"]] == ["ladakh cycling tour", "siachen glacier"]
    assert out["keywords"]["top_keywords"][0] == "ladakh cycling tour"


@pytest.mark.asyncio
async def test_fetch_all_keeps_seed_when_nothing_has_volume():
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keywords = AsyncMock(return_value={})
    client._serp_advanced = AsyncMock(return_value={})
    client.fetch_keyword_ideas_multi = AsyncMock(side_effect=RuntimeError("dfs down"))
    out = await client.fetch_all("Classic Exploration Sri Lanka", extra_seeds=["Sri Lanka tours"])
    assert out["keyword_ideas"] == []
    assert out["keywords"]["top_keywords"] == ["Classic Exploration Sri Lanka"]


def test_migration_195_view_active_sources_only():
    sql = (Path(__file__).resolve().parents[2] / "api/migrations/195_v_trip_registry_active_sources.sql").read_text()
    assert "WHERE rt.source_status = 'active'" in sql
    assert "!= 'trashed'" not in sql
    assert "VALUES ('195'" in sql
