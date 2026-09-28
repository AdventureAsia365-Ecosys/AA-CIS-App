-- Migration 168: AA-649 — shared.spend_budget (cost guard for DFS / LLM / Jev spend)
--
-- Context: on 25/09/2026 one research run spent $49 of DataForSEO in a few hours and drained the
-- balance mid-run. Nothing capped it; the AA-627 daily Lambda only reads the balance. This table
-- holds the limits the cost guard (shared/cost_guard.py) checks BEFORE every paid call.
--
-- One row per (provider, scope):
--   scope = 'global'           -> per_day_usd caps the provider's total spend per UTC day
--   scope = 'job:<kind>'       -> per_run_usd caps one run of that job kind (and an optional
--                                 per_day_usd for that kind); the effective daily cap is the
--                                 smaller of the job and global values.
-- NULL limit = no limit on that axis. hard_stop=false turns a breach into an alert only.
-- Day spend is read from shared.dfs_call_log / shared.llm_call_log (UTC day).

BEGIN;

CREATE TABLE IF NOT EXISTS shared.spend_budget (
    provider      TEXT NOT NULL CHECK (provider IN ('dfs', 'bedrock', 'openai', 'jev')),
    scope         TEXT NOT NULL DEFAULT 'global'
                  CHECK (scope = 'global' OR scope LIKE 'job:%'),
    per_run_usd   NUMERIC(10, 2) CHECK (per_run_usd IS NULL OR per_run_usd >= 0),
    per_day_usd   NUMERIC(10, 2) CHECK (per_day_usd IS NULL OR per_day_usd >= 0),
    hard_stop     BOOLEAN NOT NULL DEFAULT true,
    alert_pct     INTEGER NOT NULL DEFAULT 80 CHECK (alert_pct BETWEEN 1 AND 100),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT,
    PRIMARY KEY (provider, scope)
);

COMMENT ON TABLE shared.spend_budget IS
    'AA-649 — spend limits checked by shared/cost_guard.py before every paid DFS/LLM/Jev call. '
    '(provider, scope) where scope is ''global'' (daily cap) or ''job:<kind>'' (per-run cap). '
    'NULL = unlimited on that axis. Edited via PUT /admin/budgets/{provider}/{scope}.';

-- Conservative starting values (Nghiệp can change them in admin): DFS $10/day overall, one
-- research run at most $5 of DFS and $2 of Bedrock (the research loop's Haiku calls).
INSERT INTO shared.spend_budget (provider, scope, per_run_usd, per_day_usd, updated_by) VALUES
    ('dfs',     'global',                NULL, 10.00, 'migration-168'),
    ('dfs',     'job:segment_research',  5.00, NULL,  'migration-168'),
    ('bedrock', 'job:segment_research',  2.00, NULL,  'migration-168')
ON CONFLICT (provider, scope) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('168', now(),
    'AA-649: shared.spend_budget — per-run / per-day spend limits for the cost guard')
ON CONFLICT (version) DO NOTHING;

COMMIT;
