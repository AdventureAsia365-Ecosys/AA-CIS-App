-- Migration 154: AA-618 (epic AA-616) — DataForSEO (DFS) usage/cost logging
--
-- Problem (S187, Kiro): DFS spend is INVISIBLE. DataForSEO is a 3rd-party API (not AWS) so it
-- never shows in Cost Explorer, and the code today discards the `cost` field DFS returns on every
-- response (dataforseo_client.py parsers stop at data["tasks"][0]["result"], never read
-- data["cost"]). There is no DFS usage table, no per-call cost, no real DFS cache-hit rate — the
-- dashboard "Cache Hit Rate" is Redis's GLOBAL keyspace hit rate (all keys), not DFS-attributable.
--
-- This adds shared.dfs_call_log, analogous to shared.llm_call_log (migration 137): one row per
-- DFS interaction — either a real live HTTP call (fetched_live=true, real cost_usd from the DFS
-- response) or a cache hit that skipped the call (cache_hit=true, cost_usd=0). Written fire-and-
-- forget by shared/dfs_client/dfs_call_log.py (mirrors call_log.py's own-connection/pool pattern),
-- so a logging failure never breaks an SEO fetch.
--
-- No FK on tenant_id/tour_id (nullable): segment_research is platform-wide (no tour_id, tenant
-- unused post-AA-545); a log row must never fail to insert because a parent row was concurrently
-- deleted — same reasoning as llm_call_log's content_piece_id (migration 137 header item 3).

CREATE TABLE IF NOT EXISTS shared.dfs_call_log (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID,            -- nullable, no FK: NULL for platform-wide segment_research
    tour_id        UUID,            -- nullable: present for process_seo (A1/T2), NULL elsewhere
    endpoint       TEXT NOT NULL,   -- 'search_volume' | 'search_volume_bulk' | 'serp_advanced' |
                                    -- 'keywords_for_keywords' | 'on_page' ... (the DFS operation)
    keyword        TEXT,            -- the seed/keyword/place asked (may be NULL for bulk)
    location_code  INTEGER,         -- DFS buyer-market location code (2840=US, etc.)
    cost_usd       NUMERIC(12, 6),  -- real cost from the DFS response `cost` field; 0 for cache_hit
    cache_hit      BOOLEAN NOT NULL DEFAULT false,   -- true = served from Redis/DB cache, no DFS call
    fetched_live   BOOLEAN NOT NULL DEFAULT false,   -- true = a real DFS HTTP call was made
    keyword_count  INTEGER,         -- how many keywords this (bulk) call covered, for cost/kw math
    meta           JSONB,           -- optional: market, language_code, seo_mode, any extra context
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dfs_call_log_created       ON shared.dfs_call_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dfs_call_log_tenant_created ON shared.dfs_call_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dfs_call_log_endpoint       ON shared.dfs_call_log(endpoint, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dfs_call_log_live           ON shared.dfs_call_log(fetched_live) WHERE fetched_live;

COMMENT ON TABLE shared.dfs_call_log IS
    'AA-618 — one row per DataForSEO interaction (live HTTP call OR cache hit). The ONLY source '
    'of real DFS cost + real per-call cache-hit rate (Cost Explorer cannot see 3rd-party DFS; the '
    'dashboard cache hit rate is Redis-global, not DFS). Written by shared/dfs_client/dfs_call_log.py.';
COMMENT ON COLUMN shared.dfs_call_log.cost_usd IS
    'AA-618 — real cost from the DFS response top-level `cost` field (per request). 0 on cache_hit.';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('154', now(), 'AA-618: shared.dfs_call_log — DataForSEO per-call usage/cost/cache-hit tracking')
ON CONFLICT (version) DO NOTHING;
