"""AA-648 — batch research: few large paid DFS tasks instead of many tiny ones. Mock-only."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import segment_research as sr
from services.acp_contract import segment_research_batch as rb
from services.seo_intelligence.dataforseo_client import DFSCallError
from shared.llm_client.models import LLMResponse

US = [(2840, "United States", "en")]
US_UK = [(2840, "United States", "en"), (2826, "United Kingdom", "en")]


def _pool(cached_rows=None, countries=None):
    conn = MagicMock()

    async def _fetch(sql, *args):
        if "mode() WITHIN GROUP" in sql:
            return [{"canonical_place": p, "country": c} for p, c in (countries or {}).items()]
        return cached_rows or []

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _llm(content: str):
    fake = MagicMock()
    fake.generate.return_value = LLMResponse(content=content, model_used="haiku-4-5", provider="bedrock",
                                             input_tokens=100, output_tokens=50, cost_usd=0.004)
    return fake


PROPOSALS = ('{"places": ['
             '{"place": "Paro", "keywords": ["paro", "paro festival"]},'
             '{"place": "Thimphu", "keywords": ["thimphu", "thimphu weekend market"]},'
             '{"place": "Haa Valley", "keywords": ["haa valley"]}]}')


# ── pure helpers ───────────────────────────────────────────────────────────────────────────────

def test_estimate_is_a_few_tasks_not_one_per_place():
    est = rb.estimate_cost(2222, 3)
    assert est["volume_tasks"] == 3 * 9            # ceil(2222*4/1000) = 9 per market
    assert est["idea_tasks"] == 112                # ceil(2222/20)
    assert est["llm_calls"] == 149                 # ceil(2222/15)
    assert est["dfs_usd"] < 40                     # vs $49 spent on 25/09 for a partial pass


def test_estimate_zero_when_nothing_selected():
    assert rb.estimate_cost(0, 3)["dfs_usd"] == 0.0


def test_parse_proposals_handles_fences_and_bad_items():
    raw = '```json\n{"places": [{"place": "Paro", "keywords": ["Paro Dzong!", ""]}, {"x": 1}]}\n```'
    assert rb._parse_proposals(raw) == {"paro": ["paro dzong"]}
    assert rb._parse_proposals("not json") == {}


# ── end to end over mocks ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_buys_one_volume_task_for_all_places_and_logs_researched():
    pool, conn = _pool(cached_rows=[{"keyword": "paro", "search_volume": 5000}],
                       countries={"Haa Valley": "Bhutan"})
    client = MagicMock()
    client.fetch_volumes_bulk = AsyncMock(return_value={"paro festival": 800, "thimphu": 2000,
                                                        "thimphu weekend market": 0, "haa valley": None})
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[{"keyword": "Haa Summer Festival",
                                                                "search_volume": 90}])
    guard = sr._RunGuard()
    stale = [("Paro", ["explore"], ["US"]), ("Thimphu", ["walk"], ["US"]), ("Haa Valley", ["drive"], ["US"])]

    with patch.object(rb, "LLMClient", return_value=_llm(PROPOSALS)), \
         patch.object(rb, "record_call_with_pool", new=AsyncMock()), \
         patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()) as m_cache_log, \
         patch.object(rb, "_serp_tool", new=AsyncMock(return_value=["q"])) as m_serp, \
         patch.object(rb, "IDEAS_MIN_SEEDS", 1):
        result = await rb.research_batch(stale, US, pool, client, guard)

    # one paid volume task carrying every uncached keyword (the cached "paro" is not re-bought)
    client.fetch_volumes_bulk.assert_awaited_once()
    bought = client.fetch_volumes_bulk.await_args.args[0]
    assert sorted(bought) == ["haa valley", "paro festival", "thimphu", "thimphu weekend market"]
    m_cache_log.assert_awaited_once()                       # one aggregated cache-hit row
    assert m_cache_log.await_args.kwargs["keyword_count"] == 1
    # SERP only for each place's best keyword with volume (Paro, Thimphu), not Haa Valley
    assert sorted(c.args[1] for c in m_serp.await_args_list) == ["paro", "thimphu"]
    # suggestions: one multi-seed task for the zero-volume place
    client.fetch_keyword_ideas_multi.assert_awaited_once()
    assert client.fetch_keyword_ideas_multi.await_args.args[0] == ["haa valley bhutan"]
    # all 3 places marked researched in one executemany
    log_calls = [c for c in conn.executemany.await_args_list if "segment_research_log" in c.args[0]]
    assert len(log_calls) == 1 and len(log_calls[0].args[1]) == 3
    assert result["places_researched"] == 3 and result["volume_tasks"] == 1 and result["llm_calls"] == 1


def test_idea_seed_adds_country_once_and_caps_words():
    assert rb._idea_seed(rb._Place("Mongar", [], ["US"]), "bhutan") == "mongar bhutan"
    assert rb._idea_seed(rb._Place("Paro, Bhutan", [], ["US"]), "bhutan") == "paro bhutan"
    assert rb._idea_seed(rb._Place("Ura Valley", [], ["US"]), None) == "ura valley"
    long = rb._Place(" ".join(f"w{i}" for i in range(12)), [], ["US"])
    assert len(rb._idea_seed(long, "nepal").split()) == 10


@pytest.mark.asyncio
async def test_suggestion_seeds_use_place_country_not_the_zero_volume_keyword():
    # 28/09/2026: seeds were the places' own zero-volume keywords and DFS echoed them back (0 ideas)
    pool, conn = _pool(countries={"Mongar": "Bhutan", "Ura Valley": "Bhutan"})
    places = [rb._Place("Mongar", [], ["US"], keywords=["mongar to bumthang"]),
              rb._Place("Ura Valley", [], ["US"], keywords=["ura valley"]),
              rb._Place("Somewhere", [], ["US"], keywords=["somewhere x"])]
    client = MagicMock()
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[
        {"keyword": "mongar bhutan", "search_volume": 40},
        {"keyword": "hotels in mongar", "search_volume": 10},
        {"keyword": "ura valley bhutan", "search_volume": None}])
    with patch.object(rb, "IDEAS_MIN_SEEDS", 1):
        stats = await rb._buy_suggestions(places, US, client, pool, sr._RunGuard())
    assert client.fetch_keyword_ideas_multi.await_args.args[0] == [
        "mongar bhutan", "ura valley bhutan", "somewhere"]
    stored = [c.args[1] for c in conn.executemany.await_args_list if "search_demand" in c.args[0]][0]
    assert stored == [("mongar bhutan", "US", 40), ("hotels in mongar", "US", 10)]
    assert stats["idea_tasks"] == 1 and stats["ideas_stored"] == 2


@pytest.mark.asyncio
async def test_ideas_task_skipped_below_min_seeds():
    pool, conn = _pool(countries={"Robluthang": "Bhutan"})
    client = MagicMock()
    client.fetch_keyword_ideas_multi = AsyncMock()
    places = [rb._Place("Robluthang", [], ["US"])]
    stats = await rb._buy_suggestions(places, US, client, pool, sr._RunGuard())
    client.fetch_keyword_ideas_multi.assert_not_awaited()
    assert stats == {"idea_tasks": 0, "ideas_stored": 0, "idea_tasks_skipped": 1}
    assert not places[0].failed          # skipping is not a purchase failure


@pytest.mark.asyncio
async def test_fatal_volume_failure_aborts_and_marks_nothing_researched():
    pool, conn = _pool()
    client = MagicMock()
    client.fetch_volumes_bulk = AsyncMock(side_effect=DFSCallError("Payment Required", True, 40200))
    client.fetch_keyword_ideas_multi = AsyncMock()
    guard = sr._RunGuard()

    with patch.object(rb, "LLMClient", return_value=_llm(PROPOSALS)), \
         patch.object(rb, "record_call_with_pool", new=AsyncMock()), \
         patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()), \
         patch.object(rb, "_serp_tool", new=AsyncMock()) as m_serp:
        result = await rb.research_batch([("Paro", ["explore"], ["US"])], US, pool, client, guard)

    assert guard.aborted
    assert result["places_researched"] == 0
    assert not [c for c in conn.executemany.await_args_list if "segment_research_log" in c.args[0]]
    assert not [c for c in conn.executemany.await_args_list if "search_demand" in c.args[0]]
    m_serp.assert_not_awaited()
    client.fetch_keyword_ideas_multi.assert_not_awaited()


@pytest.mark.asyncio
async def test_volume_tasks_split_at_task_limit_per_market():
    pool, _ = _pool()
    client = MagicMock()
    client.fetch_volumes_bulk = AsyncMock(side_effect=lambda kws, *a, **k: {kw: 10 for kw in kws})
    places = [rb._Place(f"p{i}", [], ["US", "UK"], keywords=[f"kw{i}"]) for i in range(5)]
    with patch.object(rb, "VOLUME_TASK_MAX", 2), patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()):
        stats = await rb._buy_volumes(places, US_UK, client, pool, sr._RunGuard())
    assert stats["volume_tasks"] == 6          # ceil(5/2)=3 per market x 2 markets
    assert all(p.volumes[p.keywords[0]] == {"US": 10, "UK": 10} for p in places)


@pytest.mark.asyncio
async def test_unusable_llm_output_still_researches_the_plain_place_name():
    places = [rb._Place("Paro Dzong", ["visit"], ["US"])]
    pool, _ = _pool()
    with patch.object(rb, "LLMClient", return_value=_llm("garbage")), \
         patch.object(rb, "record_call_with_pool", new=AsyncMock()):
        await rb._propose(places, pool, sr._RunGuard(), None)
    assert places[0].keywords == ["paro dzong"]


@pytest.mark.asyncio
async def test_run_segment_research_dry_run_returns_batch_estimate():
    pool, conn = _pool()
    conn.fetch = AsyncMock(side_effect=[
        [{"canonical_place": p, "canonical_action": "explore"} for p in ("a", "b")], [], [],
    ])
    result = await sr.run_segment_research({"countries": ["US"]}, pool, max_places=10, dry_run=True)
    assert result["strategy"] == "batch"
    assert result["estimate"]["volume_tasks"] == 1 and result["estimate"]["llm_calls"] == 1


def test_unknown_strategy_rejected():
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(sr.run_segment_research({"countries": ["US"]}, MagicMock(), strategy="nope"))
