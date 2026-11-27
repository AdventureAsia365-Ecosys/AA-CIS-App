-- break-glass: retire t10_claims (tech lead S294 D6): never wired into a gate, a 0-diff replay, no W2 record
-- Migration 495: AA-1050 (S294 D6, carry-over of the dormant claim layers) — retire the stage route t10_claims, the
-- F9 v2 claim extraction that no production code ever called.
--
-- Why. D6: a dormant claim layer whose $0 replay shows 0 changed decisions and that cannot pass W2 is removed. The
-- t10_claims route had one sender, the contract smoke (no object ids); W2 (AA-1048) measured the pinned R1 instrument,
-- never this prompt. A route nobody calls still answers the day a stray caller comes back, and still counts in the
-- failure-domain report and the AI Use Register. The code PR of this change drops the smoke case; the prompt text
-- (claim_rules.EXTRACTION_SYSTEM) stays as the history of the registered versions v1-v3.
--
-- What. The row is switched off with the stage kill switch of migration 326 (is_active = false: the gateway refuses a
-- call before any provider attempt, 0 USD ledger row 'disabled') and disabled_reason says why. Nothing is deleted:
-- the row, its chain, the shared.prompt_version rows and every ledger row stay; the admin switch
-- (PATCH /admin/llm-config/{stage}/switch, audited) turns it back on.
--
-- Guards (RAISE rolls the whole file back):
--   before: 326 registered (is_active = false means "switched off" only since then).
--   traffic: no shared.llm_call_log row of the stage in the last 30 days tied to an object (content_piece_id,
--            angle_gate_request_id or tour_id set): such a row would be a decision path, and the replay said none.
--            A contract-smoke row carries none of them and is allowed. Uses idx_llm_call_log_stage_created.
--   after: the row, if it exists, is inactive with a reason; every other row keeps its is_active.
-- A missing row is reported with a NOTICE (nothing to retire), never an error.
--
-- Class: contract (root ADR 0017 d3: an UPDATE of an existing routing row), no migrate directive. MIG-4: this
-- migration-only PR, the byte copy in AA-CIS-Infra db/migrations/cis/, Infra Migration Apply once every job table is
-- idle. No code needs it. DM-7.1: no new table. Writing path: the route row of a t10_ stage is part of the T9 writers'
-- release fingerprint (config_fp routes), so the active T9 releases get a reverify flag (model_route) after the apply;
-- they keep serving.
--
-- Rollback (a NEW forward migration, or the admin switch):
--   UPDATE shared.llm_role_config SET is_active = true, disabled_reason = NULL WHERE stage = 't10_claims';
--
-- X1. One BEGIN … COMMIT block, no role switch (Migration Apply runs cis/ files as cis_owner). Locks: one UPDATE.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

DO $$
DECLARE
    retired constant text := 't10_claims';
    reason  constant text := 'Retired (AA-1050, S294 D6): never wired into a gate, 0 changed decisions in the $0 '
                             'replay, no W2 record. Kept for history; switch it on again only for a new caller.';
    others_before jsonb;
    others_after  jsonb;
    traffic text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM shared.schema_versions WHERE version = '326') THEN
        RAISE EXCEPTION 'migration 495: apply 326 first (is_active = false must mean "switched off")';
    END IF;

    SELECT count(*) || ' row(s), last ' || max(created_at) INTO traffic
      FROM shared.llm_call_log
     WHERE stage = retired AND created_at > now() - interval '30 days'
       AND (content_piece_id IS NOT NULL OR angle_gate_request_id IS NOT NULL OR tour_id IS NOT NULL)
    HAVING count(*) > 0;
    IF traffic IS NOT NULL THEN
        RAISE EXCEPTION 'migration 495: t10_claims has object-tied ledger traffic in the last 30 days (%): find the '
                        'caller first', traffic;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM shared.llm_role_config WHERE stage = retired) THEN
        RAISE NOTICE 'migration 495: no row to retire for t10_claims';
    END IF;

    SELECT coalesce(jsonb_object_agg(stage, is_active), '{}'::jsonb) INTO others_before
      FROM shared.llm_role_config WHERE stage <> retired;

    UPDATE shared.llm_role_config
       SET is_active = false, disabled_reason = reason, updated_at = now(), updated_by = 'migration-495-aa1050'
     WHERE stage = retired AND (is_active OR disabled_reason IS NULL);

    IF EXISTS (SELECT 1 FROM shared.llm_role_config
                WHERE stage = retired AND (is_active OR disabled_reason IS NULL)) THEN
        RAISE EXCEPTION 'migration 495: t10_claims is still active or has no reason';
    END IF;
    SELECT coalesce(jsonb_object_agg(stage, is_active), '{}'::jsonb) INTO others_after
      FROM shared.llm_role_config WHERE stage <> retired;
    IF others_after IS DISTINCT FROM others_before THEN
        RAISE EXCEPTION 'migration 495: another stage changed its switch';
    END IF;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('495', now(), 'AA-1050 S294 D6: retire the t10_claims route (switched off with a reason, history kept)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
