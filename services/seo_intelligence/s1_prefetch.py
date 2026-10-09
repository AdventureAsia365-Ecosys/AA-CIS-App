"""AA-653 — S1 DataForSEO spend: long reuse + batched keyword ideas.

Per tour, S1 used to buy three live DataForSEO tasks: search_volume for ONE keyword ($0.09 — DFS
bills per task, up to 1,000 keywords), keywords_for_keywords ($0.09) and a SERP ($0.002). For the
736-tour rerun that is ~$130. Now:

1. **Reuse** — a tour's own `seo_context` row in the current format is reused for
   `S1_SEO_REUSE_DAYS` (default 180): a master tour is not rewritten often, and search demand for a
   trip changes slowly. Nothing is bought for a tour that already has one.
2. **Research cache first** — `acp_contract.search_demand` (bought by Segment research) already
   holds volumes + PAA for many places; ideas naming the tour's places are taken from there free.
3. **Batch** — the remaining tours share keywords_for_keywords tasks (≤20 seeds per task, same
   country together). Ideas are assigned back per tour with the same relevance rule as before
   (title place / activity), and an idea naming another tour's place in the batch is not taken.
4. **No separate search_volume task** — the ideas task returns volume for the seeds themselves.
5. SERP stays per tour ($0.002) for People-Also-Ask, skipped when the research cache already has
   PAA for the tour's places.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import structlog

from .seed_builder import (
    LOCATION_CODE_TO_MARKET, _WEAK_PLACE_WORDS, activity_terms, build_seed, idea_seeds, is_lodging_search,
    rank_keyword_ideas, title_place_terms,
)

logger = structlog.get_logger()

SEO_FORMAT = "ideas_v3"          # marker in seo_context.cache_key for rows built this way
# 180 days: Google Ads volumes are 12-month averages (seasonality already inside), but keyword
# ideas and PAA drift with trends — two refreshes a year at most, and only when a tour is rewritten.
REUSE_DAYS = int(os.environ.get("S1_SEO_REUSE_DAYS", "180"))
# AA-706 — Jev judges each candidate idea (shadow until calibrated; see migration 196).
JEV_STAGE = "a1_seo"
JEV_Q = "a1_keyword_about_tour"
JEV_PER_TOUR = 30
JEV_CONCURRENCY = 8
MAX_SEEDS_PER_TASK = 20           # DFS keywords_for_keywords limit, same price for 1 or 20
MIN_CACHED_IDEAS = 5              # research cache alone is enough when it has this many
# AA-747: commit the prefetch's seo_context rows in batches of this many tours, in the order given,
# so a concurrently-running s1_rewrite job finds the early tours' rows within ~1 min instead of
# waiting for the whole prefetch (~8 min / 50 tours) to finish.
PREFETCH_COMMIT_BATCH = 5


def cache_key(seed: str, location_code: int) -> str:
    return f"seo:{seed.lower().replace(' ', '_')}:{location_code}:{SEO_FORMAT}"


def strong_terms(places: list[str]) -> list[str]:
    return [p for p in places if p and p not in _WEAK_PLACE_WORDS]


def tour_spec(row: dict) -> dict:
    """Seeds and relevance terms for one raw_tours row (pure)."""
    acts = row.get("activities")
    seeds = idea_seeds(row.get("country"), acts, row.get("src_name"))
    seed = build_seed(row.get("country"), acts, row.get("src_name")) or row.get("src_name") or ""
    return {
        "tour_id": str(row["tour_id"]),
        "name": row.get("src_name") or "",
        "country": row.get("country") or "",
        "seed": seed,
        "seeds": seeds or ([seed] if seed else []),
        "places": title_place_terms(row.get("src_name"), row.get("country")),
        "activity": activity_terms(acts),
    }


def plan_batches(specs: list[dict], max_seeds: int = MAX_SEEDS_PER_TASK) -> list[list[dict]]:
    """Group tours into keywords_for_keywords tasks: same country together, ≤max_seeds seeds."""
    batches: list[list[dict]] = []
    by_country: dict[str, list[dict]] = {}
    for s in specs:
        by_country.setdefault(s["country"], []).append(s)
    for country in sorted(by_country):
        cur: list[dict] = []
        n = 0
        for s in by_country[country]:
            k = len(s["seeds"][:max_seeds])
            if cur and n + k > max_seeds:
                batches.append(cur)
                cur, n = [], 0
            cur.append(s)
            n += k
        if cur:
            batches.append(cur)
    return batches


def candidates_for(batch: list[dict], spec: dict, ideas: list[dict]) -> list[dict]:
    """Ideas of one shared task that may belong to `spec`: an idea naming a strong place of another
    tour in the batch (and none of this tour's) is that tour's keyword, not this one's."""
    own = set(strong_terms(spec["places"]))
    others = {t for o in batch if o is not spec for t in strong_terms(o["places"])} - own
    return [i for i in ideas
            if not any(t in str(i.get("keyword", "")).lower() for t in others)
            or any(t in str(i.get("keyword", "")).lower() for t in own)]


def assign_ideas(batch: list[dict], ideas: list[dict]) -> dict[str, list[dict]]:
    """Ideas from one shared task, split back per tour with the substring relevance rule."""
    return {s["tour_id"]: rank_keyword_ideas(candidates_for(batch, s, ideas), s["places"],
                                             activity_words=s["activity"], country=s["country"],
                                             foreign_places=s.get("foreign"))
            for s in batch}


def _vol(i: dict) -> int:
    v = i.get("search_volume")
    return v if isinstance(v, int) and v > 0 else 0


async def jev_review(spec: dict, candidates: list[dict], kept: list[dict], *, pool=None) -> list[dict]:
    """AA-706 — ask Jev about up to JEV_PER_TOUR candidates (by volume). In shadow (the seeded mode)
    verdicts are only logged and `kept` is returned unchanged; once enforced, a confident accept adds
    an idea the substring rule missed (synonyms: "tiger's nest" for Taktsang) and a confident reject
    drops one it kept."""
    import asyncio

    from shared.llm_client.decide import decide

    ask = sorted(candidates, key=lambda i: -_vol(i))[:JEV_PER_TOUR]
    if not ask:
        return kept
    sem = asyncio.Semaphore(JEV_CONCURRENCY)
    state_base = {"tour": spec.get("name") or spec["seed"], "country": spec["country"],
                  "title_places": spec["places"], "activity": spec["activity"]}

    async def _one(idea):
        async with sem:
            return idea, await decide(JEV_STAGE, f"kw:{spec['tour_id']}:{idea['keyword'].lower()}",
                                      {**state_base, "keyword": idea["keyword"]}, [JEV_Q], pool=pool)

    results = await asyncio.gather(*[_one(i) for i in ask])
    keep = {i["keyword"].casefold(): i for i in kept}
    for idea, dec in results:
        k = idea["keyword"].casefold()
        if dec.rejected(JEV_Q):
            keep.pop(k, None)
        elif dec.accepted(JEV_Q):
            keep.setdefault(k, idea)
    if len(keep) == len(kept) and all(i["keyword"].casefold() in keep for i in kept):
        return kept                                   # shadow / no enforced change: same order
    return sorted(keep.values(), key=lambda i: (_vol(i) == 0, -_vol(i)))


def cached_ideas(spec: dict, demand_rows: list[dict]) -> tuple[list[dict], list[str]]:
    """(ideas, paa) for one tour from search_demand rows naming one of its strong places."""
    terms = strong_terms(spec["places"])
    if not terms:
        return [], []
    hits = [r for r in demand_rows if any(t in r["keyword"].lower() for t in terms)]
    ideas = [{"keyword": r["keyword"], "search_volume": r["search_volume"], "competition": None,
              "competition_index": None, "cpc": None, "source": "search_demand"} for r in hits]
    paa: list[str] = []
    for r in hits:
        for q in (r.get("people_also_ask") or [])[:5]:
            q = q if isinstance(q, str) else (q.get("question") or q.get("title") or "")
            if q and q not in paa:
                paa.append(q)
    return rank_keyword_ideas(ideas, spec["places"], activity_words=spec["activity"],
                              country=spec["country"], foreign_places=spec.get("foreign")), paa[:10]


async def fresh_tour_ids(conn, tour_ids: list[str]) -> set[str]:
    """Tours whose seo_context is already in this format and younger than REUSE_DAYS."""
    rows = await conn.fetch(
        """SELECT tour_id::text FROM silver_aa_internal.seo_context
            WHERE tour_id = ANY($1::uuid[]) AND cache_key LIKE $2
              AND fetched_at > now() - make_interval(days => $3)""",
        tour_ids, f"%:{SEO_FORMAT}", REUSE_DAYS)
    return {r["tour_id"] for r in rows}


async def load_fresh(conn, tour_id: str) -> dict | None:
    """seo_data shaped like DataForSEOClient.fetch_all() from a reusable row, else None."""
    row = await conn.fetchrow(
        """SELECT keyword_search, keyword_ideas, top_keywords, people_also_ask, related_keywords
             FROM silver_aa_internal.seo_context
            WHERE tour_id = $1::uuid AND cache_key LIKE $2
              AND fetched_at > now() - make_interval(days => $3)""",
        tour_id, f"%:{SEO_FORMAT}", REUSE_DAYS)
    if not row:
        return None

    def _j(v, default):
        if v is None:
            return default
        return json.loads(v) if isinstance(v, str) else v
    top = _j(row["top_keywords"], [])
    return {
        "keywords": {"top_keywords": top},
        "keyword_ideas": _j(row["keyword_ideas"], []),
        "people_also_ask": _j(row["people_also_ask"], []),
        "related_keywords": _j(row["related_keywords"], []),
        "destination": row["keyword_search"],
    }


# AA-747: a rewrite started at the same time as the prefetch (the FE no longer awaits the prefetch)
# must not buy DataForSEO for a tour the prefetch is about to commit. These let the S1 SEO step wait
# for the prefetch's own row for this tour instead of buying — and fall back to the per-tour fetch
# after a cap, so a stuck/failed prefetch never blocks a rewrite indefinitely. The prefetch commits
# per batch of 5, so an early tour's row appears within ~1 min; never a double buy (once the row
# exists, load_fresh reuses it).
PREFETCH_WAIT_POLL_SECONDS = 10
PREFETCH_WAIT_CAP_SECONDS = 300


async def prefetch_job_covers_tour(conn, tour_id: str) -> bool:
    """True if an s1_seo_prefetch job that INCLUDES this tour is still queued or running — i.e. the
    tour's seo_context row is on its way, so the rewrite should wait for it rather than buy."""
    row = await conn.fetchrow(
        """SELECT 1 FROM shared.job
            WHERE kind = 's1_seo_prefetch' AND status IN ('queued', 'running')
              AND payload -> 'tour_ids' @> $1::jsonb
            LIMIT 1""",
        json.dumps([str(tour_id)]))
    return row is not None


async def wait_for_prefetched_seo(tour_id: str, *, poll_seconds: int = PREFETCH_WAIT_POLL_SECONDS,
                                  cap_seconds: int = PREFETCH_WAIT_CAP_SECONDS) -> dict | None:
    """AA-747: poll for the prefetch's own seo_context row for `tour_id`, every `poll_seconds` up to
    `cap_seconds`. Returns the reusable seo_data as soon as the row appears, or None after the cap
    (caller then falls back to its own per-tour fetch). Opens short-lived connections (same style as
    process_seo) so it never shares one connection across concurrent tasks (S219 #600). Only waits
    while a prefetch job that includes this tour is still queued/running — stops early if the job
    disappears (finished/failed) and the row still isn't there."""
    import asyncio
    import time

    import asyncpg

    from shared.secrets import get_database_url

    deadline = time.monotonic() + max(0, cap_seconds)
    while True:
        conn = await asyncpg.connect(get_database_url())
        try:
            fresh = await load_fresh(conn, str(tour_id))
            if fresh:
                return fresh
            covered = await prefetch_job_covers_tour(conn, str(tour_id))
        finally:
            await conn.close()
        if not covered:
            return None  # no in-flight prefetch for this tour anymore and still no row
        if time.monotonic() >= deadline:
            logger.info("s1_seo_prefetch_wait_cap_reached", tour_id=str(tour_id))
            return None
        await asyncio.sleep(poll_seconds)


async def _persist(conn, spec: dict, location_code: int, ideas: list[dict], paa: list[str],
                   related: list[str], tenant_id: str) -> None:
    from shared.repository.seo_context_repository import SeoContextRepository
    top = [i["keyword"] for i in ideas if (i.get("search_volume") or 0) > 0][:10] or [spec["seed"]]
    await SeoContextRepository(conn).insert({
        "tour_id": spec["tour_id"],
        "tenant_id": tenant_id,
        "keyword_search": spec["seed"],
        "keyword_ideas": json.dumps(ideas, default=str),
        "top_keywords": json.dumps(top),
        "people_also_ask": json.dumps(paa, default=str),
        "related_keywords": json.dumps(related, default=str),
        "cache_key": cache_key(spec["seed"], location_code),
        "expires_at": datetime.utcnow() + timedelta(days=REUSE_DAYS),
    })


async def foreign_places(conn, country: str) -> frozenset[str]:
    """Lowercase names of places in OTHER countries (shared.destinations, read only), minus names
    that also exist in `country` and generic words. Empty on any error."""
    from .seed_builder import _GENERIC_TITLE_WORDS, _WEAK_PLACE_WORDS
    skip = _GENERIC_TITLE_WORDS | _WEAK_PLACE_WORDS
    try:
        rows = await conn.fetch("SELECT lower(name) AS n, country FROM shared.destinations")
        own = {r["n"] for r in rows if r["country"] == country}
        return frozenset(r["n"] for r in rows if r["country"] != country and r["n"] not in own
                         and len(r["n"]) >= 4 and r["n"] not in skip)
    except Exception as e:
        logger.warning("foreign_places_unavailable", error=str(e)[:200])
        return frozenset()


async def prefetch(conn, rows: list[dict], *, tenant_id: str, location_code: int,
                   language_code: str, client=None, progress=None, pool=None, jev: bool = True) -> dict:
    """Fill seo_context for every tour in `rows` that has no reusable one. Returns a summary."""
    from .dataforseo_client import DataForSEOClient

    specs = [tour_spec(r) for r in rows]
    specs = [s for s in specs if s["seed"]]
    fp_by_country = {c: await foreign_places(conn, c) for c in {s["country"] for s in specs}}
    for s in specs:
        s["foreign"] = fp_by_country[s["country"]]
    fresh = await fresh_tour_ids(conn, [s["tour_id"] for s in specs])
    todo = [s for s in specs if s["tour_id"] not in fresh]
    market = LOCATION_CODE_TO_MARKET.get(location_code, "US")
    demand = [dict(r) for r in await conn.fetch(
        """SELECT keyword, search_volume, people_also_ask FROM acp_contract.search_demand
            WHERE market = $1 AND search_volume > 0""", market)]
    for d in demand:
        if isinstance(d.get("people_also_ask"), str):
            d["people_also_ask"] = json.loads(d["people_also_ask"])

    summary = {"tours": len(specs), "reused": len(fresh), "from_research_cache": 0,
               "ideas_tasks": 0, "serp_calls": 0}
    cached: dict[str, tuple[list[dict], list[str]]] = {}
    buy: list[dict] = []
    for s in todo:
        ideas, paa = cached_ideas(s, demand)
        if len([i for i in ideas if (i.get("search_volume") or 0) > 0]) >= MIN_CACHED_IDEAS:
            cached[s["tour_id"]] = (ideas, paa)
        else:
            buy.append(s)
            if ideas:
                cached[s["tour_id"]] = (ideas, paa)  # merged with bought ideas below

    client = client or DataForSEOClient(tenant_id=tenant_id)

    # AA-747: process the tours IN THE ORDER GIVEN, in batches of 5, committing each batch's
    # seo_context rows before starting the next — so a running s1_rewrite job finds the early
    # tours' rows within ~1 min instead of waiting for the whole ~8 min prefetch. Within a batch
    # the paid keywords_for_keywords task is still shared (plan_batches: same country, ≤20 seeds),
    # so this does not cost more DFS tasks than the old all-at-once path.
    done = 0
    buy_ids = {s["tour_id"] for s in buy}
    for i in range(0, len(todo), PREFETCH_COMMIT_BATCH):
        chunk = todo[i:i + PREFETCH_COMMIT_BATCH]
        chunk_buy = [s for s in chunk if s["tour_id"] in buy_ids]
        bought: dict[str, list[dict]] = {}
        for dfs_batch in plan_batches(chunk_buy):
            seeds: list[str] = []
            for s in dfs_batch:
                for sd in s["seeds"]:
                    if sd.casefold() not in {x.casefold() for x in seeds}:
                        seeds.append(sd)
            try:
                ideas = await client.fetch_keyword_ideas_multi(seeds[:MAX_SEEDS_PER_TASK], location_code,
                                                               language_code, limit=300)
                summary["ideas_tasks"] += 1
            except Exception as e:  # BudgetExceeded included: stop buying, keep what we have
                from shared.cost_guard import BudgetExceeded
                if isinstance(e, BudgetExceeded):
                    raise
                logger.warning("s1_prefetch_ideas_failed", error=str(e)[:200], seeds=len(seeds))
                ideas = []
            for s in dfs_batch:
                bought[s["tour_id"]] = candidates_for(dfs_batch, s, ideas)

        for s in chunk:
            ideas, paa = cached.get(s["tour_id"], ([], []))
            candidates = list(ideas)
            if s["tour_id"] in bought:
                seen = {i["keyword"].casefold() for i in candidates}
                candidates += [i for i in bought[s["tour_id"]] if i["keyword"].casefold() not in seen]
            else:
                summary["from_research_cache"] += 1
            ideas = rank_keyword_ideas(candidates, s["places"], activity_words=s["activity"], country=s["country"],
                                                 foreign_places=s.get("foreign"))
            # AA-706: Jev sees every candidate — research-cache ideas too (the pilot's "druk hotel
            # paro" came from search_demand and bypassed the question). Lodging searches never reach it.
            if jev:
                ideas = await jev_review(s, [i for i in candidates if not is_lodging_search(i["keyword"])],
                                         ideas, pool=pool)
            related: list[str] = []
            if not paa:
                try:
                    client.tour_id = s["tour_id"]
                    serp = await client._serp_advanced(s["seed"], location_code, language_code)
                    paa, related = client._parse_paa(serp), client._parse_related(serp)
                    summary["serp_calls"] += 1
                except Exception as e:
                    from shared.cost_guard import BudgetExceeded
                    if isinstance(e, BudgetExceeded):
                        raise
                    logger.warning("s1_prefetch_serp_failed", tour_id=s["tour_id"], error=str(e)[:200])
            # Commit this tour's row now (asyncpg auto-commits each statement) — a concurrent
            # s1_rewrite job polling for this tour_id sees it as soon as this returns.
            await _persist(conn, s, location_code, ideas[:25], paa, related, tenant_id)
            done += 1
        if progress is not None:
            await progress(done=done, total=len(todo))
    logger.info("s1_prefetch_done", **summary)
    return summary


async def tenant_market_seo(conn, tenant_id: str, tour_id: str) -> tuple[str, dict | None]:
    """AA-707 — T2 keywords for the tenant's OWN buyer market, from the research cache only (a
    tenant action never buys DataForSEO). Returns (market, seo_data | None when the cache has nothing
    for this tour in that market)."""
    from shared.services.tenant_config_service import TenantConfigService

    from .seed_builder import resolve_buyer_market
    try:
        cfg = await TenantConfigService(conn).get_seo_config(tenant_id)
        location_code, _name, _lang = resolve_buyer_market(cfg.target_market)
    except Exception as e:
        logger.warning("t2_market_resolve_failed", tenant_id=tenant_id, error=str(e)[:200])
        return "US", None
    market = LOCATION_CODE_TO_MARKET.get(location_code, "US")
    row = await conn.fetchrow(
        "SELECT tour_id, src_name, country, activities FROM silver_aa_internal.raw_tours "
        "WHERE tour_id = $1::uuid", tour_id)
    if not row:
        return market, None
    spec = tour_spec(dict(row))
    spec["foreign"] = await foreign_places(conn, spec["country"])
    terms = strong_terms(spec["places"]) + spec["activity"]
    if not terms:
        return market, None
    demand = [dict(r) for r in await conn.fetch(
        """SELECT keyword, search_volume, people_also_ask FROM acp_contract.search_demand
            WHERE market = $1 AND search_volume > 0 AND keyword ILIKE ANY($2::text[])""",
        market, [f"%{t}%" for t in terms])]
    for d in demand:
        if isinstance(d.get("people_also_ask"), str):
            d["people_also_ask"] = json.loads(d["people_also_ask"])
    ideas, paa = cached_ideas(spec, demand)
    if not ideas:
        # activity-only matches (cached_ideas needs a place) still count for the tenant's market
        acts = spec["activity"]
        ideas = rank_keyword_ideas([{"keyword": d["keyword"], "search_volume": d["search_volume"]}
                                    for d in demand], spec["places"], activity_words=acts,
                                   country=spec["country"], foreign_places=spec.get("foreign"))
    top = [i["keyword"] for i in ideas if _vol(i) > 0][:10]
    if not top:
        return market, None
    return market, {"keywords": {"top_keywords": top}, "top_keywords": top,
                    "people_also_ask": paa, "seo_market": market}
