-- Migration 166: AA-631 (Debate stage) — acp_shared.debate_brand_fit_cache.
--
-- Context: propose_slate() runs on EVERY `GET /v1/slate` (not on a schedule). Debate's
-- brand-fit standard (AA-631 standard #4, "sounds like this operator") reuses the EXISTING
-- GPT-4.1 brand-fit judge (services/content_generation/brand_fit.py, AA-206) rather than a new
-- LLM mechanism -- but calling it uncached on every page view would repeat the exact
-- performance mistake AA-610 Sub 2 made before its scope fix (PR #420): recomputing real LLM
-- signal on every read instead of caching what hasn't changed.
--
-- Cache key: (subject_id, tenant_id, brand_version) -- Nghiệp confirmed (22/09/2026): a
-- candidate's brand-fit result is reused across every `GET /v1/slate` call for that tenant
-- UNLESS the tenant edits their Brand Identity (`shared.tenant_brand_rules.version` bumps) or
-- the candidate itself is new. `subject_id` here is NOT `acp_shared.subject.subject_id` (that
-- row does not exist until propose_slate() decides to propose it) -- it is the Segment or
-- Route's own id (`segment_id` OR `route_id`, exactly one non-null, same shape
-- `acp_shared.subject` itself already uses for the same distinction).

BEGIN;

CREATE TABLE IF NOT EXISTS acp_shared.debate_brand_fit_cache (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES shared.tenants(tenant_id),
    segment_id        TEXT,
    route_id          TEXT,
    brand_version     INT NOT NULL,
    judge_score       REAL NOT NULL,
    brand_fit_score   REAL NOT NULL,
    cross_brand_distinct REAL NOT NULL,
    mission_present   BOOLEAN NOT NULL,
    feedback          TEXT,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((segment_id IS NOT NULL) != (route_id IS NOT NULL))
);

-- Partial unique indexes mirror acp_shared.subject's own (tenant_id, segment_id)/(tenant_id,
-- route_id) pattern (migration 133) -- exactly one row per (tenant, candidate, brand_version).
CREATE UNIQUE INDEX IF NOT EXISTS idx_debate_brand_fit_segment
    ON acp_shared.debate_brand_fit_cache(tenant_id, segment_id, brand_version)
    WHERE segment_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_debate_brand_fit_route
    ON acp_shared.debate_brand_fit_cache(tenant_id, route_id, brand_version)
    WHERE route_id IS NOT NULL;

COMMENT ON TABLE acp_shared.debate_brand_fit_cache IS
    'AA-631 (Debate stage) -- cached brand-fit judge result per (tenant, Segment-or-Route, '
    'brand_version). propose_slate() reads this before calling brand_fit.score_brand_fit() -- '
    'a cache HIT for the tenant''s CURRENT brand_version means zero LLM calls. A row becomes '
    'stale (never deleted, just superseded) when tenant_brand_rules.version bumps -- '
    'propose_slate() looks up the CURRENT version only, so an old-version row is simply never '
    'matched again, not actively cleaned up (same "superseded, not deleted" precedent '
    'acp_contract.route already uses, migration 131/AA-532).';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('166', now(),
    'AA-631 (Debate stage): acp_shared.debate_brand_fit_cache -- cached brand-fit judge result '
    'keyed (tenant_id, segment_id_or_route_id, brand_version), so propose_slate() never '
    're-calls the GPT-4.1 judge for a candidate already scored under the tenant''s current '
    'brand_version')
ON CONFLICT (version) DO NOTHING;

COMMIT;
