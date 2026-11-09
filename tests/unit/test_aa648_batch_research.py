"""AA-648 / AA-1055 — batch research: few large paid DFS tasks, a stored keyword map, one SERP per place, logs per place
as soon as it is done. Mock-only; invented place names."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import segment_research as sr
from services.acp_contract import segment_research_batch as rb
from services.seo_intelligence.dataforseo_client import DFSCallError
from shared.llm_client.decide import Decision, Verdict
from shared.llm_client.models import LLMResponse

US = [(2840, "United States", "en")]
US_UK = [(2840, "United States", "en"), (2826, "United Kingdom", "en")]


def _decision(key: str, zone: str = "grey", mode: str = "shadow") -> Decision:
    return Decision(verdicts={key: Verdict(key, mode, zone)})


@pytest.fixture(autouse=True)
def _no_jev():
    """AA-693: research asks Jev; by default every verdict is a shadow grey (changes nothing)."""
    async def fake(stage, subject, state, keys, **kw):
        return _decision(keys[0])
    with patch.object(rb, "decide", new=fake):
        yield


def _pool(cached=None, countries=None, serp_rows=None, maps=None):
    """cached: {market: {keyword: volume}} fresh in the store; serp_rows: rows of _SERP_STATE_SQL."""
    conn = MagicMock()
    cached = cached or {}

    async def _fetch(sql, *args):
        if "mode() WITHIN GROUP" in sql:
            return [{"canonical_place": p, "country": c} for p, c in (countries or {}).items()]
        if "serp_fresh" in sql:
            return serp_rows or []
        if "place_keyword" in sql:
            return maps or []
        if "FROM acp_contract.search_demand" in sql and "retrieved_on >" in sql:
            market, wanted = args[0], set(args[1])
            return [{"keyword": k, "search_volume": v} for k, v in cached.get(market, {}).items() if k in wanted]
        return []

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


def _logs(conn, tier=None) -> list[tuple]:
    rows = [r for c in conn.executemany.await_args_list if "segment_research_log" in c.args[0] for r in c.args[1]]
    return [r for r in rows if tier is None or r[2] == tier]


def _serp_body(paa=("Is it open all year?",), related=("zarvok lake map",), domains=("example.test",)):
    items = [{"type": "people_also_ask", "items": [{"title": q} for q in paa]},
             {"type": "related_searches", "items": list(related)}]
    items += [{"type": "organic", "domain": d} for d in domains]
    return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": items}]}]}


def _client(volumes=None, serp=None):
    from services.seo_intelligence.dataforseo_client import DataForSEOClient

    client = MagicMock()
    client.fetch_volumes_bulk = AsyncMock(side_effect=volumes or (lambda kws, *a, **k: {kw: 10 for kw in kws}))
    client._serp_advanced = AsyncMock(return_value=serp or _serp_body())
    real = DataForSEOClient(login="x", password="y")
    client._parse_paa, client._parse_related = real._parse_paa, real._parse_related
    client._parse_organic_domains = real._parse_organic_domains
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[])
    return client


PROPOSALS = ('{"places": ['
             '{"place": "Zarvok Lake", "keywords": ["zarvok lake", "zarvok lake kayak"]},'
             '{"place": "Melnith Gorge", "keywords": ["melnith gorge", "melnith gorge hike"]},'
             '{"place": "Quorra Valley", "keywords": ["quorra valley"]}]}')


# ── pure helpers ───────────────────────────────────────────────────────────────────────────────

def test_estimate_has_a_real_and_a_capped_figure_and_one_serp_per_place():
    est = rb.estimate_cost(2222, 3)
    assert est["volume_tasks_cap"] == 3 * 9           # ceil(2222*4/1000) = 9 per market
    assert est["volume_tasks"] == 3 * 6               # ceil(2222*2.565/1000) = 6 per market
    assert est["serp_calls_cap"] == 2222              # one per place, not one per market
    assert est["serp_calls"] == 1718                  # ceil(2222*0.773)
    assert est["idea_tasks"] == 0                     # suggestions are off by default
    assert est["llm_calls"] == 149                    # ceil(2222/15)
    assert est["dfs_usd"] < est["dfs_usd_cap"]
    assert rb.estimate_cost(2222, 3, use_suggestions=True)["idea_tasks"] == 112


def test_estimate_uses_the_stored_map_size_and_skips_the_llm_for_mapped_places():
    mapped = rb._Place("Zarvok Lake", [], ["US"], keywords=["zarvok lake"], has_map=True, serp=False)
    est = rb.estimate_plan([mapped])
    assert est["llm_calls"] == 0 and est["volume_tasks"] == 1 and est["serp_calls_cap"] == 0


def test_estimate_zero_when_nothing_selected():
    assert rb.estimate_cost(0, 3)["dfs_usd_cap"] == 0.0


def test_usd_cap_selects_the_longest_affordable_prefix():
    places = [rb._Place(f"p{i}", [], ["US"], serp=True) for i in range(3000)]
    picked = rb.select_within_usd(places, 1.0)
    assert 0 < len(picked) < 3000
    assert rb.estimate_plan(picked)["dfs_usd_cap"] <= 1.0
    assert rb.estimate_plan(places[:len(picked) + 1])["dfs_usd_cap"] > 1.0
    assert rb.select_within_usd(places, None) == places
    assert rb.select_within_usd(places, 0.0001) == []


@pytest.mark.parametrize("raw,clean", [
    ("Hội An Old Town!", "hoi an old town"),
    ("Đà Lạt", "da lat"),
    ("  Zarvok   LAKE ", "zarvok lake"),
    ("12345", None),
    ("", None),
    (" ".join(f"w{i}" for i in range(11)), None),       # Google Ads refuses > 10 words
    ("x" * 81, None),                                   # and > 80 characters
])
def test_clean_keyword(raw, clean):
    assert rb.clean_keyword(raw) == clean


def test_parse_proposals_handles_fences_and_bad_items():
    raw = '```json\n{"places": [{"place": "Zarvok", "keywords": ["Zarvok Dzong!", ""]}, {"x": 1}]}\n```'
    assert rb._parse_proposals(raw) == {"zarvok": ["zarvok dzong"]}
    assert rb._parse_proposals("not json") == {}


def test_keywords_from_map_needs_the_current_prompt_and_drops_enforced_rejects():
    rows = [{"keyword": "zarvok lake", "jev_zone": "not_asked", "jev_mode": None, "prompt_version": rb.PROMPT_VERSION},
            {"keyword": "lakes", "jev_zone": "reject", "jev_mode": "enforce", "prompt_version": rb.PROMPT_VERSION},
            {"keyword": "zarvok pier", "jev_zone": "reject", "jev_mode": "shadow", "prompt_version": rb.PROMPT_VERSION}]
    assert rb.keywords_from_map(rows) == ["zarvok lake", "zarvok pier"]
    assert rb.keywords_from_map([{**rows[0], "prompt_version": "a3_propose@old"}]) is None
    assert rb.keywords_from_map([]) is None


# ── end to end over mocks ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_maps_buys_one_volume_task_one_serp_per_place_and_logs_each_tier():
    pool, conn = _pool(cached={"US": {"zarvok lake": 5000}})
    client = _client(volumes=lambda kws, *a, **k: {"zarvok lake kayak": 800, "melnith gorge": 2000,
                                                   "melnith gorge hike": 0, "quorra valley": None})
    places = [rb._Place("Zarvok Lake", ["explore"], ["US"], serp=True),
              rb._Place("Melnith Gorge", ["walk"], ["US"], serp=True),
              rb._Place("Quorra Valley", ["drive"], ["US"], serp=True)]

    with patch.object(rb, "LLMClient", return_value=_llm(PROPOSALS)), \
         patch.object(rb, "record_call_with_pool", new=AsyncMock()) as m_llm_log, \
         patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()) as m_cache_log:
        result = await rb.research_batch(places, US, pool, client, sr._RunGuard())

    # every LLM call logs its billed fields (AA-1002 S6)
    assert m_llm_log.await_args.kwargs["quality_signal"]["prompt_version"] == rb.PROMPT_VERSION
    # the map is stored with the plain name as 'not_asked' and the proposals with their Jev zone
    map_rows = [r for c in conn.executemany.await_args_list if "place_keyword" in c.args[0] for r in c.args[1]]
    assert ("Zarvok Lake", "zarvok lake", 0, "place_name", rb.PROMPT_VERSION, "not_asked", None, None) in map_rows
    assert any(r[1] == "zarvok lake kayak" and r[3] == "llm_propose" and r[5] == "grey" for r in map_rows)
    # one paid volume task carrying every uncached keyword (the cached one is not re-bought)
    client.fetch_volumes_bulk.assert_awaited_once()
    assert sorted(client.fetch_volumes_bulk.await_args.args[0]) == [
        "melnith gorge", "melnith gorge hike", "quorra valley", "zarvok lake kayak"]
    assert m_cache_log.await_args.kwargs["keyword_count"] == 1
    # one SERP per place with volume, US / English, for its best keyword
    assert sorted(c.args[0] for c in client._serp_advanced.await_args_list) == ["melnith gorge", "zarvok lake"]
    assert all(c.args[1:] == (2840, "en") for c in client._serp_advanced.await_args_list)
    serp_writes = [c.args for c in conn.execute.await_args_list if "related_searches" in c.args[0]]
    assert {a[1] for a in serp_writes} == {"zarvok lake", "melnith gorge"}
    assert all(a[2] == "US" for a in serp_writes)
    # a log row per place and tier; the zero-volume place gets its SERP tier too (US volume known, nothing to buy)
    assert sorted(r[0] for r in _logs(conn, "volume")) == ["Melnith Gorge", "Quorra Valley", "Zarvok Lake"]
    assert sorted(r[0] for r in _logs(conn, "serp")) == ["Melnith Gorge", "Quorra Valley", "Zarvok Lake"]
    client.fetch_keyword_ideas_multi.assert_not_awaited()          # suggestions off by default
    assert result["places_complete"] == 3 and result["serp_calls"] == 2 and result["serp_no_volume"] == 1
    assert result["places_changed"] == ["Melnith Gorge", "Quorra Valley", "Zarvok Lake"]


@pytest.mark.asyncio
async def test_a_stored_map_is_reused_without_any_llm_call():
    pool, conn = _pool()
    client = _client()
    places = [rb._Place("Zarvok Lake", [], ["US"], keywords=["zarvok lake"], has_map=True)]
    with patch.object(rb, "LLMClient") as m_llm, patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()):
        result = await rb.research_batch(places, US, pool, client, sr._RunGuard())
    m_llm.return_value.generate.assert_not_called()
    assert result["llm_calls"] == 0 and result["maps_reused"] == 1
    assert _logs(conn, "volume") == [("Zarvok Lake", "US", "volume")]


@pytest.mark.asyncio
async def test_serp_skipped_when_the_row_already_holds_fresh_paa_and_related():
    pool, conn = _pool(serp_rows=[{"keyword": "zarvok lake", "market": "US", "search_volume": 900,
                                   "serp_fresh": True, "volume_fresh": True}])
    client = _client()
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], has_map=True, serp=True)]
    stats = await rb._buy_serps(places, client, pool, sr._RunGuard())
    client._serp_advanced.assert_not_awaited()
    assert stats["serp_fresh_skips"] == 1 and _logs(conn, "serp") == [("Zarvok Lake", "US", "serp")]


@pytest.mark.asyncio
async def test_serp_is_bought_again_when_related_searches_were_never_stored():
    # serp_fresh is False when related_searches IS NULL (the places bought before CIS 393)
    pool, conn = _pool(serp_rows=[{"keyword": "zarvok lake", "market": "US", "search_volume": 900,
                                   "serp_fresh": False, "volume_fresh": True}])
    client = _client()
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], has_map=True, serp=True)]
    stats = await rb._buy_serps(places, client, pool, sr._RunGuard())
    assert stats["serp_calls"] == 1


@pytest.mark.asyncio
async def test_no_volume_place_waits_while_its_serp_market_volume_is_unknown():
    pool, conn = _pool(serp_rows=[{"keyword": "quorra valley", "market": "UK", "search_volume": 0,
                                   "serp_fresh": False, "volume_fresh": True}])
    places = [rb._Place("Quorra Valley", [], [], keywords=["quorra valley"], has_map=True, serp=True)]
    stats = await rb._buy_serps(places, _client(), pool, sr._RunGuard())
    assert stats["serp_waiting_volume"] == 1 and _logs(conn) == []


@pytest.mark.asyncio
async def test_volume_fresh_place_with_a_new_keyword_never_logs_its_serp_as_nothing_to_buy():
    # The place's US volume tier is fresh (researched before CIS 393), but phase A proposed a keyword the store has no
    # row for ("quorra valley lookout"), and the stored plain-name row is a zero. Until that keyword's US volume is
    # known, "nobody searches it" is not proven: no SERP log row, the place stays stale.
    pool, conn = _pool(serp_rows=[{"keyword": "quorra valley", "market": "US", "search_volume": None,
                                   "serp_fresh": False, "volume_fresh": True}])
    places = [rb._Place("Quorra Valley", [], [], keywords=["quorra valley", "quorra valley lookout"], serp=True)]
    stats = await rb._buy_serps(places, _client(), pool, sr._RunGuard())
    assert stats == {"serp_calls": 0, "serp_fresh_skips": 0, "serp_no_volume": 0, "serp_waiting_volume": 1}
    assert _logs(conn) == [] and (rb.SERP_MARKET, "serp") not in places[0].logged


@pytest.mark.asyncio
async def test_serp_only_row_does_not_count_as_a_known_volume():
    # A SERP-only row (volume never measured, retrieved_on at the epoch) is not a known zero.
    pool, conn = _pool(serp_rows=[{"keyword": "quorra valley", "market": "US", "search_volume": None,
                                   "serp_fresh": True, "volume_fresh": False}])
    places = [rb._Place("Quorra Valley", [], [], keywords=["quorra valley"], has_map=True, serp=True)]
    stats = await rb._buy_serps(places, _client(), pool, sr._RunGuard())
    assert stats["serp_waiting_volume"] == 1 and _logs(conn) == []


@pytest.mark.asyncio
async def test_every_keyword_known_at_zero_logs_nothing_to_buy():
    # one zero from the fresh store, one answered by this run's US volume task
    pool, conn = _pool(serp_rows=[{"keyword": "quorra valley", "market": "US", "search_volume": 0,
                                   "serp_fresh": False, "volume_fresh": True}])
    places = [rb._Place("Quorra Valley", [], [], keywords=["quorra valley", "quorra valley lookout"], serp=True,
                        volumes={"quorra valley lookout": {"US": None}})]
    client = _client()
    stats = await rb._buy_serps(places, client, pool, sr._RunGuard())
    client._serp_advanced.assert_not_awaited()
    assert stats["serp_no_volume"] == 1 and _logs(conn, "serp") == [("Quorra Valley", "US", "serp")]


@pytest.mark.asyncio
async def test_failed_serp_writes_nothing_and_no_log_row():
    pool, conn = _pool()
    client = _client()
    client._serp_advanced = AsyncMock(side_effect=DFSCallError("timeout"))
    place = rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], has_map=True, serp=True,
                      volumes={"zarvok lake": {"US": 50}})
    guard = sr._RunGuard()
    await rb._buy_serps([place], client, pool, guard)
    assert place.failed and not guard.aborted
    assert not [c for c in conn.execute.await_args_list if "related_searches" in c.args[0]]
    assert _logs(conn) == []


@pytest.mark.asyncio
async def test_fatal_volume_failure_aborts_and_marks_nothing_researched():
    pool, conn = _pool()
    client = _client()
    client.fetch_volumes_bulk = AsyncMock(side_effect=DFSCallError("Payment Required", True, 40200))
    guard = sr._RunGuard()
    places = [rb._Place("Zarvok Lake", ["explore"], ["US"], serp=True)]

    with patch.object(rb, "LLMClient", return_value=_llm(PROPOSALS)), \
         patch.object(rb, "record_call_with_pool", new=AsyncMock()), \
         patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()):
        result = await rb.research_batch(places, US, pool, client, guard)

    assert guard.aborted
    assert result["places_researched"] == 0 and result["places_complete"] == 0
    assert _logs(conn) == []
    assert not [c for c in conn.executemany.await_args_list if "search_demand" in c.args[0]]
    client._serp_advanced.assert_not_awaited()
    client.fetch_volumes_bulk.assert_awaited_once()          # a fatal error is never split


@pytest.mark.asyncio
async def test_a_failed_task_is_split_in_two_before_its_keywords_fail():
    pool, conn = _pool()
    calls = []

    async def vols(kws, *a, **k):
        calls.append(len(kws))
        if len(calls) == 1:
            raise DFSCallError("DataForSEO status 40501: Invalid Field")
        return {kw: 5 for kw in kws}

    client = _client()
    client.fetch_volumes_bulk = AsyncMock(side_effect=vols)
    places = [rb._Place(f"p{i}", [], ["US"], keywords=[f"kw{i:03d}"]) for i in range(300)]
    with patch.object(rb, "SPLIT_MIN", 100), patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()):
        stats = await rb._buy_volumes(places, US, client, pool, sr._RunGuard())
    assert calls == [300, 150, 150]
    assert stats["volume_splits"] == 1 and stats["volume_tasks"] == 2
    assert len(_logs(conn, "volume")) == 300 and not any(p.failed for p in places)


@pytest.mark.asyncio
async def test_small_failed_task_is_not_split_and_its_places_stay_stale():
    pool, conn = _pool()
    client = _client()
    client.fetch_volumes_bulk = AsyncMock(side_effect=DFSCallError("DataForSEO status 50000: Internal Error"))
    places = [rb._Place("Zarvok Lake", [], ["US"], keywords=["zarvok lake"])]
    with patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()):
        await rb._buy_volumes(places, US, client, pool, sr._RunGuard())
    client.fetch_volumes_bulk.assert_awaited_once()
    assert places[0].failed and _logs(conn) == []


@pytest.mark.asyncio
async def test_each_place_is_logged_as_soon_as_its_task_landed():
    pool, conn = _pool()
    seen_logs_at_call: list[int] = []

    async def vols(kws, *a, **k):
        seen_logs_at_call.append(len(_logs(conn)))
        return {kw: 1 for kw in kws}

    client = _client()
    client.fetch_volumes_bulk = AsyncMock(side_effect=vols)
    places = [rb._Place(f"p{i}", [], ["US"], keywords=[f"kw{i}"]) for i in range(4)]
    with patch.object(rb, "VOLUME_TASK_MAX", 2), patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()):
        await rb._buy_volumes(places, US, client, pool, sr._RunGuard())
    # the second task starts after the first task's 2 places were logged: a stopped job resumes from there
    assert seen_logs_at_call == [0, 2] and len(_logs(conn)) == 4


@pytest.mark.asyncio
async def test_volume_tasks_split_at_task_limit_per_market():
    pool, _ = _pool()
    client = _client()
    places = [rb._Place(f"p{i}", [], ["US", "UK"], keywords=[f"kw{i}"]) for i in range(5)]
    with patch.object(rb, "VOLUME_TASK_MAX", 2), patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()):
        stats = await rb._buy_volumes(places, US_UK, client, pool, sr._RunGuard())
    assert stats["volume_tasks"] == 6          # ceil(5/2)=3 per market x 2 markets
    assert all(p.volumes[p.keywords[0]] == {"US": 10, "UK": 10} for p in places)


def _queue_serps(answers):
    """A StandardQueue.serps stand-in: reports each index through on_result (as the real queue does when it collects
    a task), then returns them all. `answers` is a dict or a function of the asks."""
    async def serps(asks, on_result=None):
        got = answers(asks) if callable(answers) else answers
        for i in sorted(got):
            if on_result is not None:
                await on_result(i, got[i])
        return got
    return serps


@pytest.mark.asyncio
async def test_queue_mode_buys_through_the_standard_queue():
    pool, conn = _pool()
    client = _client()
    queue = MagicMock()
    queue.volumes = AsyncMock(side_effect=lambda kws, *a: {kw: 30 for kw in kws})
    queue.serps = AsyncMock(side_effect=_queue_serps(lambda asks: {i: _serp_body() for i in range(len(asks))}))
    places = [rb._Place("Zarvok Lake", [], ["US"], keywords=["zarvok lake"], has_map=True, serp=True)]
    with patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()):
        result = await rb.research_batch(places, US, pool, client, sr._RunGuard(), queue=queue)
    queue.volumes.assert_awaited_once()
    assert queue.serps.await_args.args[0] == [("zarvok lake", 2840, "en")]
    client.fetch_volumes_bulk.assert_not_awaited()
    client._serp_advanced.assert_not_awaited()
    assert result["places_complete"] == 1


@pytest.mark.asyncio
async def test_queue_task_error_fails_only_that_place():
    pool, conn = _pool()
    queue = MagicMock()
    queue.serps = AsyncMock(side_effect=_queue_serps({0: DFSCallError("not finished"), 1: _serp_body()}))
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], has_map=True, serp=True,
                        volumes={"zarvok lake": {"US": 9}}),
              rb._Place("Melnith Gorge", [], [], keywords=["melnith gorge"], has_map=True, serp=True,
                        volumes={"melnith gorge": {"US": 9}})]
    await rb._buy_serps(places, _client(), pool, sr._RunGuard(), queue)
    assert places[0].failed and not places[1].failed
    assert _logs(conn, "serp") == [("Melnith Gorge", "US", "serp")]


@pytest.mark.asyncio
async def test_queue_budget_stop_or_fatal_answer_aborts_the_run():
    from shared.cost_guard import BudgetExceeded
    pool, conn = _pool()
    queue = MagicMock()
    queue.serps = AsyncMock(side_effect=_queue_serps(
        {0: _serp_body(), 1: BudgetExceeded("run budget", "dataforseo", "per_run")}))
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], has_map=True, serp=True,
                        volumes={"zarvok lake": {"US": 9}}),
              rb._Place("Melnith Gorge", [], [], keywords=["melnith gorge"], has_map=True, serp=True,
                        volumes={"melnith gorge": {"US": 9}})]
    guard = sr._RunGuard()
    await rb._buy_serps(places, _client(), pool, guard, queue)
    assert guard.aborted and places[1].failed and not places[0].failed
    assert _logs(conn, "serp") == [("Zarvok Lake", "US", "serp")]     # the paid one is still stored and logged


@pytest.mark.asyncio
async def test_queue_serps_are_stored_and_logged_as_each_task_is_collected():
    # The queue is still waiting for the second task when the first one is collected: the first SERP must already be
    # stored and its place logged (a worker restart during the wait keeps it).
    pool, conn = _pool()
    seen_at_second: list = []

    async def serps(asks, on_result=None):
        await on_result(0, _serp_body())
        seen_at_second.append(list(_logs(conn, "serp")))
        await on_result(1, _serp_body())
        return {}

    queue = MagicMock()
    queue.serps = AsyncMock(side_effect=serps)
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], has_map=True, serp=True,
                        volumes={"zarvok lake": {"US": 9}}),
              rb._Place("Melnith Gorge", [], [], keywords=["melnith gorge"], has_map=True, serp=True,
                        volumes={"melnith gorge": {"US": 9}})]
    stats = await rb._buy_serps(places, _client(), pool, sr._RunGuard(), queue)
    assert seen_at_second == [[("Zarvok Lake", "US", "serp")]]
    assert stats["serp_calls"] == 2 and not any(p.failed for p in places)


@pytest.mark.asyncio
async def test_queue_serp_never_reported_fails_its_place_and_a_fatal_wait_keeps_the_collected_ones():
    pool, conn = _pool()

    async def serps(asks, on_result=None):
        await on_result(0, _serp_body())
        raise DFSCallError("Payment Required", True, 40200)      # fatal while waiting for the second task

    queue = MagicMock()
    queue.serps = AsyncMock(side_effect=serps)
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], has_map=True, serp=True,
                        volumes={"zarvok lake": {"US": 9}}),
              rb._Place("Melnith Gorge", [], [], keywords=["melnith gorge"], has_map=True, serp=True,
                        volumes={"melnith gorge": {"US": 9}})]
    guard = sr._RunGuard()
    await rb._buy_serps(places, _client(), pool, guard, queue)
    assert guard.aborted and not places[0].failed and places[1].failed
    assert _logs(conn, "serp") == [("Zarvok Lake", "US", "serp")]


@pytest.mark.asyncio
async def test_a_queued_task_that_failed_after_it_was_accepted_is_never_split_nor_bought_again():
    from services.seo_intelligence.dfs_queue import QueuedTaskFailed

    pool, conn = _pool()
    queue = MagicMock()
    queue.volumes = AsyncMock(side_effect=QueuedTaskFailed.of("DataForSEO task t-1 not finished after 3600 s", "t-1"))
    places = [rb._Place(f"p{i}", [], ["US"], keywords=[f"kw{i:03d}"]) for i in range(300)]
    guard = sr._RunGuard()
    with patch.object(rb, "SPLIT_MIN", 100), patch.object(rb, "record_dfs_call_with_pool", new=AsyncMock()):
        stats = await rb._buy_volumes(places, US, _client(), pool, guard, queue)
    queue.volumes.assert_awaited_once()                       # one paid post, no halves posted again
    assert stats["volume_splits"] == 0 and stats["volume_tasks"] == 0
    assert all(p.failed for p in places) and _logs(conn) == [] and not guard.aborted


@pytest.mark.asyncio
async def test_unusable_llm_output_still_researches_the_plain_place_name():
    places = [rb._Place("Zarvok Dzong", ["visit"], ["US"])]
    pool, _ = _pool()
    with patch.object(rb, "LLMClient", return_value=_llm("garbage")), \
         patch.object(rb, "record_call_with_pool", new=AsyncMock()):
        await rb._propose(places, pool, sr._RunGuard(), None)
    assert places[0].keywords == ["zarvok dzong"]


@pytest.mark.asyncio
async def test_keyword_gate_drops_only_enforced_rejects_and_never_the_place_name():
    places = [rb._Place("Velmora Hot Spring", [], ["US"],
                        keywords=["velmora hot spring", "hot springs", "velmora onsen"])]
    asked = []

    async def fake(stage, subject, state, keys, **kw):
        asked.append(state["keyword"])
        return _decision(keys[0], "reject", "enforce" if state["keyword"] == "hot springs" else "shadow")

    with patch.object(rb, "decide", new=fake):
        stats = await rb._gate_keywords(places, MagicMock())
    assert sorted(asked) == ["hot springs", "velmora onsen"]          # plain name never asked
    assert places[0].keywords == ["velmora hot spring", "velmora onsen"]   # shadow reject kept
    assert places[0].verdicts["hot springs"].zone == "reject"           # kept for the map
    assert stats == {"jev_keywords_checked": 2, "jev_keywords_rejected": 1}


# ── suggestions (off by default, unchanged when on) ───────────────────────────────────────────

def test_idea_seed_adds_country_once_and_caps_words():
    assert rb._idea_seed(rb._Place("Zarvok", [], ["US"]), "velmora") == "zarvok velmora"
    assert rb._idea_seed(rb._Place("Zarvok, Velmora", [], ["US"]), "velmora") == "zarvok velmora"
    assert rb._idea_seed(rb._Place("Quorra Valley", [], ["US"]), None) == "quorra valley"
    long = rb._Place(" ".join(f"w{i}" for i in range(12)), [], ["US"])
    assert len(rb._idea_seed(long, "velmora").split()) == 10


@pytest.mark.asyncio
async def test_suggestion_seeds_use_place_country_and_drop_lodging_ideas():
    pool, conn = _pool(countries={"Zarvok": "Velmora", "Quorra Valley": "Velmora"})
    places = [rb._Place("Zarvok", [], ["US"], keywords=["zarvok to melnith"]),
              rb._Place("Quorra Valley", [], ["US"], keywords=["quorra valley"]),
              rb._Place("Somewhere", [], ["US"], keywords=["somewhere x"])]
    client = MagicMock()
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[
        {"keyword": "zarvok velmora", "search_volume": 40},
        {"keyword": "hotels in zarvok", "search_volume": 10},
        {"keyword": "quorra valley velmora", "search_volume": None}])
    with patch.object(rb, "IDEAS_MIN_SEEDS", 1):
        stats = await rb._buy_suggestions(places, US, client, pool, sr._RunGuard())
    assert client.fetch_keyword_ideas_multi.await_args.args[0] == [
        "zarvok velmora", "quorra valley velmora", "somewhere"]
    stored = [c.args[1] for c in conn.executemany.await_args_list if "search_demand" in c.args[0]][0]
    assert stored == [("zarvok velmora", "US", 40)]
    assert stats["idea_tasks"] == 1 and stats["ideas_stored"] == 1


@pytest.mark.asyncio
async def test_ideas_task_skipped_below_min_seeds():
    pool, conn = _pool(countries={"Zarvok": "Velmora"})
    client = MagicMock()
    client.fetch_keyword_ideas_multi = AsyncMock()
    places = [rb._Place("Zarvok", [], ["US"])]
    stats = await rb._buy_suggestions(places, US, client, pool, sr._RunGuard())
    client.fetch_keyword_ideas_multi.assert_not_awaited()
    assert stats == {"idea_tasks": 0, "ideas_stored": 0, "idea_tasks_skipped": 1}
    assert not places[0].failed          # skipping is not a purchase failure


@pytest.mark.asyncio
async def test_idea_gate_drops_enforced_rejects_before_storing():
    rows = [("hotel zarvok", "US", 10), ("zarvok velmora", "US", 40)]

    async def fake(stage, subject, state, keys, **kw):
        return _decision(keys[0], "reject" if "hotel" in state["keyword"] else "accept", "enforce")

    with patch.object(rb, "decide", new=fake):
        kept = await rb._gate_ideas(rows, ["zarvok velmora"], MagicMock())
    assert kept == [("zarvok velmora", "US", 40)]


# ── run_segment_research ───────────────────────────────────────────────────────────────────────

def _scope_rows(*places):
    return [{"segment_id": f"s-{p}", "canonical_place": p, "canonical_action": "explore", "activity_types": []}
            for p in places]


def _run_pool(scope, fresh=(), maps=(), segments=()):
    pool, conn = _pool()

    async def _fetch(sql, *args):
        if "GROUP BY s.segment_id" in sql:
            return scope
        if "segment_research_log" in sql:
            return [{"canonical_place": p, "market": m, "tier": t} for p, m, t in fresh]
        if "place_keyword" in sql:
            return list(maps)
        if "SELECT segment_id FROM acp_contract.atom_segment" in sql:
            return [{"segment_id": s} for s in segments]
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    return pool, conn


@pytest.mark.asyncio
async def test_dry_run_returns_the_estimate_and_never_pays():
    zarvok_map = [{"subject": "Zarvok Lake", "keyword": "zarvok lake", "rank": 0, "jev_zone": "not_asked",
                   "jev_mode": None, "prompt_version": rb.PROMPT_VERSION}]
    pool, conn = _run_pool(_scope_rows("Zarvok Lake", "Melnith Gorge"),
                           fresh=[("Zarvok Lake", "US", "volume")], maps=zarvok_map)
    with patch.object(sr, "DataForSEOClient") as m_client:
        result = await sr.run_segment_research({"countries": ["US"]}, pool, dry_run=True)
    m_client.assert_not_called()
    assert result["places_stale"] == 2                 # Zarvok Lake still needs its SERP
    assert result["places_stale_volume"] == 1 and result["places_stale_serp"] == 2
    assert result["estimate"]["volume_tasks_cap"] == 1 and result["estimate"]["serp_calls_cap"] == 2
    assert result["dfs_queue"] == "live" and result["tiers"] == ["volume", "serp"]


@pytest.mark.asyncio
async def test_tiers_and_queue_are_validated():
    pool, _ = _run_pool([])
    with pytest.raises(ValueError):
        await sr.run_segment_research({"countries": ["US"]}, pool, tiers=["paa"], dry_run=True)
    with pytest.raises(ValueError):
        await sr.run_segment_research({"countries": ["US"]}, pool, dfs_queue="priority", dry_run=True)


@pytest.mark.asyncio
async def test_a_run_that_stored_demand_enqueues_one_debounced_recompute():
    pool, conn = _run_pool(_scope_rows("Zarvok Lake"), segments=["s-1", "s-2"])
    order = []

    async def before(summary):
        order.append(("before_buy", summary["estimate"]["dfs_usd_cap"]))

    batch = AsyncMock(return_value={"places_changed": ["Zarvok Lake"], "places_researched": 1})
    enq = AsyncMock(return_value=("job-r", True))
    with patch.object(rb, "research_batch", batch), patch.object(sr, "DataForSEOClient"), \
         patch("services.jobs.recompute_job.enqueue_recompute", enq):
        result = await sr.run_segment_research({"countries": ["US"]}, pool, before_buy=before)
    assert order and order[0][0] == "before_buy"
    kwargs = enq.await_args.kwargs
    assert kwargs["scope"] == "platform" and kwargs["segment_ids"] == ["s-1", "s-2"]
    assert kwargs["debounce_s"] > 0 and kwargs["reason"] == "segment_research"
    assert result["recompute_job_id"] == "job-r"


@pytest.mark.asyncio
async def test_no_recompute_when_nothing_changed_or_switched_off():
    pool, _ = _run_pool(_scope_rows("Zarvok Lake"), segments=["s-1"])
    batch = AsyncMock(return_value={"places_changed": [], "places_researched": 0})
    enq = AsyncMock()
    with patch.object(rb, "research_batch", batch), patch.object(sr, "DataForSEOClient"), \
         patch("services.jobs.recompute_job.enqueue_recompute", enq):
        await sr.run_segment_research({"countries": ["US"]}, pool)
        batch.return_value = {"places_changed": ["Zarvok Lake"]}
        await sr.run_segment_research({"countries": ["US"]}, pool, recompute=False)
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_recompute_enqueue_never_fails_the_run():
    pool, _ = _run_pool(_scope_rows("Zarvok Lake"), segments=["s-1"])
    with patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock(side_effect=RuntimeError("db"))):
        out = await sr.enqueue_recompute_for_places(pool, ["Zarvok Lake"])
    assert out["recompute_job_id"] is None and "db" in out["recompute_error"]


def test_unknown_strategy_rejected():
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(sr.run_segment_research({"countries": ["US"]}, MagicMock(), strategy="nope"))


def test_loop_strategy_is_refused():
    # Its estimate counted volume tasks only while each place could buy a SERP per market and a suggestion.
    import asyncio

    with pytest.raises(ValueError, match="retired"):
        asyncio.run(sr.run_segment_research({"countries": ["US"]}, MagicMock(), strategy="loop", dry_run=True))


@pytest.mark.asyncio
async def test_plan_makes_a_place_without_a_map_stale_in_every_requested_market():
    current = [{"subject": "Zarvok Lake", "keyword": "zarvok lake", "rank": 0, "jev_zone": "not_asked",
                "jev_mode": None, "prompt_version": rb.PROMPT_VERSION}]
    # Both places: volume fresh in US and UK, SERP stale. Zarvok Lake has a map of the current prompt; Melnith
    # Gorge has none (researched before CIS 393): its new keywords were never bought anywhere.
    fresh = [(p, m, "volume") for p in ("Zarvok Lake", "Melnith Gorge") for m in ("US", "UK")]
    pool, _ = _run_pool(_scope_rows("Zarvok Lake", "Melnith Gorge"), fresh=fresh, maps=current)
    stale = await sr._plan(pool, {"Zarvok Lake": ["explore"], "Melnith Gorge": ["walk"]}, ["US", "UK"],
                           ("volume", "serp"))
    by_name = {p.name: p for p in stale}
    assert by_name["Zarvok Lake"].markets == [] and by_name["Zarvok Lake"].has_map
    assert by_name["Melnith Gorge"].markets == ["US", "UK"] and not by_name["Melnith Gorge"].has_map
    assert all(p.serp for p in stale)
    # a SERP-only run buys no volume, so it adds no market (the SERP pass makes the place wait instead)
    serp_only = await sr._plan(pool, {"Melnith Gorge": ["walk"]}, ["US", "UK"], ("serp",))
    assert serp_only[0].markets == []


@pytest.mark.asyncio
async def test_a_large_research_run_leaves_its_recompute_to_the_night_sweep(monkeypatch):
    from datetime import datetime, timezone

    from services.acp_contract import landing_sweep as ls

    monkeypatch.delenv(ls.SWITCH_ENV, raising=False)
    many = [f"s-{i:04d}" for i in range(sr.IMMEDIATE_RECOMPUTE_MAX_SEGMENTS + 1)]
    pool, _ = _run_pool([], segments=many)
    day = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)
    night = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)
    enq = AsyncMock(return_value=("job-r", True))
    with patch("services.jobs.recompute_job.enqueue_recompute", enq):
        out = await sr.enqueue_recompute_for_places(pool, ["Zarvok Lake"], now=day)
        assert out == {"recompute_job_id": None, "recompute_deferred": "landing_sweep",
                       "recompute_segments": len(many)}
        enq.assert_not_awaited()
        out = await sr.enqueue_recompute_for_places(pool, ["Zarvok Lake"], now=night)   # inside the window
        assert out["recompute_job_id"] == "job-r"
        monkeypatch.setenv(ls.SWITCH_ENV, "off")                       # no sweep to catch them: enqueue now
        out = await sr.enqueue_recompute_for_places(pool, ["Zarvok Lake"], now=day)
        assert out["recompute_job_id"] == "job-r"
    small, _ = _run_pool([], segments=["s-1"])
    monkeypatch.delenv(ls.SWITCH_ENV, raising=False)
    with patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock(return_value=("job-s", True))):
        assert (await sr.enqueue_recompute_for_places(small, ["Zarvok Lake"], now=day))["recompute_job_id"] == "job-s"
