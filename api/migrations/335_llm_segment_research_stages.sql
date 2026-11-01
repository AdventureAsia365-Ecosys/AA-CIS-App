-- Migration 335: AA-1024 (AI gateway v2, root plan S263 slice A2, gaps CIS-7 / RW-11 part 4) — Model Gateway stages
-- for the demand research (segment research) job.
--
-- Why. Root ADR 0009 decision 5: `model_tier` is accepted only in an eval or probe context. The demand-research job
-- (job kind segment_research, AA-515 / AA-648) asks the gateway with model_tier="haiku" and no stage, so it has no
-- route, no data scope, no deadline, no kill switch and no degrade mode, and its ledger rows are written under the
-- name a3_search_demand by the caller. The A2 code lane gives its two calls a stage and refuses model_tier outside
-- eval / probe; this file creates the two stage rows first, so the code PR finds them.
--
-- What. Two shared.llm_role_config rows (the per-call service names in brackets):
--
--   stage                     call site                                              prompt
--   segment_research          services/acp_contract/segment_research.py (the loop:   SYSTEM_PROMPT / USER_PROMPT
--                             one keyword step per call, max_tokens 1024)
--   segment_research_batch    services/acp_contract/segment_research_batch.py (the   PROPOSE_SYSTEM
--                             batch proposer, max_tokens 4096)
--
--   Both: role writer (they phrase keywords; the caller logs role writer today), provider claude, model_id 'haiku'
--   (the legacy key the calls use today, so the provider calls do not change: the legacy chain, or the haiku alias
--   when LLM_LEGACY_ALIASES is on; S11 moves them with the other legacy stages), account_route acc3, no fallback, no
--   shadow (shadow_sample_pct 0), allowed_scopes {aws_only}, route_deadline_s 1200 (the writer value of 334),
--   degrade_mode 'queue_retry' (the job is re-queued when the stage cannot run: tech lead T-21's CIS mode), active.
--   SAFE_DEFAULTS (shared/llm_client/role_config.py) mirrors the rows in the code PR.
--
-- Idempotent: ON CONFLICT (stage) DO NOTHING keeps a row an admin already edited; the guard checks the rows exist as
-- writer stages. Prompt register: the stages' prompt_version rows come with the A2 code PR (prompt_versions.json and
-- its migration); until then the ledger rows carry prompt_version NULL, as today under a3_search_demand.
--
-- Class: contract (root ADR 0017 d3: an INSERT into an existing routing table), no expand header. MIG-4: this
-- migration-only PR merges after 334 (number order), the byte copy in AA-CIS-Infra db/migrations/cis/ merges, then DB
-- Migrate check / apply with the job tables idle. No code reads the rows before the A2 code PR. DM-7.1: no new table.
--
-- Rollback (a NEW forward migration, after the A2 code PR is reverted):
--   DELETE FROM shared.llm_role_config WHERE stage IN ('segment_research', 'segment_research_batch');
--
-- X1. One BEGIN … COMMIT block, no role switch (DB Migrate runs cis/ files as cis_owner). Locks: one INSERT.

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    col text;
BEGIN
    FOREACH col IN ARRAY ARRAY['allowed_scopes', 'route_deadline_s', 'degrade_mode'] LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'shared'
                          AND table_name = 'llm_role_config' AND column_name = col) THEN
            RAISE EXCEPTION 'migration 335: shared.llm_role_config.% is missing (apply 311, 320 and 326 first)', col;
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM shared.llm_model_catalog WHERE model_key = 'haiku') THEN
        RAISE EXCEPTION 'migration 335: shared.llm_model_catalog has no haiku row (migration 169)';
    END IF;
END $$;

INSERT INTO shared.llm_role_config
    (stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id,
     shadow_sample_pct, allowed_scopes, route_deadline_s, degrade_mode, updated_by)
VALUES
    ('segment_research',       'writer', 'claude', 'haiku', 'acc3', true, ARRAY[]::text[], NULL, 0,
     ARRAY['aws_only']::text[], 1200, 'queue_retry', 'migration-335-aa1024'),
    ('segment_research_batch', 'writer', 'claude', 'haiku', 'acc3', true, ARRAY[]::text[], NULL, 0,
     ARRAY['aws_only']::text[], 1200, 'queue_retry', 'migration-335-aa1024')
ON CONFLICT (stage) DO NOTHING;

DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n FROM shared.llm_role_config
     WHERE stage IN ('segment_research', 'segment_research_batch') AND role = 'writer'
       AND route_deadline_s IS NOT NULL;
    IF n <> 2 THEN
        RAISE EXCEPTION 'migration 335: expected the two segment research writer stages with a deadline, found %', n;
    END IF;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('335', now(), 'AA-1024 A2: Model Gateway stages segment_research and segment_research_batch (writer, legacy '
                      'haiku as today, aws_only, deadline 1200 s, degrade queue_retry)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
