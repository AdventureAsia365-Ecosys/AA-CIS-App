import asyncpg
import json
import redis.asyncio as _aioredis
import structlog
from datetime import datetime, timedelta
from .dataforseo_client import (
    DataForSEOClient,
    DEFAULT_LOCATION_CODE, DEFAULT_LOCATION_NAME, DEFAULT_LANGUAGE_CODE,
)
from .seed_builder import resolve_buyer_market
from shared import cost_guard
from shared.secrets import get_database_url
from shared.dfs_client.call_log import record_dfs_call
from shared.services.tenant_config_service import TenantConfigService
from shared.repository.seo_context_repository import SeoContextRepository
from shared.cache.redis_cache import RedisCache
from shared.cache.redis_conn import redis_conn_args

logger = structlog.get_logger()

# AA-249: module-level singleton, built once at import — shared by every
# process_seo() call regardless of caller (HTTP route or the background 3-worker
# job queue, AA-223, neither of which guarantees a Request/app.state in scope).
# redis.asyncio.from_url() is lazy — it does no I/O here, just builds a client
# backed by its own internal connection pool; real connections open on first
# command and are managed/reconnected by the library for the life of the worker
# process. No explicit shutdown hook: the socket closes with the process, and a
# single long-lived client isn't a meaningful leak to guard against.
# AA-874: URL + TLS / AUTH kwargs from env (shared/cache/redis_conn.py); localhost when REDIS_HOST is unset.
_redis_url, _redis_kwargs = redis_conn_args()
_seo_redis_client = _aioredis.from_url(_redis_url, encoding="utf-8", decode_responses=True, **_redis_kwargs)

# AA-625: SEO cache TTL raised 24h -> 7 days. Search volume / SERP for travel keywords change on a
# weekly/monthly cadence, not daily, so a 24h TTL forced a fresh (billable) DataForSEO fetch for the
# same (seed, market) whenever a rerun spanned more than a day. 7 days keeps cache HITs across a
# multi-day rerun while still refreshing often enough for demand to stay current. Single source of
# truth for both the Redis TTL and the seo_context.expires_at trace column below.
_SEO_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 604800

# AA-642 (S0): every live DataForSEO fetch of process_seo runs under a cost_guard.RunBudget. The S1 rewrite (the
# s1_rewrite job, the sync /admin/run-tour and the S1 batch prep, one tour each) uses the job kind below
# (shared.spend_budget dfs / job:s1_rewrite, migration 318: 0.50 USD per tour, hard stop); a caller that names no
# kind gets DEFAULT_BUDGET_KIND, i.e. the dfs `global` row only.
S1_REWRITE_BUDGET_KIND = "s1_rewrite"
DEFAULT_BUDGET_KIND = "global"


async def _load_budget(kind: str) -> "cost_guard.RunBudget":
    """The DataForSEO run budget of `kind` on its own connection (process_seo holds no pool). Raises when the DB
    cannot be read: no live DataForSEO call is made without a budget (the S1 callers then go on without SEO)."""
    conn = await asyncpg.connect(get_database_url())
    try:
        return await cost_guard.load_run_budget(cost_guard.ConnAsPool(conn), "dfs", kind)
    finally:
        await conn.close()


async def _alert_stop(budget, tour_id: str, stop: dict) -> None:
    """The budget bell + ops e-mail when a cap stopped a tour's DataForSEO fetch (cost_guard.maybe_alert throttles
    to one per provider per 24 h). Best-effort."""
    try:
        conn = await asyncpg.connect(get_database_url())
        try:
            await cost_guard.maybe_alert(
                cost_guard.ConnAsPool(conn), budget,
                reason=f"DataForSEO budget stopped the SEO fetch of tour {tour_id}: {stop.get('error', '')}"[:500])
        finally:
            await conn.close()
    except Exception as exc:
        logger.warning("seo_budget_alert_failed", tour_id=tour_id, error=str(exc)[:200])


async def process_seo(
    tour_id: str,
    destination: str,
    activity: str = None,
    cache=None,
    seo_mode: str = "dataforseo",
    seed: str = "",
    tenant_id: str = None,
    extra_seeds: list[str] | None = None,
    place_terms: list[str] | None = None,
    activity_words: list[str] | None = None,
    country: str = "",
    budget: "cost_guard.RunBudget | None" = None,
    budget_kind: str | None = None,
) -> dict:
    """AA-642 (S0): `budget` caps the live DataForSEO calls of this fetch (checked before each, charged after). None =
    loaded only when a live fetch is needed, for `budget_kind` (else DEFAULT_BUDGET_KIND), so no path calls
    DataForSEO without a cap. When the cap or a fatal DataForSEO error (auth, no money) stops the fetch, the partial
    data is returned with status 'budget_stopped' / 'dfs_stopped', nothing is cached or persisted, and a budget stop
    raises the budget alert. The S1 callers then write without fresh SEO data, as they did when DataForSEO failed."""
    # "disabled" — skip SEO step entirely
    if seo_mode == "disabled":
        logger.info("seo_disabled", tour_id=tour_id)
        return {"status": "disabled", "data": {}}

    # AA-197: caller passes a pre-built seed; fall back to destination for old callers.
    effective_seed = seed or destination

    # AA-653: S1 (the only caller passing extra_seeds) reuses the tour's own seo_context row for
    # S1_SEO_REUSE_DAYS — filled by an earlier run or by the batched S1 prefetch job — instead of
    # buying DataForSEO again for a tour that is already in Master Content.
    # extra_seeds is a list (possibly EMPTY: a one-seed title like "LAYA-GASA TREK") for every S1
    # call, None for other callers — the S1 pilot's Laya-Gasa skipped reuse on `[]` and re-bought.
    if extra_seeds is not None and tour_id and seo_mode == "dataforseo":
        from .s1_prefetch import load_fresh, prefetch_job_covers_tour, wait_for_prefetched_seo
        try:
            reuse_conn = await asyncpg.connect(get_database_url())
            try:
                fresh = await load_fresh(reuse_conn, tour_id)
                # AA-747: the S1 page now starts the rewrite workers at the same time as the batched
                # prefetch (no await on it). If this tour has no row yet BUT a prefetch job that
                # includes it is still queued/running, wait for the prefetch's own row (poll ~10s,
                # cap 5 min) instead of buying DataForSEO here — the prefetch commits per batch of 5,
                # so an early tour's row lands within ~1 min. After the cap we fall through to the
                # per-tour fetch below. This is the "never buy twice" guarantee: once the row exists,
                # we reuse it; we only buy if the prefetch never produced one.
                covered = fresh is None and await prefetch_job_covers_tour(reuse_conn, tour_id)
            finally:
                await reuse_conn.close()
        except Exception as _reuse_err:
            logger.warning("seo_reuse_lookup_failed", tour_id=tour_id, error=str(_reuse_err))
            fresh = None
            covered = False
        if not fresh and covered:
            logger.info("seo_waiting_on_prefetch", tour_id=tour_id)
            waited = await wait_for_prefetched_seo(tour_id)
            if waited:
                fresh = waited
        if fresh:
            logger.info("seo_reused", tour_id=tour_id)
            return {"status": "reused", "data": fresh}

    # AA-197: resolve buyer market from tenant target_market (UNWIRED before).
    location_code, location_name, language_code = (
        DEFAULT_LOCATION_CODE, DEFAULT_LOCATION_NAME, DEFAULT_LANGUAGE_CODE,
    )
    if tenant_id:
        try:
            mkt_conn = await asyncpg.connect(get_database_url())
            try:
                cfg = await TenantConfigService(mkt_conn).get_seo_config(tenant_id)
                location_code, location_name, language_code = resolve_buyer_market(cfg.target_market)
            finally:
                await mkt_conn.close()
        except Exception as _mkt_err:
            logger.warning("seo_market_resolve_failed", tenant_id=tenant_id, error=str(_mkt_err))

    # AA-249: default to the real Redis singleton (module-level, see top of file).
    cache = cache or RedisCache(client=_seo_redis_client)
    # AA-249: this is the ephemeral cache-layer key (Redis, TTL, shared across tours
    # of the same country/market) — deliberately named apart from the seo_context.
    # cache_key DB column (persist layer, per-tour, trace-only since migration 075).
    # Cache per (seed, buyer market) — same tour for a different market is a distinct entry.
    redis_seed_key = RedisCache.make_key(effective_seed, str(location_code))
    if extra_seeds is not None:
        # AA-702: multi-seed ideas are a different (richer) result for the same seed — own key, so
        # 7-day-old single-seed entries are not served back.
        redis_seed_key += ":ideas_v3"

    try:
        cached = await cache.get(redis_seed_key)
    except Exception as _cache_err:
        logger.warning("seo_cache_get_failed", error=str(_cache_err))
        cached = None

    if cached:
        # AA-249: the cache decides whether DataForSEO needs to be called again —
        # it must NOT decide whether THIS tour gets its own seo_context row. Redis
        # is shared across every tour of the same country/market; a hit here only
        # means some other tour already paid for the fetch, not that this tour_id
        # has been persisted. Falls through to the shared insert below instead of
        # returning early (that early return was the bug: confirmed live on Dev —
        # a 2nd same-country tour got cache_hit and silently never got a row).
        logger.info("cache_hit", destination=effective_seed, seo_mode=seo_mode)
        seo_data = cached
        # AA-618 — record the cache hit (no DFS call, cost 0) so the admin DFS rollup has a real
        # per-call cache-hit rate, not just the Redis-global one. Fire-and-forget.
        await record_dfs_call(
            endpoint="fetch_all", cache_hit=True, cost_usd=0.0,
            tenant_id=tenant_id, tour_id=tour_id, keyword=effective_seed,
            location_code=location_code, meta={"seo_mode": seo_mode},
        )
    else:
        # "custom_keywords" — use existing seo_context from DB, skip DataForSEO API call
        if seo_mode == "custom_keywords":
            logger.info("seo_custom_keywords", tour_id=tour_id, destination=destination)
            conn = await asyncpg.connect(get_database_url())
            try:
                row = await conn.fetchrow(
                    """SELECT top_keywords, keyword_ideas
                       FROM silver_aa_internal.seo_context
                       WHERE tour_id = $1::uuid
                       ORDER BY fetched_at DESC LIMIT 1""",
                    tour_id,
                )
                if row:
                    # AA-235: post-backfill keyword_ideas is [] (legacy was a {kw:vol} map).
                    # Guard so custom_keywords mode keeps search_volumes a dict, not a list.
                    _ki = json.loads(row["keyword_ideas"] or "[]")
                    existing = {
                        "keywords": {
                            "top_keywords": json.loads(row["top_keywords"] or "[]"),
                            "search_volumes": _ki if isinstance(_ki, dict) else {},
                        }
                    }
                    try:
                        await cache.set(redis_seed_key, existing, ttl_seconds=_SEO_CACHE_TTL_SECONDS)
                    except Exception as _cache_err:
                        logger.warning("seo_cache_set_failed", error=str(_cache_err))
                    # Row already belongs to THIS tour_id (queried by tour_id, not
                    # shared by country) — no re-insert needed, unlike the cache_hit
                    # case above.
                    return {"status": "custom_keywords", "data": existing}
            finally:
                await conn.close()
            # No existing data — return empty rather than calling DataForSEO
            return {"status": "custom_keywords_empty", "data": {}}

        # "dataforseo" (default) — live keyword fetch
        logger.info("cache_miss", destination=effective_seed, seo_mode=seo_mode, location=location_name)
        # AA-618 — pass attribution so each live DFS HTTP call inside fetch_all logs a dfs_call_log
        # row with tenant_id/tour_id + real cost.
        if budget is None:
            budget = await _load_budget(budget_kind or DEFAULT_BUDGET_KIND)
        client = DataForSEOClient(tenant_id=tenant_id, tour_id=tour_id, budget=budget)
        seo_data = await client.fetch_all(
            effective_seed, location_code, location_name, language_code, activity,
            extra_seeds=extra_seeds, place_terms=place_terms, activity_words=activity_words,
            country=country,
        )
        stop = getattr(client, "last_stop", None)
        if isinstance(stop, dict) and stop:
            # AA-642 (S0): a partial result is never cached (7 days, shared by tours of the same seed) nor persisted
            # as this tour's seo_context row (S1 would reuse it for S1_SEO_REUSE_DAYS instead of buying again).
            logger.warning("seo_fetch_stopped", tour_id=tour_id, reason=stop.get("reason"), at=stop.get("at"))
            if stop.get("reason") == "budget":
                await _alert_stop(budget, tour_id, stop)
            status = "budget_stopped" if stop.get("reason") == "budget" else "dfs_stopped"
            return {"status": status, "data": seo_data, "stopped": stop}
        try:
            await cache.set(redis_seed_key, seo_data, ttl_seconds=_SEO_CACHE_TTL_SECONDS)
        except Exception as _cache_err:
            logger.warning("seo_cache_set_failed", error=str(_cache_err))

    # AA-249: persist runs for BOTH cache_hit and freshly-fetched data — every tour
    # gets its own seo_context row regardless of whether the Redis cache saved us
    # the DataForSEO call.
    # AA-235: shape guard at the writer — keyword_ideas MUST persist as a JSON array.
    # A dict/None (e.g. legacy search_volumes map or an empty DFS result) would store a
    # JSON object that crashes the FE [...spread] and the export-docx [:25] slice.
    _ideas = seo_data.get("keyword_ideas", [])
    if not isinstance(_ideas, list):
        _ideas = []

    conn = await asyncpg.connect(get_database_url())
    try:
        repo = SeoContextRepository(conn, tenant_slug="aa_internal")
        seo_id = await repo.insert({
            "tour_id":        tour_id,
            "keyword_search": effective_seed,
            # AA-203: persist the AA-197 top-level keyword_ideas list (full-metric:
            # volume/competition/cpc) — NOT keywords.search_volumes (an often-empty dict).
            "keyword_ideas":  json.dumps(_ideas, default=str),
            "demographics":   json.dumps(seo_data.get("demographics", {})),
            "trends":         json.dumps(seo_data.get("trends", {})),
            "top_keywords":   json.dumps(seo_data.get("keywords", {}).get("top_keywords", [])),
            # AA-218: DFS fetch_all returns these top-level lists — persist them
            # (real keys are people_also_ask / related_keywords, not "related").
            "people_also_ask":  json.dumps(seo_data.get("people_also_ask", []), default=str),
            "related_keywords": json.dumps(seo_data.get("related_keywords", []), default=str),
            # DB cache_key column: trace-only since migration 075 (row identity is
            # tour_id) — still populated so it's easy to see which seed produced a row.
            "cache_key":      redis_seed_key,
            "expires_at":     datetime.utcnow() + timedelta(seconds=_SEO_CACHE_TTL_SECONDS),
        })
        logger.info("seo_inserted", id=seo_id)
    finally:
        await conn.close()

    return {"status": "cache_hit" if cached else "fetched", "id": seo_id, "data": seo_data}
