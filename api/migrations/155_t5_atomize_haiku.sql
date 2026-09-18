-- Migration 155: AA-619 (epic AA-616) — switch t5_atomize from Sonnet to Haiku
--
-- Evidence (S187, Kiro — A/B measured live in the ECS api container, read-only): atomize is a
-- verbatim structured-extraction task (place/action pairs to a fixed JSON schema, no judge, no
-- retry). Haiku vs Sonnet on the same real tours produced NEAR-IDENTICAL atom counts (27 vs 28,
-- 32 vs 32, 28 vs 29), clean JSON parse both, and Haiku's place/action were correctly grounded
-- (no fabrication). Sonnet cost ~4x more. t5_atomize was the single biggest acc3 cost driver
-- ($19.22 / last-30d = ~82% of acc3 spend). Sonnet here was a hardcoded default (migration 137
-- froze the pre-existing `_T5_MODEL_TIER="sonnet"`), never a quality decision — full record in
-- Linear AA-619.
--
-- Admin can still flip it back via /admin/llm-config (Settings > LLM Models) — this only changes
-- the seeded default. The day-fingerprint (acp_contract.atomize_day_fingerprint) is keyed on the
-- live config model as of AA-619, so this change correctly invalidates every day's fingerprint:
-- on the next atomize/rerun each day re-runs with Haiku instead of silently keeping Sonnet atoms.

BEGIN;

UPDATE shared.llm_role_config
   SET model_id   = 'haiku',
       updated_by = 'system-migration-aa619',
       updated_at = now()
 WHERE stage = 't5_atomize'
   AND model_id = 'sonnet';   -- idempotent: no-op if an admin already changed it

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('155', now(), 'AA-619: t5_atomize Sonnet->Haiku (A/B proved equal atom quality, ~4x cheaper)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
