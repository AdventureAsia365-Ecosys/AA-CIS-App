"""AA-1120 — the research cache around every DataForSEO purchase: the Standard queue reads the fresh store and the paid,
uncollected tasks before it posts, deduplicates its asks, writes every answer back and logs its hits under its own
endpoint label; phase C buys one SERP per distinct keyword; S1 buys one SERP per seed; the deduped pre-run estimate.
Mock-only (no DB, no DataForSEO); invented place names."""
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import segment_research as sr
from services.acp_contract import segment_research_batch as rb
from services.seo_intelligence import dfs_queue as q
from services.seo_intelligence import research_cache as rc
from services.seo_intelligence.dataforseo_client import DataForSEOClient

WINDOWS = rc.Windows(timedelta(days=365), timedelta(days=365), timedelta(days=7))


class FakeCache:
    """In-memory ResearchCache: {market: {kw: volume}}, {market: {kw: CachedSerp}}, pending tasks, hit rows."""

    def __init__(self, volumes=None, serps=None, pending=None, windows=WINDOWS):
        self.vol = {m: dict(v) for m, v in (volumes or {}).items()}
        self.serp = {m: dict(v) for m, v in (serps or {}).items()}
        self.pending = list(pending or [])
        self.windows = windows
        self.hits: list[tuple[str, int, dict]] = []
        self.serp_writes: list[tuple[str, str]] = []
        self.volume_writes: list[tuple[str, dict]] = []
        self.reads = 0

    async def volumes(self, keywords, market):
        self.reads += 1
        return {k: v for k, v in self.vol.get(market, {}).items() if k in set(keywords)}

    async def put_volumes(self, market, answers):
        self.volume_writes.append((market, dict(answers)))
        self.vol.setdefault(market, {}).update(answers)

    async def serps(self, keywords, market):
        return {k: v for k, v in self.serp.get(market, {}).items() if k in set(keywords)}

    async def put_serp_answer(self, client, keyword, market, data):
        self.serp_writes.append((keyword, market))
        paa, related, domains = rc.parse_serp(client, data)
        self.serp.setdefault(market, {})[keyword] = rc.CachedSerp(keyword, market, paa, related, domains)

    async def log_hits(self, endpoint, *, count, location_code, meta=None):
        if count > 0:
            self.hits.append((endpoint, count, dict(meta or {})))

    async def pending_tasks(self, endpoint, location_code, keywords, exclude=frozenset()):
        return [p for p in self.pending if p.task_id not in exclude and set(p.keywords) & set(keywords)]


class QueueClient:
    """Answers _request like DataForSEO; `old` = tasks an earlier run posted: task id -> its raw answer."""

    def __init__(self, old=None):
        self.old = dict(old or {})
        self.posted: list[dict] = []
        self.logged: list[dict] = []
        self.gets: list[str] = []
        real = DataForSEOClient(login="x", password="y")
        self._parse_paa, self._parse_related = real._parse_paa, real._parse_related
        self._parse_organic_domains = real._parse_organic_domains

    def _precheck(self, endpoint, **kw):
        return None

    def _log_live(self, endpoint, data, **kw):
        self.logged.append({"endpoint": endpoint, "meta": kw.get("meta")})

    async def _request(self, endpoint, url, *, payload=None, **kw):
        if url.endswith("task_post"):
            self.posted.extend(payload)
            return {"status_code": 20000, "tasks": [
                {"id": f"id-{t['tag']}", "status_code": 20100, "cost": 0.06, "data": {"tag": t["tag"]}}
                for t in payload]}, 5
        if url.endswith("tasks_ready"):
            return {"status_code": 20000, "tasks": [{"result": [{"id": f"id-{t['tag']}"} for t in self.posted]}]}, 5
        task_id = url.rsplit("/", 1)[1]
        self.gets.append(task_id)
        if task_id in self.old:
            return self.old[task_id], 5
        t = next(t for t in self.posted if f"id-{t['tag']}" == task_id)
        if "keywords" in t:
            result = [{"keyword": k, "search_volume": 10} for k in t["keywords"]]
        else:
            result = [{"items": _serp_items()}]
        return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": result}]}, 5


def _serp_items(q_text="Is the zarvok trail open?"):
    return [{"type": "people_also_ask", "items": [{"title": q_text}]},
            {"type": "related_searches", "items": ["zarvok lake map"]},
            {"type": "organic", "domain": "example.test"}]


def _volume_answer(pairs: dict) -> dict:
    return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [
        {"keyword": k, "search_volume": v} for k, v in pairs.items()]}]}


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_dedupe_keeps_first_seen_order_and_every_position():
    unique, where = rc.dedupe(["b", "a", "b", "c", "a"])
    assert unique == ["b", "a", "c"]
    assert where == {"b": [0, 2], "a": [1, 4], "c": [3]}


def test_pending_from_rows_takes_the_newest_task_per_keyword_at_the_location():
    rows = [{"posted": [{"id": "t-new", "keywords": ["zarvok lake"], "location_code": 2840}]},
            {"posted": '[{"id": "t-old", "keywords": ["zarvok lake", "melnith gorge"], "location_code": 2840}]'},
            {"posted": [{"id": "t-uk", "keywords": ["quorra valley"], "location_code": 2826}]},
            {"posted": [{"id": "t-mine", "keywords": ["quorra valley"], "location_code": 2840}]}]
    got = rc.pending_from_rows(rows, {"zarvok lake", "melnith gorge", "quorra valley"}, 2840, exclude={"t-mine"})
    assert [(p.task_id, sorted(p.keywords)) for p in got] == [
        ("t-new", ["zarvok lake"]), ("t-old", ["melnith gorge", "zarvok lake"])]


def test_posted_meta_records_each_task_id_with_its_keywords():
    payload = [{"keywords": ["a", "b"], "location_code": 2840, "tag": "x"},
               {"keyword": "c", "location_code": 2826, "tag": "y"}]
    posted = [q.PostedTask("id-x", "x", 0.06), q.PostedTask("id-y", "y", 0.0006)]
    assert q.posted_meta(payload, posted) == [{"id": "id-x", "keywords": ["a", "b"], "location_code": 2840},
                                              {"id": "id-y", "keywords": ["c"], "location_code": 2826}]


def test_windows_come_from_the_settings_registry():
    from shared import settings
    assert rc.volume_fresh_for() == timedelta(days=int(settings.get("cis.seo.research_volume_fresh_days")))
    assert rc.serp_fresh_for() == timedelta(days=int(settings.get("cis.seo.research_serp_fresh_days")))


# ── the Standard queue reads the cache before it posts ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_volumes_post_only_what_the_store_lacks_and_write_it_back():
    cache = FakeCache(volumes={"US": {"zarvok lake": 5000}})
    client = QueueClient()
    queue = q.StandardQueue(client, cache=cache, sleep=AsyncMock())
    out = await queue.volumes(["zarvok lake", "melnith gorge", "melnith gorge"], 2840, "en")
    assert out == {"zarvok lake": 5000, "melnith gorge": 10}
    assert [p["keywords"] for p in client.posted] == [["melnith gorge"]]          # deduped, the hit not posted
    assert cache.volume_writes == [("US", {"melnith gorge": 10})]
    assert cache.hits == [(q.VOLUME_ENDPOINT, 1, {"queue": "standard"})]          # the queue's own label
    posted = client.logged[0]["meta"]["posted"]
    assert posted == [{"id": f"id-{client.posted[0]['tag']}", "keywords": ["melnith gorge"], "location_code": 2840}]


@pytest.mark.asyncio
async def test_queue_volumes_all_cached_post_nothing():
    cache = FakeCache(volumes={"US": {"zarvok lake": 5000, "melnith gorge": None}})
    client = QueueClient()
    out = await q.StandardQueue(client, cache=cache, sleep=AsyncMock()).volumes(
        ["zarvok lake", "melnith gorge"], 2840, "en")
    assert out == {"zarvok lake": 5000, "melnith gorge": None} and client.posted == []


@pytest.mark.asyncio
async def test_queue_collects_a_paid_uncollected_task_instead_of_posting_again():
    pending = [rc.PendingTask("t-paid", ["zarvok lake", "melnith gorge"], 2840)]
    cache = FakeCache(pending=pending)
    client = QueueClient(old={"t-paid": _volume_answer({"zarvok lake": 70, "melnith gorge": 0})})
    out = await q.StandardQueue(client, cache=cache, sleep=AsyncMock()).volumes(
        ["zarvok lake", "melnith gorge"], 2840, "en")
    assert out == {"zarvok lake": 70, "melnith gorge": 0}
    assert client.posted == [] and client.gets == ["t-paid"]                        # free task_get, no post
    assert cache.volume_writes == [("US", {"zarvok lake": 70, "melnith gorge": 0})]
    assert any(m.get("source") == "recovered_task" and n == 2 for _e, n, m in cache.hits)


@pytest.mark.asyncio
async def test_an_expired_paid_task_falls_back_to_a_post():
    cache = FakeCache(pending=[rc.PendingTask("t-gone", ["zarvok lake"], 2840)])
    client = QueueClient(old={"t-gone": {"status_code": 20000, "tasks": [{"status_code": 40400, "result": None}]}})
    out = await q.StandardQueue(client, cache=cache, sleep=AsyncMock()).volumes(["zarvok lake"], 2840, "en")
    assert out == {"zarvok lake": 10} and [p["keywords"] for p in client.posted] == [["zarvok lake"]]


@pytest.mark.asyncio
async def test_queue_serps_one_task_per_distinct_ask_cache_hits_and_write_back():
    stored = rc.CachedSerp("zarvok lake", "US", ["Q?"], ["r"], ["example.test"])
    cache = FakeCache(serps={"US": {"zarvok lake": stored}})
    client = QueueClient()
    seen = {}

    async def on_result(i, v):
        seen[i] = v

    asks = [("zarvok lake", 2840, "en"), ("melnith gorge", 2840, "en"), ("melnith gorge", 2840, "en")]
    out = await q.StandardQueue(client, cache=cache, sleep=AsyncMock()).serps(asks, on_result=on_result)
    assert out[0] is stored and seen[0] is stored                                  # a store hit: nothing bought
    assert [p["keyword"] for p in client.posted] == ["melnith gorge"]              # the duplicate shares one task
    assert out[1] is out[2] and isinstance(out[1], dict)
    assert cache.serp_writes == [("melnith gorge", "US")]                          # written back once
    assert (q.SERP_ENDPOINT, 1, {"queue": "standard"}) in cache.hits


@pytest.mark.asyncio
async def test_queue_serps_collect_a_paid_uncollected_serp():
    cache = FakeCache(pending=[rc.PendingTask("t-serp", ["melnith gorge"], 2840)])
    body = {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": _serp_items()}]}]}
    client = QueueClient(old={"t-serp": body})
    out = await q.StandardQueue(client, cache=cache, sleep=AsyncMock()).serps([("melnith gorge", 2840, "en")])
    assert client.posted == [] and out[0] is body and cache.serp_writes == [("melnith gorge", "US")]


# ── phase C: one SERP per distinct keyword ───────────────────────────────────────────────────────────────────────────

def _pool(serp_rows):
    conn = MagicMock()

    async def _fetch(sql, *args):
        return serp_rows if "serp_fresh" in sql else []
    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _serp_client():
    client = MagicMock()
    client.stage = rb.RESEARCH_STAGE
    client._serp_advanced = AsyncMock(return_value={"status_code": 20000, "tasks": [
        {"status_code": 20000, "result": [{"items": _serp_items()}]}]})
    real = DataForSEOClient(login="x", password="y")
    client._parse_paa, client._parse_related = real._parse_paa, real._parse_related
    client._parse_organic_domains = real._parse_organic_domains
    return client


def _state(kw, vol, serp_fresh=False):
    return {"keyword": kw, "market": "US", "search_volume": vol, "serp_fresh": serp_fresh, "volume_fresh": True}


@pytest.mark.asyncio
async def test_live_phase_c_buys_one_serp_for_places_sharing_their_best_keyword():
    pool, conn = _pool([_state("zarvok lake", 900), _state("zarvok lake ferry", 10), _state("melnith gorge", 0)])
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], serp=True),
              rb._Place("Zarvok Lake Pier", [], [], keywords=["zarvok lake ferry", "zarvok lake"], serp=True)]
    cache = FakeCache()
    stats = await rb._buy_serps(places, _serp_client(), pool, sr._RunGuard(), cache=cache)
    assert stats["serp_calls"] == 1 and stats["serp_shared_keyword"] == 1
    assert cache.serp_writes == [("zarvok lake", "US")]
    assert all((rb.SERP_MARKET, "serp") in p.logged for p in places)


@pytest.mark.asyncio
async def test_phase_c_logs_its_fresh_skips_under_the_queue_label():
    pool, _conn = _pool([_state("zarvok lake", 900, serp_fresh=True)])
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], serp=True)]
    cache = FakeCache()
    queue = MagicMock()
    queue.serps = AsyncMock(side_effect=AssertionError("nothing to buy"))
    stats = await rb._buy_serps(places, _serp_client(), pool, sr._RunGuard(), queue, cache)
    assert stats["serp_fresh_skips"] == 1
    assert cache.hits == [(q.SERP_ENDPOINT, 1, {"job": "segment_research", "phase": "serp"})]


@pytest.mark.asyncio
async def test_queue_phase_c_marks_places_of_a_store_hit_without_storing():
    pool, conn = _pool([_state("zarvok lake", 900)])
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], serp=True)]
    cache = FakeCache()
    queue = MagicMock()
    queue.cache = cache

    async def serps(asks, on_result=None):
        await on_result(0, rc.CachedSerp("zarvok lake", "US", [], [], []))
        return {}
    queue.serps = serps
    stats = await rb._buy_serps(places, _serp_client(), pool, sr._RunGuard(), queue, cache)
    assert stats["serp_calls"] == 0 and stats["serp_fresh_skips"] == 1 and not places[0].failed
    assert (rb.SERP_MARKET, "serp") in places[0].logged and cache.serp_writes == []


@pytest.mark.asyncio
async def test_phase_b_hit_row_uses_the_queue_label_in_queue_mode():
    pool, _conn = _pool([])
    cache = FakeCache(volumes={"US": {"zarvok lake": 40}})
    queue = MagicMock()
    queue.volumes = AsyncMock(side_effect=AssertionError("all cached"))
    place = rb._Place("Zarvok Lake", [], ["US"], keywords=["zarvok lake"])
    stats = await rb._buy_volumes([place], [(2840, "United States", "en")], _serp_client(), pool, sr._RunGuard(),
                                  queue, cache)
    assert stats["keywords_cached"] == 1 and cache.hits[0][0] == q.VOLUME_ENDPOINT


# ── the deduped pre-run estimate ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deduped_estimate_counts_distinct_uncached_keywords_and_shared_serps():
    cache = FakeCache(volumes={"US": {"zarvok lake": 900, "zarvok lake ferry": 10, "melnith gorge": 50}},
                      serps={"US": {"melnith gorge": rc.CachedSerp("melnith gorge", "US", [], [], [])}})
    places = [rb._Place("Zarvok Lake", [], ["US"], keywords=["zarvok lake", "quorra pass"], has_map=True, serp=True),
              rb._Place("Zarvok Pier", [], ["US"], keywords=["zarvok lake ferry", "zarvok lake", "quorra pass"],
                        has_map=True, serp=True),
              rb._Place("Melnith Gorge", [], [], keywords=["melnith gorge"], has_map=True, serp=True),
              rb._Place("Unmapped Place", [], ["US"], serp=True)]
    est = await rb.estimate_deduped(places, cache, "standard")
    assert est["keywords_asked"] == 2 + 3 + 3                  # 2.565 rounded up for the unmapped place
    assert est["keywords_to_buy"] == 1 + 3                     # "quorra pass" once + the unmapped place's keywords
    assert est["volume_tasks"] == 1
    # Melnith Gorge: its SERP is fresh; the two Zarvok places wait for "quorra pass"; the unmapped one is unknown
    assert est["serp_cached"] == 1 and est["serp_unknown_volume"] == 3
    assert est["serp_calls"] == 3                              # ceil(3 x 0.773)
    assert est["serp_requests"] == 1 and est["calls"] == 2     # queue: one volume task + one SERP post
    assert est["dfs_usd"] == round(0.06 + 3 * 0.0006, 4)


@pytest.mark.asyncio
async def test_deduped_estimate_one_serp_for_a_shared_best_keyword():
    cache = FakeCache(volumes={"US": {"zarvok lake": 900, "zarvok lake ferry": 10}})
    places = [rb._Place("Zarvok Lake", [], [], keywords=["zarvok lake"], has_map=True, serp=True),
              rb._Place("Zarvok Pier", [], [], keywords=["zarvok lake ferry", "zarvok lake"], has_map=True, serp=True)]
    est = await rb.estimate_deduped(places, cache, "live")
    assert est["serp_calls"] == 1 and est["serp_shared_keyword"] == 1 and est["calls"] == 1


# ── S1: one SERP per seed ────────────────────────────────────────────────────────────────────────────────────────────

class _S1Conn:
    def __init__(self, reusable=None):
        self.reusable = dict(reusable or {})
        self.inserted = []

    async def fetch(self, sql, *args):
        if "DISTINCT ON (cache_key)" in sql:
            return [{"cache_key": k, "people_also_ask": v[0], "related_keywords": v[1]}
                    for k, v in self.reusable.items() if k in args[0]]
        return []

    async def fetchrow(self, sql, *args):
        self.inserted.append(args)
        return {"id": "x"}


def _s1_row(tid, title, activity):
    return {"tour_id": tid, "src_name": title, "country": "Zarvokia", "activities": [activity]}


@pytest.mark.asyncio
async def test_s1_prefetch_buys_one_serp_per_seed_and_reuses_another_tours_row():
    from services.seo_intelligence import s1_prefetch as P

    rows = [_s1_row("00000000-0000-0000-0000-0000000000a1", "Zarvok Lake Hike", "trekking"),
            _s1_row("00000000-0000-0000-0000-0000000000a2", "Zarvok Ridge Hike", "trekking"),
            _s1_row("00000000-0000-0000-0000-0000000000a3", "Melnith Gorge Ride", "cycling")]
    specs = [P.tour_spec(r) for r in rows]
    reused_key = P.cache_key(specs[2]["seed"], 2840)
    conn = _S1Conn(reusable={reused_key: (["Stored question?"], ["stored related"])})
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[])
    client._serp_advanced = AsyncMock(return_value={"tasks": [{"result": [{"items": _serp_items()}]}]})
    with patch.object(P, "fresh_tour_ids", AsyncMock(return_value=set())), \
         patch.object(P, "foreign_places", AsyncMock(return_value=frozenset())):
        summary = await P.prefetch(conn, rows, tenant_id="t", location_code=2840, language_code="en",
                                   client=client, jev=False)
    assert specs[0]["seed"] == specs[1]["seed"] != specs[2]["seed"]
    assert client._serp_advanced.await_count == 1                                  # one SERP for the shared seed
    assert summary["serp_calls"] == 1 and summary["serp_reused"] == 2             # the twin tour + the stored row
    assert len(conn.inserted) == 3
