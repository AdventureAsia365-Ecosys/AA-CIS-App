-- Migration 172: AA-685 — stages for the model calls that bypassed the gateway, plus an `embed`
-- role for embedding stages.
--
-- Additive: the code deployed before AA-685 never reads these stages, so this is applied BEFORE
-- the AA-685 deploy.
--
-- New stages (every value matches what each call site did before AA-685, so the move itself
-- changes no model):
--   a0_column_map    shared/llm_client/column_mapper.py — Excel column auto-detect (A0), Haiku.
--   f10_embed        services/acp_shared/content_embedding.py — Cohere Embed v4 on acc2.
--   tp_compose       AA-TripPlanner-Web assembly Lambda — Sonnet 4.6 via acc3 (acc1 fallback).
--   tp_search_embed  AA-TripPlanner-Web browse Lambda — Cohere Embed v4 on acc2, one per search.
-- The TripPlanner rows are seeded here because this repo owns the `shared` schema; the
-- TripPlanner code that reads them ships in its own PR.

BEGIN;

-- Inline CHECKs from migration 137 carry generated names, so find them by definition.
DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT conrelid::regclass AS tbl, conname
        FROM pg_constraint
        WHERE contype = 'c'
          AND conrelid IN ('shared.llm_role_config'::regclass, 'shared.llm_call_log'::regclass)
          AND (pg_get_constraintdef(oid) LIKE '%role%writer%'
               OR pg_get_constraintdef(oid) LIKE '%provider%claude%')
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.tbl, c.conname);
    END LOOP;
END $$;

ALTER TABLE shared.llm_role_config
    ADD CONSTRAINT llm_role_config_role_check
        CHECK (role IN ('writer', 'judge', 'validate', 'embed')),
    ADD CONSTRAINT llm_role_config_provider_check
        CHECK (provider IN ('claude', 'openai', 'cohere'));

ALTER TABLE shared.llm_call_log
    ADD CONSTRAINT llm_call_log_role_check
        CHECK (role IN ('writer', 'judge', 'validate', 'embed'));

INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, updated_by) VALUES
    ('a0_column_map',   'writer', 'claude', 'haiku',           'acc3', 'migration-172'),
    ('f10_embed',       'embed',  'cohere', 'cohere-embed-v4', NULL,   'migration-172'),
    ('tp_compose',      'writer', 'claude', 'sonnet',          'acc3', 'migration-172'),
    ('tp_search_embed', 'embed',  'cohere', 'cohere-embed-v4', NULL,   'migration-172')
ON CONFLICT (stage) DO NOTHING;

UPDATE shared.llm_model_catalog SET
    notes = 'Called through the gateway embed path (stages f10_embed, tp_search_embed; AA-685).',
    updated_at = now(), updated_by = 'migration-172'
WHERE model_key = 'cohere-embed-v4';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('172', now(),
    'AA-685: embed role + stages a0_column_map, f10_embed, tp_compose, tp_search_embed')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- Rollback (only if no row uses the new values yet):
--   DELETE FROM shared.llm_role_config
--     WHERE stage IN ('a0_column_map', 'f10_embed', 'tp_compose', 'tp_search_embed');
--   then restore the CHECKs without 'embed' / 'cohere'.
