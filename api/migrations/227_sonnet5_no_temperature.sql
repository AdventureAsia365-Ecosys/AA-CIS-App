-- Migration 227: AA-801 — Claude Sonnet 5 takes no `temperature`.
--
-- S236 AA-801 round 2 on Dev: every sonnet-5 judge call (modes model:sonnet-5 and
-- anchors+model:sonnet-5, 90/90) failed with
--   ValidationException ... temperature is deprecated for this model
-- because migration 169 seeded the sonnet-5 catalog row with supports_temperature = true, and
-- LLMClient._call_bedrock_converse sends inferenceConfig.temperature whenever the row says it is
-- supported and the caller set one (the brand-fit judge always sets 0.1).
--
-- With the flag false the gateway leaves temperature out for sonnet-5 (same as the Luna rows), and
-- brand_fit._seeded_model treats it as a non-seeded model (the AA-714 near-band median applies).
-- The same PR adds a gateway guard: a Converse call rejected for its temperature is retried once
-- without it, so a stale flag on another row degrades to a warning instead of a failed call.
--
-- No route change: sonnet-5 is not on any judge route (ADR 0006 §5 writer/judge vendor rule).
--
-- Rollback:
--   UPDATE shared.llm_model_catalog SET supports_temperature = true, updated_at = now(),
--          updated_by = 'rollback-227' WHERE model_key = 'sonnet-5';

BEGIN;

UPDATE shared.llm_model_catalog
   SET supports_temperature = false,
       notes = COALESCE(notes || ' ', '') || 'AA-801 (S236): rejects temperature ("deprecated for this model").',
       updated_at = now(),
       updated_by = 'migration-227-aa801'
 WHERE model_key = 'sonnet-5'
   AND supports_temperature;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('227', now(), 'AA-801: llm_model_catalog sonnet-5 supports_temperature = false '
        '(Bedrock: temperature is deprecated for this model)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
