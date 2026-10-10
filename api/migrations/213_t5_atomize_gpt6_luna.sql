-- Migration 213: AA-757 (S224) — A3 atomize (stage key `t5_atomize`, a pre-AA-526 name) moves from
-- Claude Haiku to GPT-6 Luna as a `validate` stage (ADR 0008: extraction the judge never scores),
-- with Haiku as the fallback. Needs atomize on the gateway (#642), already deployed.
--
-- Evidence: S224 offline A/B, 60 tours / 14 countries / 477 days — Luna 0 errors, 0 JSON failures,
-- no invented detail in 120 hand-labelled items, Jev a3_atom_in_text p<0.5 0.5% vs Haiku 1.5%,
-- $0.00069 vs ~$0.0042 per day call. Report on AA-757.
--
-- Atomize runs once per tour when it is published to Master (A3 platform only — tenants never
-- atomize). Existing atoms are NOT re-atomized: the day fingerprint includes the model, so a tour
-- re-atomizes on Luna only when it is next published or re-atomized from admin.
-- Rollback: role 'writer', provider 'claude', model 'haiku', fallback_model_ids '{}'.

BEGIN;

UPDATE shared.llm_role_config
   SET role = 'validate', provider = 'openai', model_id = 'gpt-6-luna', account_route = 'acc3',
       fallback_model_ids = ARRAY['haiku']::text[], updated_at = now(),
       updated_by = 'migration-213-aa757'
 WHERE stage = 't5_atomize';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('213', now(), 'AA-757: t5_atomize (A3 atomize) -> validate role on gpt-6-luna, haiku fallback')
ON CONFLICT (version) DO NOTHING;

COMMIT;
