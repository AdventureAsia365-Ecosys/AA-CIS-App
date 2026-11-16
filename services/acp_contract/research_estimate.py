"""services/acp_contract/research_estimate.py — AA-1120: the deduplicated pre-run estimate of a segment research run.

`segment_research_batch.estimate_plan` is the upper bound (it ignores the store); the USD cap and the balance check use
it. This module prices what the run will really buy after the research cache and the deduplication, from store reads
only (0 USD, no provider call); `run_segment_research` returns it as `estimate.deduped` in the preview and the run.
"""
from __future__ import annotations

import math

from services.seo_intelligence.research_cache import ResearchCache
from shared.cost_guard import dfs_call_estimate_usd


async def estimate_deduped(places: list, cache: ResearchCache, queue: str = "live",
                           use_suggestions: bool = False) -> dict:
    """AA-1120 — the DataForSEO calls a run over `places` really makes after the research cache and the deduplication,
    and their USD (store reads only: 0 USD, no provider call). `estimate_plan` stays the upper bound the USD cap and
    the balance check use; this is the figure the preview shows next to it (`estimate.deduped`).

      - volume: per market, the DISTINCT keywords of the mapped places that are not fresh in the store, plus
        REAL_KEYWORDS_PER_PLACE new keywords per place without a usable map (new keywords are not in the store yet), in
        tasks of up to VOLUME_TASK_MAX keywords;
      - SERP: a mapped place whose keywords all have a fresh volume (any requested market or the SERP market) gets its
        best keyword (best_keyword); ONE SERP per distinct best keyword not fresh in the SERP market. A place whose
        volumes are still unknown counts SERP_HIT_RATE of a SERP (no dedupe possible before its volumes are bought);
      - `calls` = the rows the ledger will get: volume tasks + SERP requests (queue mode: posts of up to 100 SERPs).
    """
    from services.acp_contract.segment_research_batch import (
        IDEAS_SEEDS_PER_TASK, REAL_KEYWORDS_PER_PLACE, SERP_HIT_RATE, SERP_MARKET, VOLUME_TASK_MAX, _prices,
        best_keyword)

    volume_price, serp_price = _prices(queue)
    markets = sorted({m for p in places for m in p.markets} | ({SERP_MARKET} if any(p.serp for p in places) else set()))
    mapped_kws = sorted({kw for p in places if p.has_map for kw in p.keywords})
    stored: dict[str, dict[str, int | None]] = {m: await cache.volumes(mapped_kws, m) for m in markets} \
        if mapped_kws else {}
    to_buy: dict[str, set[str]] = {}
    asked: dict[str, int] = {}
    new_kw: dict[str, float] = {}
    for p in places:
        for m in p.markets:
            if p.has_map:
                asked[m] = asked.get(m, 0) + len(p.keywords)
                to_buy.setdefault(m, set()).update(kw for kw in p.keywords if kw not in stored.get(m, {}))
            else:
                new_kw[m] = new_kw.get(m, 0.0) + REAL_KEYWORDS_PER_PLACE
    keywords_to_buy = {m: len(to_buy.get(m, ())) + math.ceil(new_kw.get(m, 0.0)) for m in set(to_buy) | set(new_kw)}
    volume_tasks = sum(math.ceil(n / VOLUME_TASK_MAX) for n in keywords_to_buy.values() if n > 0)
    cached = sum(len(v) for m, v in stored.items() if any(m in p.markets for p in places))

    best: dict[str, int] = {}
    known: set[str] = set()
    for per in stored.values():
        for kw, v in per.items():
            best[kw] = max(best.get(kw, 0), v or 0)
            known.add(kw)
    candidates: dict[str, int] = {}
    unknown = 0
    no_volume = 0
    for p in places:
        if not p.serp:
            continue
        if not p.has_map or not p.keywords or any(kw not in known for kw in p.keywords):
            unknown += 1 if p.keywords or not p.has_map else 0
            continue
        top = best_keyword(p, best)
        if top is None:
            no_volume += 1
            continue
        candidates[top] = candidates.get(top, 0) + 1
    fresh = await cache.serps(list(candidates), SERP_MARKET) if candidates else {}
    serp_known = [kw for kw in candidates if kw not in fresh]
    serp_calls = len(serp_known) + math.ceil(unknown * SERP_HIT_RATE)
    serp_requests = math.ceil(serp_calls / 100) if queue == "standard" else serp_calls
    idea_tasks = math.ceil(len(places) / IDEAS_SEEDS_PER_TASK) if use_suggestions else 0
    ideas = idea_tasks * dfs_call_estimate_usd().get("keywords_for_keywords", 0.1)
    return {
        "volume_tasks": volume_tasks,
        "keywords_asked": sum(asked.values()) + sum(math.ceil(n) for n in new_kw.values()),
        "keywords_to_buy": sum(keywords_to_buy.values()),
        "keywords_cached": cached,
        "serp_calls": serp_calls,
        "serp_requests": serp_requests,
        "serp_cached": len(fresh),
        "serp_shared_keyword": sum(n - 1 for n in candidates.values()),
        "serp_no_volume": no_volume,
        "serp_unknown_volume": unknown,
        "idea_tasks": idea_tasks,
        "calls": volume_tasks + serp_requests + idea_tasks,
        "dfs_usd": round(volume_tasks * volume_price + serp_calls * serp_price + ideas, 4),
        "queue": queue,
    }


__all__ = ["estimate_deduped"]
