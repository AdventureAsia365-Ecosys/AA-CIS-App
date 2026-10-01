"""AA-702 — S1 DataForSEO keyword ideas from several seeds in one task, ranked by real volume with
tour-specific ideas first; volume-less search_volume rows are not "top keywords"; superseded
sources are excluded from v_trip_registry (migration 195)."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from services.seo_intelligence.dataforseo_client import DataForSEOClient
from services.seo_intelligence.seed_builder import (
    idea_seeds, rank_keyword_ideas, title_place_terms,
)


def test_title_place_terms_strip_generic_words_numbers_and_country():
    assert title_place_terms("Kang Yatse 2 and the Lhato Valley — 15 Days", "India") == \
        ["kang", "yatse", "lhato", "valley"]
    assert title_place_terms("South Korea: Seoul to Jeju — 13 Days", "South Korea") == ["seoul", "jeju"]
    assert title_place_terms("Classic Exploration", "Sri Lanka") == []


def test_idea_seeds_specific_first_then_places_then_generic():
    assert idea_seeds("India", None, "Kang Yatse 2 and the Lhato Valley") == [
        "Kang Yatse 2 and the Lhato Valley India", "kang yatse lhato valley India", "India tours"]
    # no place words in the title -> specific seed + generic only, no duplicates
    assert idea_seeds("Sri Lanka", None, "Classic Exploration") == [
        "Classic Exploration Sri Lanka", "Sri Lanka tours"]


def test_idea_seeds_adds_country_activity_when_known():
    seeds = idea_seeds("Nepal", ["Trekking, Rafting"], "Manaslu Circuit")
    assert seeds[0] == "Trekking in Nepal"
    assert "Nepal Trekking" in seeds and "Nepal tours" in seeds
    assert len(seeds) <= 20


def test_rank_keyword_ideas_volume_then_specific_then_generic():
    ideas = [
        {"keyword": "india tours", "search_volume": 9000},
        {"keyword": "no volume idea", "search_volume": None},
        {"keyword": "kathmandu valley tour", "search_volume": 5000},
        {"keyword": "ladakh bike trip", "search_volume": 300},
        {"keyword": "zero", "search_volume": 0},
    ]
    ranked = [i["keyword"] for i in rank_keyword_ideas(ideas, ["ladakh", "valley"])]
    # "valley" is a weak geo word: it must not make "kathmandu valley tour" look tour-specific
    assert ranked == ["ladakh bike trip", "india tours", "kathmandu valley tour", "no volume idea", "zero"]


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
        {"keyword": "kang yatse", "search_volume": None},
    ])
    out = await client.fetch_all("Ladakh Siachen India", 2840, "United States", "en",
                                 extra_seeds=["ladakh siachen India", "siachen India", "India tours"],
                                 place_terms=["ladakh", "siachen"])
    client.fetch_keyword_ideas_multi.assert_awaited_once()
    seeds = client.fetch_keyword_ideas_multi.await_args.args[0]
    assert seeds == ["Ladakh Siachen India", "siachen India", "India tours"]  # case-insensitive dedup
    assert [i["keyword"] for i in out["keyword_ideas"]] == ["ladakh cycling tour", "india tours", "kang yatse"]
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
