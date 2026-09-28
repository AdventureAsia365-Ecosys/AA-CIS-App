-- Migration 171: AA-659 — switch the four judge stages from GPT-4.1 to GPT-5.6 Luna (Nghiệp, 28/09).
--
-- Apply ONLY after the AA-659 code is deployed (the old code calls s1_brand_audit's model with
-- the OpenAI API + temperature, which the Luna models may reject) and the live smoke test passed.
--
-- Route (ADR 0006):
--   gpt-5.6-luna (Bedrock acc3)  ->  gpt-6-luna (Bedrock acc3, skipped while disabled)
--                                ->  gpt-6-luna-openai (OpenAI API)
-- GPT-4.1 is no longer in the chain; it runs as the SHADOW on every call so the switch is
-- still A/B-checked (shared.llm_shadow_log).
--
-- Rollback:
--   UPDATE shared.llm_role_config SET model_id = 'gpt-4.1', fallback_model_ids = '{}',
--          shadow_model_id = NULL, shadow_sample_pct = 0, updated_at = now(),
--          updated_by = 'rollback-171'
--   WHERE stage IN ('s1_judge', 's1_brand_audit', 't10_judge', 'n7_judge');

BEGIN;

UPDATE shared.llm_role_config SET
    model_id = 'gpt-5.6-luna',
    fallback_model_ids = '{gpt-6-luna,gpt-6-luna-openai}',
    shadow_model_id = 'gpt-4.1',
    shadow_sample_pct = 100,
    updated_at = now(),
    updated_by = 'migration-171'
WHERE stage IN ('s1_judge', 's1_brand_audit', 't10_judge', 'n7_judge');

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('171', now(), 'AA-659: judge stages -> GPT-5.6 Luna route, GPT-4.1 as shadow')
ON CONFLICT (version) DO NOTHING;

COMMIT;
