-- Migration 167: AA-623 — shared.cost_explorer_snapshot (AWS actual spend, per account)
--
-- Context: AA-616/AA-622's External Spend Overview shows ESTIMATED cost (token counts x
-- pricing tables in shared/llm_client/pricing.py). AA-623 adds the AWS-reported ACTUAL spend
-- next to it, read from Cost Explorer's ce:GetCostAndUsage, grouped by LINKED_ACCOUNT + SERVICE.
--
-- AWS Cost Explorer pricing itself costs ~$0.01/request, so this is cached (once/day via the
-- same job-then-read split as shared.dfs_balance_snapshot, AA-627/migration 159) rather than
-- called live per page view.
--
-- acc2 (005097885195) is a member account, not the AWS Organizations payer, so there is no
-- single consolidated CE call — the app fetches each account's own costs separately (acc2
-- direct, acc1/acc3 via STS AssumeRole into their satellite CostExplorer-Reader roles — mirrors
-- shared/llm_client/bedrock_satellite.py's AssumeRole pattern, see AA-CIS-Infra PR #69). One
-- row per (account_id, service, period) per fetch, so a single "check" run inserts multiple rows.

BEGIN;

CREATE TABLE IF NOT EXISTS shared.cost_explorer_snapshot (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     TEXT NOT NULL,          -- AWS account ID (e.g. '005097885195')
    service        TEXT NOT NULL,          -- CE SERVICE dimension (e.g. 'Amazon Elastic Compute Cloud')
    period_start   DATE NOT NULL,          -- CE UsageStartDate for this row's period
    period_end     DATE NOT NULL,          -- CE UsageEndDate for this row's period
    amount_usd     NUMERIC(14, 4) NOT NULL,
    unit           TEXT NOT NULL DEFAULT 'USD',
    raw            JSONB,                  -- full CE ResultsByTime group entry, for audit
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cost_explorer_fetched ON shared.cost_explorer_snapshot(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_cost_explorer_account_period
    ON shared.cost_explorer_snapshot(account_id, period_start DESC);

COMMENT ON TABLE shared.cost_explorer_snapshot IS
    'AA-623 — one row per (account_id, service, period) AWS Cost Explorer read. Written by the '
    'daily cost-explorer-check job (POST /admin/cost-explorer/check); the External Spend page '
    'reads the latest fetch via GET /admin/cost-explorer (fetched_at DESC, most recent batch).';
COMMENT ON COLUMN shared.cost_explorer_snapshot.account_id IS
    'AWS account ID this row''s cost was fetched from directly (not necessarily the payer -- '
    'AA-623 found acc2/005097885195 is a member account, so each of acc1/acc2/acc3 is fetched '
    'separately, acc1/acc3 via STS AssumeRole satellite roles).';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('167', now(),
    'AA-623: shared.cost_explorer_snapshot — AWS actual spend per account/service/period, '
    'cached daily (Cost Explorer pricing API costs ~$0.01/request)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
