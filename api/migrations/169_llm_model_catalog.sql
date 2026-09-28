-- Migration 169: AA-658 — shared.llm_model_catalog (Model Gateway P0, ADR 0005)
--
-- The single list of models the platform can call. Replaces three hand-kept lists:
-- pricing.py COST_TABLE (prices), admin_llm_ops.py _WRITER_OPTIONS/_JUDGE_OPTIONS (dropdown) and
-- bedrock_satellite.py _INFERENCE_PROFILES (per-account ids) — those stay only as fallbacks.
--
-- model_key          the platform's name for a model; llm_role_config.model_id stores it. The
--                    legacy keys haiku / sonnet / gpt-4.1 are kept unchanged.
-- bedrock_profile_ids per-account inference-profile id, e.g. {"acc3": "global.anthropic..."}.
-- api_style          how LLMClient calls it: anthropic_native (existing chain), converse (Bedrock
--                    Converse on the satellite session), openai_chat, embed.
-- callable_via       which execution paths can run it: llm_client (LLMClient, no pinned tier),
--                    openai_direct (brand_audit_node), pinned (judge call sites hardcoded to
--                    GPT-4.1 until AA-659), embed. The admin dropdown only offers a model for a
--                    stage whose path is in this list.
-- prices             USD per 1M tokens. NULL cache prices = derive from the input price with the
--                    Anthropic multipliers (write 1.25x, read 0.1x). A row cannot be enabled
--                    without input/output prices.

BEGIN;

CREATE TABLE IF NOT EXISTS shared.llm_model_catalog (
    model_key                  TEXT PRIMARY KEY,
    label                      TEXT NOT NULL,
    vendor                     TEXT NOT NULL CHECK (vendor IN ('anthropic', 'openai', 'cohere', 'typesafe')),
    provider                   TEXT NOT NULL CHECK (provider IN ('bedrock', 'openai', 'typesafe')),
    api_style                  TEXT NOT NULL
                               CHECK (api_style IN ('anthropic_native', 'converse', 'openai_chat', 'embed')),
    bedrock_profile_ids        JSONB NOT NULL DEFAULT '{}'::jsonb,
    wire_model                 TEXT,
    callable_via               TEXT[] NOT NULL DEFAULT '{}',
    supports_temperature       BOOLEAN NOT NULL DEFAULT true,
    max_output_tokens          INTEGER CHECK (max_output_tokens IS NULL OR max_output_tokens > 0),
    price_in_per_mtok          NUMERIC(10, 4),
    price_out_per_mtok         NUMERIC(10, 4),
    price_cache_read_per_mtok  NUMERIC(10, 4),
    price_cache_write_per_mtok NUMERIC(10, 4),
    price_source               TEXT,
    enabled                    BOOLEAN NOT NULL DEFAULT false,
    blocked_reason             TEXT,
    notes                      TEXT,
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by                 TEXT NOT NULL DEFAULT 'migration-169',
    CONSTRAINT llm_model_catalog_enabled_needs_price
        CHECK (NOT enabled OR (price_in_per_mtok IS NOT NULL AND price_out_per_mtok IS NOT NULL))
);

COMMENT ON TABLE shared.llm_model_catalog IS
    'AA-658 / ADR 0005 — single source for the admin model dropdowns and LLM pricing. Owned by '
    'AA-CIS-App; other apps may read it, never write it.';

INSERT INTO shared.llm_model_catalog
    (model_key, label, vendor, provider, api_style, bedrock_profile_ids, wire_model, callable_via,
     supports_temperature, price_in_per_mtok, price_out_per_mtok, price_source, enabled,
     blocked_reason, notes)
VALUES
    ('haiku', 'Claude Haiku 4.5', 'anthropic', 'bedrock', 'anthropic_native',
     '{"acc2": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
       "acc3": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
       "acc1": "global.anthropic.claude-haiku-4-5-20251001-v1:0"}',
     NULL, '{llm_client}', true, 1.0000, 5.0000, 'pricing.py (AA-635)', true, NULL,
     'Legacy key. Called through the existing acc2 -> acc3 -> acc1 chain.'),
    ('sonnet', 'Claude Sonnet 4.5 / 4.6', 'anthropic', 'bedrock', 'anthropic_native',
     '{"acc2": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
       "acc3": "global.anthropic.claude-sonnet-4-6",
       "acc1": "global.anthropic.claude-sonnet-4-6"}',
     NULL, '{llm_client}', true, 3.0000, 15.0000, 'pricing.py', true, NULL,
     'Legacy key. acc2 native is Sonnet 4.5; the satellites invoke Sonnet 4.6 (same price).'),
    ('gpt-4.1', 'GPT-4.1 (OpenAI direct)', 'openai', 'openai', 'openai_chat',
     '{}', 'gpt-4.1', '{llm_client,openai_direct,pinned}', true, 2.0000, 8.0000, 'pricing.py', true,
     NULL, 'Current judge for every judge stage.'),
    ('sonnet-5', 'Claude Sonnet 5', 'anthropic', 'bedrock', 'converse',
     '{"acc3": "global.anthropic.claude-sonnet-5"}',
     NULL, '{llm_client}', true, 2.0000, 10.0000, 'S198 research (not re-verified on the AWS page)',
     true, NULL, 'acc3 only (agreement accepted 28/09/2026). No fallback in P0 (ADR 0005).'),
    ('opus-5-5', 'Claude Opus 5.5', 'anthropic', 'bedrock', 'converse',
     '{"acc3": "global.anthropic.claude-opus-5-5"}',
     NULL, '{llm_client}', true, NULL, NULL, NULL, false,
     'Price not verified yet', 'acc3 only. Eval use only (AA-645) once a price is entered.'),
    ('gpt-5.6-luna', 'GPT-5.6 Luna (Bedrock)', 'openai', 'bedrock', 'converse',
     '{"acc3": "global.openai.gpt-5.6-luna"}',
     NULL, '{llm_client}', false, 0.1000, 0.5000, 'S198 research (not re-verified on the AWS page)',
     true, NULL, 'acc3 only. Judge candidate (AA-644); judge stages can pick it after AA-659.'),
    ('gpt-6-astra', 'GPT-6 Astra (Bedrock)', 'openai', 'bedrock', 'converse',
     '{"acc3": "global.openai.gpt-6-astra"}',
     NULL, '{llm_client}', false, NULL, NULL, NULL, false,
     'Price not verified yet', 'acc3 only.'),
    ('cohere-embed-v4', 'Cohere Embed v4', 'cohere', 'bedrock', 'embed',
     '{"acc2": "us.cohere.embed-v4:0"}',
     NULL, '{embed}', false, NULL, NULL, NULL, false,
     'Price not verified yet',
     'In use today by content_embedding.py and TripPlanner, which bypass the catalog until AA-659.')
ON CONFLICT (model_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('169', now(),
    'AA-658: shared.llm_model_catalog — model catalog for dropdowns, pricing and the Converse adapter')
ON CONFLICT (version) DO NOTHING;

COMMIT;
