import asyncio
import asyncpg
import json
import os
import redis.asyncio as _aioredis
import structlog
from datetime import datetime, timedelta
from .dataforseo_client import (
    DataForSEOClient,
    DEFAULT_LOCATION_CODE, DEFAULT_LOCATION_NAME, DEFAULT_LANGUAGE_CODE,
)
from .seed_builder import resolve_buyer_market
from shared.secrets import get_database_url
from shared.dfs_client.call_log import record_dfs_call
from shared.services.tenant_config_service import TenantConfigService
from shared.repository.seo_context_repository import SeoContextRepository
from shared.cache.redis_cache import RedisCache

logger = structlog.get_logger()

# AA-249: module-level singleton, built once at import — shared by every
# process_seo() call regardless of caller (HTTP route or the background 3-worker
# job queue, AA-223, neither of which guarantees a Request/app.state in scope).
# redis.asyncio.from_url() is lazy — it does no I/O here, just builds a client
# backed by its own internal connection pool; real connections open on first
# command and are managed/reconnected by the library for the life of the worker
# process. No explicit shutdown hook: the socket closes with the process, and a
# single long-lived client isn't a meaningful leak to guard against.
_REDIS_HOST = os.environ.get("REDIS_HOST", "aa-cis-dev-redis.wvp8vb.0001.usw1.cache.amazonaws.com")
_seo_redis_client = _aioredis.from_url(f"redis://{_REDIS_HOST}:6379", encoding="utf-8", decode_responses=True)

# AA-625: SEO cache TTL raised 24h -> 7 days. Search volume / SERP for travel keywords change on a
# weekly/monthly cadence, not daily, so a 24h TTL forced a fresh (billable) DataForSEO fetch for the
# same (seed, market) whenever a rerun spanned more than a day. 7 days keeps cache HITs across a
# multi-day rerun while still refreshing often enough for demand to stay current. Single source of
# truth for both the Redis TTL and the seo_context.expires_at trace column below.
_SEO_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 604800


async def process_seo(
    tour_id: str,
    destination: str,
    activity: str = None,
    cache=None,
    seo_mode: str = "dataforseo",
    seed: str = "",
    tenant_id: str = None,
) -> dict:
    # "disabled" — skip SEO step entirely
    if seo_mode == "disabled":
        logger.info("seo_disabled", tour_id=tour_id)
        return {"status": "disabled", "data": {}}

    # AA-197: caller passes a pre-built seed; fall back to destination for old callers.
    effective_seed = seed or destination

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
        client = DataForSEOClient(tenant_id=tenant_id, tour_id=tour_id)
        seo_data = await client.fetch_all(
            effective_seed, location_code, location_name, language_code, activity,
        )
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


async def _lookup_destination(tour_id: str) -> str:
    """Lookup country/name from DB when destination not in SF payload."""
    conn = await asyncpg.connect(get_database_url())
    try:
        row = await conn.fetchrow(
            "SELECT country, src_name FROM silver_aa_internal.raw_tours "
            "WHERE tour_id = $1::uuid",
            tour_id
        )
        if row:
            return row["country"] or row["src_name"] or ""
        return ""
    finally:
        await conn.close()


def lambda_handler(event: dict, context) -> dict:
    results = []

    # Pattern 1: SF direct invoke
    if "destination" in event:
        records = [event]
    # Pattern 2: SQS trigger (Phase 2)
    elif "Records" in event:
        records = [json.loads(r["body"]) for r in event["Records"]]
    elif "tour_id" in event:
        # SF invoke without destination — lookup from DB
        records = [event]
    else:
        logger.warning("unknown_event_format", keys=list(event.keys()))
        return {"processed": 0, "results": []}

    for body in records:
        try:
            tour_id     = body.get("tour_id", "unknown")
            destination = body.get("destination")
            activity    = body.get("activity")
            seo_mode    = body.get("seo_mode", "dataforseo")

            # Lookup destination from DB if not provided
            if not destination and tour_id != "unknown":
                try:
                    destination = asyncio.run(_lookup_destination(tour_id))
                except Exception as _e:
                    logger.warning("destination_lookup_failed", error=str(_e))

            if not destination and seo_mode != "disabled":
                logger.warning("missing_destination", tour_id=tour_id)
                continue

            result = asyncio.run(process_seo(tour_id, destination or "", activity, seo_mode=seo_mode))
            results.append({"destination": destination, **result})
        except Exception as e:
            logger.error("seo_failed", error=str(e))
            results.append({"status": "failed", "error": str(e)})

    return {"processed": len(results), "results": results}
