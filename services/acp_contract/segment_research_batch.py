"""services/acp_contract/segment_research_batch.py — AA-648, batch search-demand research.

Why: DataForSEO prices **per task**, not per keyword. The per-place ReAct loop
(`segment_research._research_place`) asks for a few keywords at a time; on 25/09/2026 its batcher
averaged 4.5 keywords per paid volume task ($39 for 678 tasks) and bought one
`keywords_for_keywords` task per place. This module runs the same research as phases over the
whole selection, so each paid task carries as much as it can:

  A. propose     — one Haiku call per PROPOSE_CHUNK places returns up to KEYWORDS_PER_PLACE search
                   keywords for each (the plain place name first). No DFS.
  B. volumes     — per market: read the fresh cache in one query, buy the rest in tasks of up to
                   VOLUME_TASK_MAX keywords, store real answers only.
  C. serp        — for each place, its best keyword with measured volume, only in the markets where
                   it has volume (PAA + organic domains, one call serves both — AA-631).
  D. suggestions — places with no volume anywhere: their seeds go IDEAS_SEEDS_PER_TASK per
                   keywords_for_keywords task (primary market); ideas with volume are cached.
  E. log         — `segment_research_log` is written only for places whose purchases all succeeded.

Every paid call goes through the cost guard (AA-649) and the run-level circuit breaker (AA-647): a
budget stop or a fatal DFS error ends the run and leaves unfinished places stale for the next one.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field

import structlog
from json_repair import repair_json

from services.acp_contract.segment_research import (
    FRESH_FOR,
    PlacePurchaseFailed,
    _RunGuard,
    _serp_tool,
)
from services.seo_intelligence.dataforseo_client import DataForSEOClient
from services.seo_intelligence.seed_builder import LOCATION_CODE_TO_MARKET
from shared.cost_guard import DFS_CALL_ESTIMATE_USD, BudgetExceeded, RunBudget
from shared.dfs_client.call_log import record_dfs_call_with_pool
from shared.llm_client.call_log import record_call_with_pool
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

PROPOSE_CHUNK = 15
KEYWORDS_PER_PLACE = 4
VOLUME_TASK_MAX = 1000
IDEAS_SEEDS_PER_TASK = 20
SERP_KEYWORDS_PER_PLACE = 1
SERP_CONCURRENCY = 4
# One batched Haiku call (~15 places): ~2k input + ~1k output tokens ≈ $0.007 at Haiku 4.5 prices.
LLM_PROPOSE_ESTIMATE_USD = 0.02

_TIDY = re.compile(r"[^a-z0-9 ]+")

PROPOSE_SYSTEM = """\
You propose what travellers type into a search engine about places on travel itineraries.

For EACH place you are given, return up to 4 search keywords:
- The first keyword is the place itself, written the way people search it ("taktsang monastery",
  not "the sacred Tiger's Nest"). Add the country only if the name alone is ambiguous.
- The others qualify the place with what the itineraries do there ("taktsang monastery hike",
  "paro festival dates"). Real search phrasing, never brochure language.
- Lowercase, no punctuation, 1-6 words each.

Respond with ONLY a JSON object, no markdown fences:
{"places": [{"place": "<the place exactly as given>", "keywords": ["...", "..."]}]}
"""

_UPSERT_VOLUME_SQL = """
    INSERT INTO acp_contract.search_demand (keyword, market, search_volume, retrieved_on)
    VALUES ($1, $2, $3, now())
    ON CONFLICT (keyword, market) DO UPDATE SET
        search_volume = excluded.search_volume, retrieved_on = excluded.retrieved_on
"""
_LOG_RESEARCHED_SQL = """
    INSERT INTO acp_contract.segment_research_log (canonical_place, market, researched_at)
    VALUES ($1, $2, now())
    ON CONFLICT (canonical_place, market) DO UPDATE SET researched_at = now()
"""


@dataclass
class _Place:
    name: str
    actions: list[str]
    markets: list[str]                 # stale markets to research for this place
    keywords: list[str] = field(default_factory=list)
    volumes: dict[str, dict[str, int | None]] = field(default_factory=dict)  # kw -> market -> vol
    failed: bool = False

    def has_volume(self) -> bool:
        return any(v for per in self.volumes.values() for v in per.values())


def _tidy(text: str) -> str:
    return " ".join(_TIDY.sub(" ", (text or "").lower()).split())


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def estimate_cost(n_places: int, n_markets: int, use_suggestions: bool = True) -> dict:
    """Upper-bound spend for one batch run over `n_places` stale places (AA-649 pre-run estimate).
    Upper bound: assumes every place needs every keyword bought, one SERP per market, and (if
    enabled) a suggestions pass for every place."""
    if n_places <= 0 or n_markets <= 0:
        return {"llm_calls": 0, "volume_tasks": 0, "serp_calls": 0, "idea_tasks": 0,
                "dfs_usd": 0.0, "llm_usd": 0.0}
    llm_calls = math.ceil(n_places / PROPOSE_CHUNK)
    volume_tasks = n_markets * math.ceil(n_places * KEYWORDS_PER_PLACE / VOLUME_TASK_MAX)
    serp_calls = n_places * SERP_KEYWORDS_PER_PLACE * n_markets
    idea_tasks = math.ceil(n_places / IDEAS_SEEDS_PER_TASK) if use_suggestions else 0
    dfs = (volume_tasks * DFS_CALL_ESTIMATE_USD["search_volume_bulk"]
           + serp_calls * DFS_CALL_ESTIMATE_USD["serp_advanced"]
           + idea_tasks * DFS_CALL_ESTIMATE_USD["keywords_for_keywords"])
    return {"llm_calls": llm_calls, "volume_tasks": volume_tasks, "serp_calls": serp_calls,
            "idea_tasks": idea_tasks, "dfs_usd": round(dfs, 4),
            "llm_usd": round(llm_calls * LLM_PROPOSE_ESTIMATE_USD, 4)}


def _parse_proposals(raw: str) -> dict[str, list[str]]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = repair_json(text, return_objects=True)
    out: dict[str, list[str]] = {}
    items = parsed.get("places") if isinstance(parsed, dict) else None
    for item in items or []:
        if not isinstance(item, dict) or not item.get("place"):
            continue
        kws = [_tidy(k) for k in (item.get("keywords") or []) if isinstance(k, str)]
        out[str(item["place"]).casefold()] = [k for k in kws if k]
    return out


async def _propose(places: list[_Place], pool, guard: _RunGuard, llm_budget: RunBudget | None) -> dict:
    llm = LLMClient()
    calls, cost = 0, 0.0
    for chunk in _chunks(places, PROPOSE_CHUNK):
        if guard.aborted:
            for p in chunk:
                p.failed = True
            continue
        if llm_budget is not None:
            try:
                llm_budget.check(LLM_PROPOSE_ESTIMATE_USD)
            except BudgetExceeded as exc:
                guard.record(exc)
                for p in chunk:
                    p.failed = True
                continue
        listing = "\n".join(
            f"- {p.name} (itineraries: {', '.join(sorted(set(p.actions))[:6]) or 'visit'})" for p in chunk
        )
        request = LLMRequest(system_prompt=PROPOSE_SYSTEM, user_prompt=f"Places:\n{listing}",
                             model_tier="haiku", max_tokens=4096)
        resp = await asyncio.to_thread(llm.generate, request)
        calls += 1
        cost += resp.cost_usd
        if llm_budget is not None:
            llm_budget.charge(resp.cost_usd)
        proposals = _parse_proposals(resp.content)
        await record_call_with_pool(
            pool, stage="a3_search_demand", role="writer", model=resp.model_used,
            tokens_in=resp.input_tokens, tokens_out=resp.output_tokens, cost_usd=resp.cost_usd,
            quality_signal={"phase": "propose", "places": len(chunk), "parsed": len(proposals)},
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
        for p in chunk:
            kws = proposals.get(p.name.casefold()) or []
            plain = _tidy(p.name)
            # The plain place name is always researched, even if the LLM output was unusable.
            ordered = list(dict.fromkeys([plain, *kws] if plain not in kws else kws))
            p.keywords = [k for k in ordered if k][:KEYWORDS_PER_PLACE]
    return {"llm_calls": calls, "llm_usd": round(cost, 5)}


async def _buy_volumes(places: list[_Place], markets: list[tuple[int, str, str]], client: DataForSEOClient,
                       pool, guard: _RunGuard) -> dict:
    stats = {"volume_tasks": 0, "keywords_bought": 0, "keywords_cached": 0}
    for loc, _name, lang in markets:
        market = LOCATION_CODE_TO_MARKET[loc]
        active = [p for p in places if not p.failed and market in p.markets]
        need = sorted({kw for p in active for kw in p.keywords})
        if not need:
            continue
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT keyword, search_volume FROM acp_contract.search_demand "
                "WHERE market = $1 AND keyword = ANY($2::text[]) AND retrieved_on > now() - $3::interval",
                market, need, FRESH_FOR,
            )
        answers: dict[str, int | None] = {r["keyword"]: r["search_volume"] for r in rows}
        if answers:
            stats["keywords_cached"] += len(answers)
            await record_dfs_call_with_pool(
                pool, endpoint="search_volume_bulk", cache_hit=True, cost_usd=0.0, location_code=loc,
                keyword_count=len(answers), meta={"job": "segment_research", "phase": "volumes"},
            )
        failed_kw: set[str] = set()
        for chunk in _chunks([kw for kw in need if kw not in answers], VOLUME_TASK_MAX):
            if guard.aborted:
                failed_kw.update(chunk)
                continue
            try:
                volumes = await client.fetch_volumes_bulk(chunk, loc, lang, raise_on_error=True)
            except Exception as exc:
                guard.record(exc)
                failed_kw.update(chunk)
                continue
            stats["volume_tasks"] += 1
            stats["keywords_bought"] += len(chunk)
            async with pool.acquire() as conn:
                await conn.executemany(_UPSERT_VOLUME_SQL, [(kw, market, volumes.get(kw)) for kw in chunk])
            answers.update({kw: volumes.get(kw) for kw in chunk})
        for p in active:
            if any(kw in failed_kw for kw in p.keywords):
                p.failed = True
                continue
            for kw in p.keywords:
                p.volumes.setdefault(kw, {})[market] = answers.get(kw)
    return stats


async def _buy_serps(places: list[_Place], markets: list[tuple[int, str, str]], client: DataForSEOClient,
                     pool, guard: _RunGuard) -> int:
    location_by_market = {LOCATION_CODE_TO_MARKET[loc]: (loc, lang) for loc, _n, lang in markets}
    jobs = []
    for p in places:
        if p.failed:
            continue
        ranked = sorted(
            ((max((v or 0) for v in p.volumes.get(kw, {}).values()) if p.volumes.get(kw) else 0, kw)
             for kw in p.keywords),
            reverse=True,
        )
        for _vol, kw in [r for r in ranked if r[0] > 0][:SERP_KEYWORDS_PER_PLACE]:
            with_volume = [m for m, v in p.volumes[kw].items() if v]
            jobs.append((p, kw, with_volume))
    sem = asyncio.Semaphore(SERP_CONCURRENCY)

    async def _one(p: _Place, kw: str, mkts: list[str]) -> int:
        async with sem:
            if guard.aborted:
                p.failed = True
                return 0
            try:
                await _serp_tool(client, kw, mkts, location_by_market, pool, guard)
                return len(mkts)
            except PlacePurchaseFailed:
                p.failed = True
                return 0

    return sum(await asyncio.gather(*[_one(*job) for job in jobs]))


async def _buy_suggestions(places: list[_Place], markets: list[tuple[int, str, str]], client: DataForSEOClient,
                           pool, guard: _RunGuard) -> dict:
    loc, _name, lang = markets[0]  # primary market only (same rule as the per-place loop)
    market = LOCATION_CODE_TO_MARKET[loc]
    zero = [p for p in places if not p.failed and not p.has_volume()]
    stats = {"idea_tasks": 0, "ideas_stored": 0}
    for chunk in _chunks(zero, IDEAS_SEEDS_PER_TASK):
        if guard.aborted:
            for p in chunk:
                p.failed = True
            continue
        seeds = [p.keywords[0] if p.keywords else _tidy(p.name) for p in chunk]
        try:
            ideas = await client.fetch_keyword_ideas_multi(seeds, loc, lang)
        except Exception as exc:
            guard.record(exc)
            for p in chunk:
                p.failed = True
            continue
        stats["idea_tasks"] += 1
        stats["ideas_returned"] = stats.get("ideas_returned", 0) + len(ideas)
        rows = [(_tidy(i["keyword"]), market, i.get("search_volume"))
                for i in ideas if i.get("keyword") and i.get("search_volume")]
        # Bhutan pilot (28/09) stored 0 ideas from an 18-seed task: log what came back so we can
        # tell "DFS returned nothing for obscure seeds" from "we parsed the response wrong".
        logger.info("segment_research_ideas_task", seeds=len(seeds), ideas_returned=len(ideas),
                    ideas_with_volume=len(rows), sample=[i.get("keyword") for i in ideas[:5]])
        if rows:
            async with pool.acquire() as conn:
                await conn.executemany(_UPSERT_VOLUME_SQL, rows)
            stats["ideas_stored"] += len(rows)
    return stats


async def research_batch(
    stale: list[tuple[str, list[str], list[str]]],
    markets: list[tuple[int, str, str]],
    pool, client: DataForSEOClient, guard: _RunGuard, *,
    llm_budget: RunBudget | None = None,
    use_suggestions: bool = True,
) -> dict:
    """Research `stale` = [(place, actions, stale_markets)] in phases (module docstring)."""
    places = [_Place(name, actions, list(mkts)) for name, actions, mkts in stale]
    llm_stats = await _propose(places, pool, guard, llm_budget)
    volume_stats = await _buy_volumes(places, markets, client, pool, guard)
    serp_calls = await _buy_serps(places, markets, client, pool, guard)
    idea_stats = (await _buy_suggestions(places, markets, client, pool, guard)
                  if use_suggestions else {"idea_tasks": 0, "ideas_stored": 0})
    idea_stats.setdefault("ideas_returned", 0)

    done = [p for p in places if not p.failed and not guard.aborted]
    if done:
        async with pool.acquire() as conn:
            await conn.executemany(_LOG_RESEARCHED_SQL, [(p.name, m) for p in done for m in p.markets])
    logger.info("segment_research_batch_done", places=len(places), researched=len(done),
                aborted=guard.aborted, **llm_stats, **volume_stats, **idea_stats, serp_calls=serp_calls)
    return {
        "places_researched": len(done),
        "places_failed": len(places) - len(done),
        "keywords_bought": volume_stats["keywords_bought"],
        "keywords_cached": volume_stats["keywords_cached"],
        "volume_tasks": volume_stats["volume_tasks"],
        "serp_calls": serp_calls,
        "idea_tasks": idea_stats["idea_tasks"],
        "ideas_stored": idea_stats["ideas_stored"],
        "ideas_returned": idea_stats["ideas_returned"],
        "llm_calls": llm_stats["llm_calls"],
        "cost_usd": llm_stats["llm_usd"],
    }
