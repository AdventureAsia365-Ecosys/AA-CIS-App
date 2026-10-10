-- Migration 212: AA-748 round 2 (S224) — s1_source_facts becomes a `validate` stage on GPT-6 Luna.
--
-- ADR 0008 (AA-CIS-App docs/adr): the writer/judge vendor rule (ADR-2026-014/027, ADR 0006 §5)
-- applies to writers and judges; a `validate` stage — extraction or checks the judge never scores —
-- may use any vendor. s1_source_facts only extracts figures from the source for the writer prompt
-- (and every figure passes a deterministic source check), so it moves from Haiku ($1/$5 per Mtok)
-- to GPT-6 Luna ($0.10/$0.50), with Haiku as the fallback. Data-only change; rollback = the
-- previous row (role writer, provider claude, model haiku, no fallbacks).

BEGIN;

UPDATE shared.llm_role_config
   SET role = 'validate', provider = 'openai', model_id = 'gpt-6-luna', account_route = 'acc3',
       fallback_model_ids = ARRAY['haiku']::text[], updated_at = now(),
       updated_by = 'migration-212-aa748'
 WHERE stage = 's1_source_facts';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('212', now(), 'AA-748: s1_source_facts -> validate role on gpt-6-luna (ADR 0008), haiku fallback')
ON CONFLICT (version) DO NOTHING;

COMMIT;
