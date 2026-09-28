-- Migration 173: AA-685 part 2 — let AA-TripPlanner-Web use the gateway tables.
--
-- TripPlanner's Lambdas connect as the `tripplanner` role. Its thin gateway client reads the
-- stage route and the model catalog, and writes one shared.llm_call_log row per model call.
-- Least privilege: SELECT on the two config tables, INSERT only on the log (no read-back).
--
-- Also seeds the two stages used by TripPlanner's offline extraction pipeline
-- (backend/extraction/run.py, backfill_embeddings.py), which builds itinerary_components:
--   tp_extract          Claude, extracts components from a published tour (Sonnet 4.6, as before).
--   tp_component_embed  Cohere Embed v4 on acc2, one per component (search_document).
-- tp_compose / tp_search_embed were seeded by migration 172.
--
-- Additive; apply before the TripPlanner deploy.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tripplanner') THEN
        GRANT USAGE ON SCHEMA shared TO tripplanner;
        GRANT SELECT ON shared.llm_role_config, shared.llm_model_catalog TO tripplanner;
        GRANT INSERT ON shared.llm_call_log TO tripplanner;
    END IF;
END $$;

INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, updated_by) VALUES
    ('tp_extract',         'writer', 'claude', 'sonnet',          'acc3', 'migration-173'),
    ('tp_component_embed', 'embed',  'cohere', 'cohere-embed-v4', NULL,   'migration-173')
ON CONFLICT (stage) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('173', now(),
    'AA-685: tripplanner role may read llm_role_config/llm_model_catalog and insert llm_call_log; '
    'stages tp_extract, tp_component_embed')
ON CONFLICT (version) DO NOTHING;

COMMIT;
