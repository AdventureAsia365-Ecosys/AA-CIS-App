-- Migration 164: AA-629 — shared.unmapped_market_requests (Tier 1 detect+record + Tier 2 admin
-- visibility for tenants whose target_market.countries has zero overlap with DFS_LOCATION_MAP).
--
-- Context: resolve_buyer_market()/resolve_buyer_markets() (services/seo_intelligence/
-- seed_builder.py) silently fell back to US market data whenever a tenant's declared countries
-- were ALL outside the 6-entry DFS_LOCATION_MAP whitelist — no error, no warning, tenant reads
-- someone else's market's ranking data with no signal anything is wrong. This already happened
-- for real (tenant exploreasia-co, before AA-515 expanded the whitelist to cover it).
--
-- Tier 1 (this table): every time resolve_buyer_market()/resolve_buyer_markets() detects a
-- non-empty, all-unmatched countries list, one row per (tenant_id, country_code) is
-- upserted here (resolved_at NULL = still waiting). Tier 2 (admin UI): an admin endpoint groups
-- unresolved rows by country_code so admin can see "N tenants waiting on market X" without
-- manual DB digging. Tier 3 (human-approved activation — add the market to DFS_LOCATION_MAP,
-- buy DFS data for it) stays a manual admin decision, no table/code for it here.

CREATE TABLE IF NOT EXISTS shared.unmapped_market_requests (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL,
    country_code  TEXT NOT NULL,          -- tenant's own 2-letter code, NOT in DFS_LOCATION_MAP
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- last time this pair was detected
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- first time this pair was ever detected
    resolved_at   TIMESTAMPTZ,            -- set when admin adds the market (Tier 3) or dismisses
    UNIQUE (tenant_id, country_code)
);

CREATE INDEX IF NOT EXISTS idx_unmapped_market_unresolved
    ON shared.unmapped_market_requests(country_code)
    WHERE resolved_at IS NULL;

COMMENT ON TABLE shared.unmapped_market_requests IS
    'AA-629 Tier 1/2 — one row per (tenant_id, country_code) a tenant declared in '
    'target_market.countries that has no match in seed_builder.DFS_LOCATION_MAP. Upserted '
    '(requested_at bumped) every time resolve_buyer_market()/resolve_buyer_markets() detects it '
    'again; resolved_at is set manually by an admin once the market is added (Tier 3) or the '
    'request is dismissed.';
COMMENT ON COLUMN shared.unmapped_market_requests.country_code IS
    'AA-629 — the tenant''s own declared code (target_market.countries), not a DFS location '
    'code — this is exactly the value DFS_LOCATION_MAP does not recognize.';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('164', now(), 'AA-629: shared.unmapped_market_requests — detect+record tenant markets outside DFS_LOCATION_MAP whitelist')
ON CONFLICT (version) DO NOTHING;
