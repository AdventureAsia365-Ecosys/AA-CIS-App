-- Migration 159: AA-627 — shared.dfs_balance_snapshot (DataForSEO account balance history)
--
-- Context (S189): the DataForSEO account (thu@adventure.asia) ran out of money on 19/09
-- (balance -$0.04) and EVERY SEO fetch started returning HTTP 402 — with nobody noticing until
-- the pipeline failed. AA-627 adds a once-a-day balance read (DFS `GET /appendix/user_data` is
-- FREE, cost:0) that stores the balance here and alerts when it drops below a threshold.
--
-- This table is the "remaining balance" layer — a DIFFERENT concern from shared.dfs_call_log
-- (AA-618), which records the cost we already SPENT per call. One row per balance read; the
-- daily scheduler appends, the External Spend page reads the latest via fetched_at DESC LIMIT 1.
--
-- balance_usd is the raw account balance from DFS money.balance (can be negative when the
-- account is overdrawn, so no CHECK >= 0). `raw` keeps the full money{} block for later audit.

CREATE TABLE IF NOT EXISTS shared.dfs_balance_snapshot (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    balance_usd  NUMERIC(14, 4) NOT NULL,   -- DFS money.balance (may be negative if overdrawn)
    currency     TEXT,                      -- DFS money.currency (e.g. 'USD')
    below_threshold BOOLEAN NOT NULL DEFAULT false,  -- balance < alert threshold at read time
    threshold_usd   NUMERIC(14, 4),         -- the threshold in effect when this row was written
    raw          JSONB,                     -- full DFS money{} block for audit
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dfs_balance_fetched ON shared.dfs_balance_snapshot(fetched_at DESC);

COMMENT ON TABLE shared.dfs_balance_snapshot IS
    'AA-627 — one row per DataForSEO account balance read (money.balance from the FREE '
    'appendix/user_data endpoint). The "remaining balance" layer, distinct from dfs_call_log '
    '(AA-618 = cost already spent). Written by the daily balance-check job; the External Spend '
    'page reads the latest via fetched_at DESC LIMIT 1.';
COMMENT ON COLUMN shared.dfs_balance_snapshot.balance_usd IS
    'AA-627 — raw DFS money.balance; can be negative when the account is overdrawn.';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('159', now(), 'AA-627: shared.dfs_balance_snapshot — DataForSEO account balance history + low-balance alert')
ON CONFLICT (version) DO NOTHING;
