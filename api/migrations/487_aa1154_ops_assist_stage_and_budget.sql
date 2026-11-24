-- Migration 487: AA-1154 (S292, phase A1 of the CIS admin assistant) — the AI gateway stage `ops_assist` and its own
-- monthly spend cap.
--
-- Why. Phase A0 (CIS 481, code PR 149c844c) turns a Needs-attention item of the Ecosystem status page into ranked action
-- cards by rule, with no model call. Phase A1 lets a model rank and explain those cards (and phrase the "what changed
-- in the last hour" look-up) through the existing AI gateway. Tech lead decisions S285 (root
-- docs/audits/2026-11-19-S285-session/advisory-ai-assistants.md sections 5, 6 and 9):
--   AI2  Claude Haiku 5.5 by default, Sonnet 5.5 only on "Deep analysis"; the Anthropic API first, then acc1 (the
--        route control's own provider order decides, nothing here names a connection);
--   AI3  a cap of 10 USD a month as its own spend row for the stage, hard stop, editable on AI gateway > Budgets;
--   AI4  the model has no tools: it only ranks the rule-made cards and explains them;
--   kill switch = shared.llm_role_config.is_active of the stage (no new setting).
-- The code (api/services/ops_assist/explain.py) makes no model call while either row below is missing: a stage with
-- no row would run on the gateway's defaults, and a stage with no hard-stop cap has no ceiling.
--
-- What:
--   1. One shared.llm_role_config row:
--        stage ops_assist, role writer (the generate seam: the call writes a ranking and a short explanation; the
--        ledger role `decide` is the Jev seam, ADR 0009 D3, and the row CHECK allows writer / judge / validate /
--        embed only), provider claude, model_id haiku-5-5 (the Converse key of migration 259; its deployments and their
--        order are the route control's), account_route NULL (the column is ignored for every value but acc3, AA-998),
--        no fallback model (the haiku-5-5 deployments already span two failure domains, the Anthropic API and Bedrock;
--        an admin adds a fallback on AI gateway > Routing), no shadow, allowed_scopes {aws_only, vendor_direct} (the
--        stage sends aggregated operations numbers, keys and error classes only, no personal data and no tenant
--        text, so the Anthropic API may serve it: AI2), route_deadline_s 25 (the call answers a click and must end
--        inside the API Gateway's 30 s), degrade_mode basic_narration (when the stage cannot run the page shows the
--        rule-made cards and Summary without the model's words), pii_class content, free_text false, active.
--   2. One shared.spend_budget row: provider llm, scope stage:ops_assist, per_month_usd 10.00, hard_stop true,
--      alert_pct 80, no day or run cap. shared/llm_client/central_budget.py refuses a call of the stage that would pass
--      the month's cap (0 USD ledger row, status refused, class budget); the bell at 80 %.
--   3. Register 487.
--
-- Idempotent: ON CONFLICT DO NOTHING keeps a row an admin already edited (a lowered cap, a switched-off stage); the
-- guard checks the rows exist with a hard-stop month cap.
--
-- Class: contract (root ADR 0017 d3: INSERTs into existing routing and budget tables), no directive line. MIG-4: this
-- migration-only PR ships first (Infra copy, then Infra Migration Apply once every job table is idle, ADR 0017a); the
-- A1 code PR follows. No readiness raise: the code reads both rows before every model call and answers with the
-- rule-made cards while they are missing. DM-7.1: no new table.
--
-- Number. Owner main max + 1 at the time of writing (CIS 484-486 taken by other lanes of S292). If it differs at merge,
-- renumber: git grep '487_' and "'487'" (this file, tests/integration/test_aa1154_migration_487.py,
-- api/services/ops_assist/explain.py, docs/implementation-notes/AA-1154.md).
--
-- Locks. Two single-row INSERTs. lock_timeout 5s.
--
-- Rollback (a new forward migration, after the A1 code PR is reverted):
--   DELETE FROM shared.spend_budget WHERE provider IN ('llm') AND scope IN ('stage:ops_assist');
--   DELETE FROM shared.llm_role_config WHERE stage IN ('ops_assist');
--
-- X1. One BEGIN … COMMIT block, no role switch (Migration Apply runs cis/ files as cis_owner).

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    col text;
BEGIN
    FOREACH col IN ARRAY ARRAY['allowed_scopes', 'route_deadline_s', 'degrade_mode', 'pii_class', 'free_text'] LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'shared'
                          AND table_name = 'llm_role_config' AND column_name = col) THEN
            RAISE EXCEPTION 'migration 487: shared.llm_role_config.% is missing (apply 311, 320, 326, 373, 451 first)',
                col;
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'shared'
                      AND table_name = 'spend_budget' AND column_name = 'per_month_usd') THEN
        RAISE EXCEPTION 'migration 487: shared.spend_budget.per_month_usd is missing (apply 340 first)';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shared.llm_model_catalog WHERE model_key = 'haiku-5-5' AND enabled) THEN
        RAISE EXCEPTION 'migration 487: haiku-5-5 is not an enabled catalog model (migration 259)';
    END IF;
END $$;

INSERT INTO shared.llm_role_config
    (stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id,
     shadow_sample_pct, allowed_scopes, route_deadline_s, degrade_mode, pii_class, free_text, updated_by)
VALUES
    ('ops_assist', 'writer', 'claude', 'haiku-5-5', NULL, true, ARRAY[]::text[], NULL, 0,
     ARRAY['aws_only', 'vendor_direct']::text[], 25, 'basic_narration', 'content', false, 'migration-487-aa1154')
ON CONFLICT (stage) DO NOTHING;

INSERT INTO shared.spend_budget
    (provider, scope, per_run_usd, per_day_usd, per_month_usd, hard_stop, alert_pct, updated_by)
VALUES
    ('llm', 'stage:ops_assist', NULL, NULL, 10.00, true, 80, 'migration-487-aa1154')
ON CONFLICT (provider, scope) DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM shared.llm_role_config WHERE stage = 'ops_assist' AND role = 'writer') THEN
        RAISE EXCEPTION 'migration 487: the ops_assist stage row is missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shared.spend_budget WHERE provider = 'llm' AND scope = 'stage:ops_assist'
                      AND per_month_usd IS NOT NULL) THEN
        RAISE EXCEPTION 'migration 487: the stage:ops_assist month cap is missing';
    END IF;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('487', now(), 'AA-1154 A1: AI gateway stage ops_assist (writer, haiku-5-5, deadline 25 s) and its own month '
                      'cap llm / stage:ops_assist 10 USD hard stop')
ON CONFLICT (version) DO NOTHING;

COMMIT;
