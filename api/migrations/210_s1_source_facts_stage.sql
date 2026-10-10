-- Migration 210: AA-748 — seed the `s1_source_facts` stage (S1 per-day source-fact extractor).
--
-- Context (S224, AA-748): the S1 writer gets structured per-day source facts (days, places,
-- numbers, meals) extracted once per tour, so numbers/meals in the rewrite can be constrained to
-- what the source literally lists. The extraction is one LLM call on a new stage,
-- `s1_source_facts`, routed through the gateway (LLMClient.generate(stage="s1_source_facts")) and
-- logged to shared.llm_call_log like every S1 stage.
--
-- SEEDED TO HAIKU on purpose (same as the other S1 writer stages s1_generate / s1_flag_fix /
-- s1_itinerary_nudge): fact extraction is a cheap, structured-extraction task, no judge, no retry.
-- Admin can retune it via /admin/llm-config (Settings > LLM Models) after a measured trial. The
-- SAFE_DEFAULTS entry in shared/llm_client/role_config.py mirrors this row so routing still points
-- at Haiku if the DB (and so the catalog) is unreachable.
--
-- role/provider/account_route match the S1 writer stages (writer/claude/acc3) so the fallback
-- chain and the SAFE_DEFAULTS entry stay consistent.
--
-- Not applied by CI/CD — Claude Code applies this by hand through ECS exec (see CONTEXT.md).

BEGIN;

INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, updated_by) VALUES
    ('s1_source_facts', 'writer', 'claude', 'haiku', 'acc3', 'system-migration-aa748')
ON CONFLICT (stage) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('210', now(), 'AA-748: add s1_source_facts stage (S1 per-day source-fact extractor, seeded Haiku)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
