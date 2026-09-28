-- Migration 170: AA-659 — stage routes (fallback chain + shadow) + verified catalog prices.
--
-- Additive only: the code deployed before AA-659 ignores the new columns, so this migration is
-- applied BEFORE the AA-659 deploy. The judge switch itself is migration 171 (after deploy).
--
-- shared.llm_role_config gains:
--   fallback_model_ids  ordered Model Keys tried after model_id fails or is disabled. Empty =
--                       the pre-AA-659 behaviour for that stage.
--   shadow_model_id     a Model Key run in the background on the same request; logged only,
--                       never affects output (A/B, AA-644 / AA-645).
--   shadow_sample_pct   share of calls that also run the shadow (0 = off).
-- shared.llm_shadow_log stores primary vs shadow outputs for the A/B comparison.

BEGIN;

ALTER TABLE shared.llm_role_config
    ADD COLUMN IF NOT EXISTS fallback_model_ids TEXT[]  NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS shadow_model_id    TEXT,
    ADD COLUMN IF NOT EXISTS shadow_sample_pct  INTEGER NOT NULL DEFAULT 0
        CHECK (shadow_sample_pct BETWEEN 0 AND 100);

CREATE TABLE IF NOT EXISTS shared.llm_shadow_log (
    id                 BIGSERIAL PRIMARY KEY,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    stage              TEXT NOT NULL,
    primary_model      TEXT NOT NULL,
    shadow_model       TEXT NOT NULL,
    primary_output     TEXT,
    shadow_output      TEXT,
    shadow_error       TEXT,
    primary_cost_usd   NUMERIC(12, 6),
    shadow_cost_usd    NUMERIC(12, 6),
    shadow_latency_ms  INTEGER,
    request_sha256     TEXT
);
CREATE INDEX IF NOT EXISTS llm_shadow_log_stage_time ON shared.llm_shadow_log (stage, created_at DESC);

COMMENT ON TABLE shared.llm_shadow_log IS
    'AA-659 — primary vs shadow model outputs per call, for A/B comparison. Outputs truncated to 16k chars.';

-- Prices verified 28/09/2026 (S200).
UPDATE shared.llm_model_catalog SET
    price_cache_read_per_mtok = 0.20, price_cache_write_per_mtok = 2.50,
    price_source = 'AWS Bedrock pricing, Global cross-region, us-west-1 (28/09/2026)',
    updated_at = now(), updated_by = 'migration-170'
WHERE model_key = 'sonnet-5';

UPDATE shared.llm_model_catalog SET
    price_in_per_mtok = 4.00, price_out_per_mtok = 20.00,
    price_cache_read_per_mtok = 0.20, price_cache_write_per_mtok = 5.00,
    price_source = 'Anthropic API list price, claude.com/pricing (28/09/2026); '
                   'Bedrock Global price not published yet (Geo/in-region: $4.40/$22)',
    enabled = true, blocked_reason = NULL, updated_at = now(), updated_by = 'migration-170'
WHERE model_key = 'opus-5-5';

UPDATE shared.llm_model_catalog SET
    price_in_per_mtok = 0.20, price_out_per_mtok = 1.20, price_cache_read_per_mtok = 0.02,
    price_source = 'OpenAI API list price (28/09/2026); Bedrock price not published. '
                   'Migration 169 had $0.10/$0.50, which is GPT-6 Luna.',
    updated_at = now(), updated_by = 'migration-170'
WHERE model_key = 'gpt-5.6-luna';

UPDATE shared.llm_model_catalog SET
    price_in_per_mtok = 10.00, price_out_per_mtok = 50.00, price_cache_read_per_mtok = 1.00,
    price_source = 'OpenAI API list price (28/09/2026); Bedrock price not published',
    enabled = true, blocked_reason = NULL, updated_at = now(), updated_by = 'migration-170'
WHERE model_key = 'gpt-6-astra';

UPDATE shared.llm_model_catalog SET
    price_in_per_mtok = 0.12, price_out_per_mtok = 0.00,
    price_source = 'AWS Bedrock pricing, us-west-1 (28/09/2026)',
    enabled = true, blocked_reason = NULL, updated_at = now(), updated_by = 'migration-170'
WHERE model_key = 'cohere-embed-v4';

UPDATE shared.llm_model_catalog SET callable_via = '{llm_client}', updated_at = now(),
    updated_by = 'migration-170'
WHERE model_key = 'gpt-4.1';

INSERT INTO shared.llm_model_catalog
    (model_key, label, vendor, provider, api_style, bedrock_profile_ids, wire_model, callable_via,
     supports_temperature, price_in_per_mtok, price_out_per_mtok, price_cache_read_per_mtok,
     price_source, enabled, blocked_reason, notes, updated_by)
VALUES
    ('gpt-6-luna', 'GPT-6 Luna (Bedrock)', 'openai', 'bedrock', 'converse',
     '{"acc3": "global.openai.gpt-6-luna"}', NULL, '{llm_client}', false, 0.10, 0.50, 0.01,
     'OpenAI API list price (28/09/2026); Bedrock price not published', false,
     'Bedrock agreement not accepted on acc3 yet (28/09/2026)',
     'Enable after the acc3 agreement + invoker IAM ARNs are in place.', 'migration-170'),
    ('gpt-6-luna-openai', 'GPT-6 Luna (OpenAI API)', 'openai', 'openai', 'openai_chat',
     '{}', 'gpt-6-luna', '{llm_client}', false, 0.10, 0.50, 0.01,
     'OpenAI API list price (28/09/2026)', true, NULL,
     'Same model as gpt-6-luna, served by the OpenAI API (external path kept on purpose).',
     'migration-170')
ON CONFLICT (model_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('170', now(),
    'AA-659: llm_role_config fallback/shadow columns, llm_shadow_log, verified catalog prices')
ON CONFLICT (version) DO NOTHING;

COMMIT;
