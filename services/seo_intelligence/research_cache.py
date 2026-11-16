"""services/seo_intelligence/research_cache.py — AA-1120: the ONE research cache every DataForSEO purchase goes through.

What it caches. ``acp_contract.search_demand`` holds, per (keyword, market), the search volume (``retrieved_on``) and
the one SERP of the keyword (``people_also_ask`` / ``related_searches`` / ``serp_domains``, ``paa_retrieved_on``).
A (keyword, market) still fresh there is never bought again; every bought answer is written back here. The two
freshness windows are settings (``cis.seo.research_volume_fresh_days`` / ``cis.seo.research_serp_fresh_days``, CIS
434), read when a run starts (:func:`fresh_windows`), never a module constant.

Why a module (AA-1120 root cause, docs/implementation-notes/AA-1120.md). The research store was read and written by
each caller with its own SQL: segment research's batch phases did it around the live endpoint and the Standard queue
alike, but the queue itself (``dfs_queue.StandardQueue``) neither read nor wrote it, deduplicated nothing and kept no
record of what it had paid for, so a task paid at post and not collected (a job retry or a worker restart during the
wait) was posted and paid again by the next attempt, and two places sharing their best keyword bought the same SERP
twice. Store hits were logged under the live endpoint label and SERP skips not at all, so the ledger showed the queue
path with no cache hit. This module is the one place that:

  - reads fresh volumes / SERPs before a purchase (:meth:`ResearchCache.volumes`, :meth:`ResearchCache.serps`);
  - writes every bought answer back (:meth:`ResearchCache.put_volumes`, :meth:`ResearchCache.put_serp`);
  - logs the hits on the ledger under the endpoint label the run buys through (:meth:`ResearchCache.log_hits`);
  - finds queued tasks paid for within the recovery window (``cis.seo.dfs_queue_recover_days``) and never collected
    (:meth:`ResearchCache.pending_tasks`), so the queue collects them free instead of posting again;
  - deduplicates (endpoint, keyword, location) asks inside a run (:func:`dedupe`).

``DFS_CALL_SITES`` lists every function that calls a paid DataForSEO method, with the cache that covers it; the guard
``tests/unit/test_aa1120_dfs_cache_guard.py`` fails when a paid call appears anywhere else, when a listed research
call site stops using this module, or when a ``StandardQueue`` is built without ``cache=``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Sequence

import structlog

from services.seo_intelligence import research_settings as rs
from shared.dfs_client.call_log import record_dfs_call_with_pool

logger = structlog.get_logger()

# ── the guard's registry ─────────────────────────────────────────────────────────────────────────────────────────────
# The DataForSEOClient / StandardQueue methods that pay DataForSEO (each posts a billed task or a live call).
PAID_CLIENT_METHODS = frozenset({
    "fetch_keywords", "fetch_volumes_bulk", "_serp_advanced", "fetch_people_also_ask", "fetch_people_also_ask_deep",
    "fetch_related", "fetch_organic_domains", "fetch_keyword_ideas", "fetch_keyword_ideas_multi", "fetch_all",
})
# `_post` too: a caller that posts a queued task by hand (the AA-1055 trial) bypasses volumes / serps and their cache.
PAID_QUEUE_METHODS = frozenset({"volumes", "serps", "_post"})
# "<path>::<function>" -> the cache that covers the paid calls in it. "research_cache" = this module (the guard checks
# the function uses it); any other value names the cache of a path that does not buy research data, with the reason.
DFS_CALL_SITES: dict[str, str] = {
    "services/acp_contract/segment_research_batch.py::_buy_volumes._market._buy": "research_cache",
    "services/acp_contract/segment_research_batch.py::_buy_serps._one": "research_cache",
    "services/acp_contract/segment_research_batch.py::_buy_serps": "research_cache",
    "services/acp_contract/segment_research_batch.py::_buy_suggestions":
        "suggestions: off by default (AA-1055); the ideas are stored in search_demand and the places are logged",
    "services/acp_contract/segment_research.py::_VolumeBatcher._flush_after_linger":
        "retired loop strategy (run_segment_research refuses it, AA-1055); its caller _volumes_tool reads the store",
    "services/acp_contract/segment_research.py::_serp_tool": "retired loop strategy (refused, AA-1055)",
    "services/acp_contract/segment_research.py::_suggestions_tool": "retired loop strategy (refused, AA-1055)",
    "services/seo_intelligence/handler.py::process_seo":
        "S1 SEO fetch: Redis cache per (seed, market), 7 days (AA-625), + seo_context per tour",
    "services/seo_intelligence/s1_prefetch.py::prefetch":
        "S1 prefetch: seo_context reuse per tour (S1_SEO_REUSE_DAYS) and the research store read first (AA-653); "
        "one SERP per (seed, location) per run (AA-1120)",
    "services/acp_shared/question_topup.py::run_topup":
        "admin top-up: its own search_demand market, a rerun skips stored places, $3 cap in all (AA-779)",
    "scripts/aa1055/dfs_queue_trial.py::main": "adhoc trial of one task per kind (AA-1055), no research data",
}
# What a "research_cache" call site must use: its top-level function reads the store (a read method or the windows)
# and writes what it buys back (a write method), on a receiver named like "cache".
CACHE_READS = frozenset({"volumes", "serps", "windows"})
CACHE_WRITES = frozenset({"put_volumes", "put_serp", "put_serp_answer"})
CACHE_METHODS = CACHE_READS | CACHE_WRITES | {"log_hits", "pending_tasks"}


# ── settings ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Windows:
    """The windows in force: a volume / SERP younger than ``volume`` / ``serp`` is fresh; a queued task posted within
    ``recover`` and never collected is collected instead of posted (timedelta(0) = off)."""
    volume: timedelta
    serp: timedelta
    recover: timedelta


def _days(key: str) -> timedelta:
    from shared import settings
    return timedelta(days=int(settings.get(key)))


def volume_fresh_for() -> timedelta:
    """The volume freshness window in force (cis.seo.research_volume_fresh_days)."""
    return _days(rs.VOLUME_FRESH_DAYS)


def serp_fresh_for() -> timedelta:
    """The SERP freshness window in force (cis.seo.research_serp_fresh_days)."""
    return _days(rs.SERP_FRESH_DAYS)


async def fresh_windows() -> Windows:
    """The three windows in force, refreshed from the database when the reader's snapshot is old (settings.aget)."""
    from shared import settings
    vals = [int(await settings.aget(k)) for k in (rs.VOLUME_FRESH_DAYS, rs.SERP_FRESH_DAYS, rs.QUEUE_RECOVER_DAYS)]
    return Windows(timedelta(days=vals[0]), timedelta(days=vals[1]), timedelta(days=vals[2]))


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────
def dedupe(items: Iterable) -> tuple[list, dict]:
    """(the distinct items in first-seen order, item -> the positions it held). Pure."""
    unique: list = []
    where: dict = {}
    for i, item in enumerate(items):
        if item not in where:
            where[item] = []
            unique.append(item)
        where[item].append(i)
    return unique, where


@dataclass(frozen=True)
class CachedSerp:
    """A SERP read from the store instead of bought: what research keeps of one (parsed, not the raw answer)."""
    keyword: str
    market: str
    paa: list
    related: list
    domains: list


def parse_serp(client, data: dict) -> tuple[list[str], list[str], list[str]]:
    """(PAA, related searches, organic domains) of one raw advanced SERP; ([], [], []) when unreadable."""
    try:
        return client._parse_paa(data), client._parse_related(data), client._parse_organic_domains(data)
    except Exception:  # noqa: BLE001 — an unreadable answer stores nothing new
        return [], [], []


@dataclass
class PendingTask:
    """A queued task paid for at post (shared.dfs_call_log meta ``posted``) whose keywords are not fresh in the store."""
    task_id: str
    keywords: list[str]
    location_code: int | None


def pending_from_rows(rows: Sequence[dict], wanted: set[str], location_code: int | None,
                      exclude: set[str] = frozenset()) -> list[PendingTask]:
    """The posted tasks of ``rows`` (newest first: one task per keyword, the newest) that hold a ``wanted`` keyword at
    ``location_code``. ``rows[*]["posted"]`` is the post row's meta list ``[{"id", "keywords", "location_code"}]``.
    Pure."""
    out: list[PendingTask] = []
    claimed: set[str] = set()
    for row in rows:
        posted = row.get("posted")
        if isinstance(posted, str):
            try:
                posted = json.loads(posted)
            except ValueError:
                posted = None
        for t in posted or []:
            if not isinstance(t, dict) or not t.get("id") or str(t["id"]) in exclude:
                continue
            loc = t.get("location_code")
            if location_code is not None and loc is not None and int(loc) != int(location_code):
                continue
            kws = [k for k in (t.get("keywords") or []) if isinstance(k, str)]
            hit = [k for k in kws if k in wanted and k not in claimed]
            if hit:
                claimed.update(hit)
                out.append(PendingTask(str(t["id"]), kws, int(loc) if loc is not None else location_code))
    return out


# ── SQL ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
_VOLUMES_SQL = """
    SELECT keyword, search_volume FROM acp_contract.search_demand
    WHERE market = $1 AND keyword = ANY($2::text[]) AND retrieved_on > now() - $3::interval
"""
# A SERP is fresh when it was stored within the window with its related searches (NULL = never stored; [] is an answer).
_SERPS_SQL = """
    SELECT keyword, people_also_ask, related_searches, serp_domains FROM acp_contract.search_demand
    WHERE market = $1 AND keyword = ANY($2::text[]) AND paa_retrieved_on IS NOT NULL
      AND paa_retrieved_on > now() - $3::interval AND related_searches IS NOT NULL
"""
_UPSERT_VOLUME_SQL = """
    INSERT INTO acp_contract.search_demand (keyword, market, search_volume, retrieved_on)
    VALUES ($1, $2, $3, now())
    ON CONFLICT (keyword, market) DO UPDATE SET
        search_volume = excluded.search_volume, retrieved_on = excluded.retrieved_on
"""
# AA-1055 — one SERP on the keyword's row. A row created here has no measured volume yet: search_volume NULL and
# retrieved_on at the epoch, so the volume store sees it as stale and buys the volume on its next pass. An answer with
# no question keeps the row's earlier PAA (a SERP with no PAA box says nothing against it); related searches are always
# replaced (an empty list is a fresh answer, NULL means never stored).
_UPSERT_SERP_SQL = """
    INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask, retrieved_on,
                                            serp_domains, related_searches, paa_retrieved_on, serp_location)
    VALUES ($1, $2, NULL, $3::jsonb, 'epoch'::timestamptz, $4::jsonb, $5::jsonb, now(), $2)
    ON CONFLICT (keyword, market) DO UPDATE SET
        people_also_ask = CASE WHEN excluded.people_also_ask <> '[]'::jsonb THEN excluded.people_also_ask
                               ELSE acp_contract.search_demand.people_also_ask END,
        serp_domains = COALESCE(excluded.serp_domains, acp_contract.search_demand.serp_domains),
        related_searches = excluded.related_searches,
        paa_retrieved_on = excluded.paa_retrieved_on,
        serp_location = excluded.serp_location
"""
# The paid queue posts of an endpoint within the recovery window that recorded their task ids (AA-1120 meta "posted").
_PENDING_SQL = """
    SELECT meta->'posted' AS posted, created_at FROM shared.dfs_call_log
    WHERE endpoint = $1 AND fetched_live AND created_at > now() - $2::interval AND meta ? 'posted'
    ORDER BY created_at DESC
    LIMIT 500
"""


def _json_list(value) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, list) else []


class ResearchCache:
    """The research store of one run: reads before a purchase, write-back after it, hit rows on the ledger."""

    def __init__(self, pool, *, stage: str, windows: Windows, meta: dict | None = None) -> None:
        self.pool = pool
        self.stage = stage
        self.windows = windows
        self.meta = dict(meta or {})

    @classmethod
    async def load(cls, pool, *, stage: str, meta: dict | None = None) -> "ResearchCache":
        """A cache with the windows in force (settings)."""
        return cls(pool, stage=stage, windows=await fresh_windows(), meta=meta)

    async def volumes(self, keywords: Sequence[str], market: str) -> dict[str, int | None]:
        """keyword -> stored volume for every keyword of ``keywords`` fresh in ``market`` (None = measured, no data)."""
        kws = sorted(set(keywords))
        if not kws:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_VOLUMES_SQL, market, kws, self.windows.volume)
        return {r["keyword"]: r["search_volume"] for r in rows}

    async def put_volumes(self, market: str, answers: dict[str, int | None]) -> None:
        """Write bought volumes back (an answered keyword with no DataForSEO row is stored as NULL = measured)."""
        if not answers:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany(_UPSERT_VOLUME_SQL, [(kw, market, v) for kw, v in answers.items()])

    async def serps(self, keywords: Sequence[str], market: str) -> dict[str, CachedSerp]:
        """keyword -> its stored SERP for every keyword of ``keywords`` whose SERP is fresh in ``market``."""
        kws = sorted(set(keywords))
        if not kws:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_SERPS_SQL, market, kws, self.windows.serp)
        return {r["keyword"]: CachedSerp(r["keyword"], market, _json_list(r["people_also_ask"]),
                                         _json_list(r["related_searches"]), _json_list(r["serp_domains"]))
                for r in rows}

    async def put_serp(self, keyword: str, market: str, paa: list[str], related: list[str],
                       domains: list[str]) -> None:
        """Write one bought SERP back on the keyword's row and reset the Segment caches it feeds (AA-630 / AA-631)."""
        async with self.pool.acquire() as conn:
            await conn.execute(_UPSERT_SERP_SQL, keyword, market, json.dumps(paa[:10]),
                               json.dumps(domains) if domains else None, json.dumps(related[:10]))
            try:
                from services.acp_contract.atom_ranking import (
                    invalidate_contested_cache_for_keyword,
                    invalidate_questions_cache_for_keyword,
                )
                if paa:
                    await invalidate_questions_cache_for_keyword(conn, keyword)
                if domains:
                    await invalidate_contested_cache_for_keyword(conn, keyword)
            except Exception as exc:  # noqa: BLE001 — best effort, never breaks the purchase
                logger.warning("serp_cache_invalidate_failed", keyword=keyword, error=str(exc)[:200])

    async def put_serp_answer(self, client, keyword: str, market: str, data: dict) -> None:
        """Parse one raw advanced SERP and write it back (put_serp)."""
        paa, related, domains = parse_serp(client, data)
        await self.put_serp(keyword, market, paa, related, domains)

    async def log_hits(self, endpoint: str, *, count: int, location_code: int | None, meta: dict | None = None) -> None:
        """One 0 USD ``cache_hit`` row for ``count`` asks answered without a purchase, under the endpoint label the run
        buys through (so the queue path shows its hits). Nothing for count 0. Never raises."""
        if count <= 0:
            return
        await record_dfs_call_with_pool(
            self.pool, endpoint=endpoint, cache_hit=True, cost_usd=0.0, location_code=location_code,
            keyword_count=count, meta={**self.meta, "source": "research_cache", **(meta or {})}, stage=self.stage,
        )

    async def pending_tasks(self, endpoint: str, location_code: int | None, keywords: Sequence[str],
                            exclude: set[str] = frozenset()) -> list[PendingTask]:
        """Queued tasks of ``endpoint`` paid for within the recovery window that hold one of ``keywords`` (which the
        caller already found not fresh in the store). Empty when the window is 0. A failed read returns nothing (the
        caller then posts, as before AA-1120)."""
        if self.windows.recover <= timedelta(0) or not keywords:
            return []
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(_PENDING_SQL, endpoint, self.windows.recover)
        except Exception as exc:  # noqa: BLE001 — recovery is a saving, never a reason to stop a run
            logger.warning("dfs_queue_pending_read_failed", endpoint=endpoint, error=str(exc)[:200])
            return []
        return pending_from_rows([dict(r) for r in rows], set(keywords), location_code, set(exclude))


__all__ = ["ResearchCache", "Windows", "CachedSerp", "PendingTask", "fresh_windows", "volume_fresh_for",
           "serp_fresh_for", "dedupe", "parse_serp", "pending_from_rows", "DFS_CALL_SITES", "PAID_CLIENT_METHODS",
           "PAID_QUEUE_METHODS", "CACHE_METHODS", "CACHE_READS", "CACHE_WRITES"]
