-- Migration 214: AA-757 (S224) — rename the LLM stage key `t5_atomize` -> `a3_atomize`.
--
-- Atomize runs ONLY at A3 (platform), once per tour when it is published to Master (or on an
-- admin re-run); tenants never atomize. The stage key still said "T5" (a pre-AA-526, pre-AA-526
-- tenant-facing name) and misled S224. This renames the live config row only — no behaviour
-- change (migration 213 already set role/provider/model/fallback for this stage).
--
-- Historical `shared.llm_call_log` rows keep `stage='t5_atomize'` (they are an immutable audit of
-- what actually ran); they are NOT rewritten. Any UI/report that groups or filters by this stage
-- treats `t5_atomize` and `a3_atomize` as the same stage, labelled "A3 atomize".
--
-- Applied by Claude (not CI/CD), via ECS exec into the api container (RDS is private).
-- Rollback: UPDATE shared.llm_role_config SET stage = 't5_atomize' WHERE stage = 'a3_atomize';

BEGIN;

UPDATE shared.llm_role_config
   SET stage = 'a3_atomize', updated_at = now(), updated_by = 'migration-214-aa757'
 WHERE stage = 't5_atomize';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('214', now(), 'AA-757: rename LLM stage key t5_atomize -> a3_atomize (A3 atomize; config only)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
