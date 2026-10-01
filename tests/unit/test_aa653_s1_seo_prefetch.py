"""AA-653 — S1 DataForSEO prefetch: batch seeds per task, assign ideas back per tour, research
cache first, long reuse of a tour's own seo_context, no separate search_volume task."""
import json
from unittest.mock import AsyncMock

import pytest

from services.seo_intelligence import s1_prefetch as P
from services.seo_intelligence.dataforseo_client import DataForSEOClient


def _row(tid, name, country, acts=None):
    return {"tour_id": tid, "src_name": name, "country": country, "activities": acts}


def test_plan_batches_groups_country_and_caps_seeds():
    specs = [P.tour_spec(_row(f"t{i}", f"Paro Thimphu Punakha {i}", "Bhutan")) for i in range(12)]
    specs += [P.tour_spec(_row("n1", "Annapurna Circuit", "Nepal"))]
    batches = P.plan_batches(specs, max_seeds=20)
    assert all(len({s["country"] for s in b}) == 1 for b in batches)
    assert all(sum(len(s["seeds"]) for s in b) <= 20 for b in batches)
    assert sum(len(b) for b in batches) == 13


def test_assign_ideas_keeps_other_tours_places_out():
    a = P.tour_spec(_row("a", "Everest Base Camp Trek", "Nepal", '["Trekking"]'))
    b = P.tour_spec(_row("b", "Annapurna Circuit", "Nepal", '["Trekking"]'))
    ideas = [{"keyword": "everest base camp trekking", "search_volume": 9000},
             {"keyword": "annapurna circuit trek", "search_volume": 4000},
             {"keyword": "trekking in nepal", "search_volume": 3000},
             {"keyword": "nepal tour packages", "search_volume": 8000}]
    out = P.assign_ideas([a, b], ideas)
    assert [i["keyword"] for i in out["a"]] == ["everest base camp trekking", "trekking in nepal"]
    assert [i["keyword"] for i in out["b"]] == ["annapurna circuit trek", "trekking in nepal"]


def test_cached_ideas_from_search_demand():
    s = P.tour_spec(_row("a", "Paro Taktsang Hike", "Bhutan"))
    rows = [{"keyword": "taktsang monastery hike", "search_volume": 2400,
             "people_also_ask": ["How long is the Tiger's Nest hike?"]},
            {"keyword": "bhutan tours", "search_volume": 5000, "people_also_ask": []}]
    ideas, paa = P.cached_ideas(s, rows)
    assert [i["keyword"] for i in ideas] == ["taktsang monastery hike"]
    assert paa == ["How long is the Tiger's Nest hike?"]


class _Conn:
    def __init__(self, fresh=(), demand=()):
        self.fresh, self.demand, self.inserted = set(fresh), list(demand), []

    async def fetch(self, sql, *args):
        if "seo_context" in sql:
            return [{"tour_id": t} for t in args[0] if t in self.fresh]
        if "search_demand" in sql:
            return self.demand
        return []

    async def fetchrow(self, sql, *args):
        self.inserted.append(args)
        return {"id": "x"}


@pytest.mark.asyncio
async def test_prefetch_reuses_buys_batched_and_skips_search_volume():
    rows = [_row("00000000-0000-0000-0000-00000000000a", "Paro Taktsang Hike", "Bhutan"),
            _row("00000000-0000-0000-0000-00000000000b", "Bumthang Owl Trek", "Bhutan"),
            _row("00000000-0000-0000-0000-00000000000c", "Haa Valley Retreat", "Bhutan")]
    conn = _Conn(fresh={"00000000-0000-0000-0000-00000000000c"})
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keywords = AsyncMock(side_effect=AssertionError("search_volume must not be bought"))
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[
        {"keyword": "tiger's nest taktsang hike", "search_volume": 1900},
        {"keyword": "bumthang owl trek", "search_volume": 50}])
    client._serp_advanced = AsyncMock(return_value={})
    summary = await P.prefetch(conn, rows, tenant_id="t", location_code=2840, language_code="en",
                               client=client)
    assert summary == {"tours": 3, "reused": 1, "from_research_cache": 0, "ideas_tasks": 1, "serp_calls": 2}
    client.fetch_keyword_ideas_multi.assert_awaited_once()
    # two rows written (the reused one is untouched), each keyed with the v3 marker
    assert len(conn.inserted) == 2
    keys = [a[8] for a in conn.inserted]
    assert all(k.endswith(":ideas_v3") for k in keys)
    by_tour = {a[0]: json.loads(a[7]) for a in conn.inserted}
    assert by_tour["00000000-0000-0000-0000-00000000000a"] == ["tiger's nest taktsang hike"]
    assert by_tour["00000000-0000-0000-0000-00000000000b"] == ["bumthang owl trek"]


@pytest.mark.asyncio
async def test_prefetch_research_cache_alone_buys_nothing():
    rows = [_row("00000000-0000-0000-0000-00000000000a", "Paro Taktsang Hike", "Bhutan")]
    demand = [{"keyword": f"taktsang hike {i}", "search_volume": 100 + i,
               "people_also_ask": json.dumps(["Q?"])} for i in range(6)]
    conn = _Conn(demand=demand)
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keyword_ideas_multi = AsyncMock(side_effect=AssertionError("nothing to buy"))
    client._serp_advanced = AsyncMock(side_effect=AssertionError("PAA came from the cache"))
    summary = await P.prefetch(conn, rows, tenant_id="t", location_code=2840, language_code="en",
                               client=client)
    assert summary["from_research_cache"] == 1 and summary["ideas_tasks"] == 0
    assert summary["serp_calls"] == 0


@pytest.mark.asyncio
async def test_fetch_all_multi_seed_skips_search_volume_task():
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keywords = AsyncMock(side_effect=AssertionError("not bought"))
    client._serp_advanced = AsyncMock(return_value={})
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[
        {"keyword": "Mountaineer in India", "search_volume": 40}])
    out = await client.fetch_all("Mountaineer in India", extra_seeds=["kang yatse India"],
                                 place_terms=["kang", "yatse"], activity_words=["mountaineer"])
    assert out["keywords"]["top_keywords"] == ["Mountaineer in India"]
