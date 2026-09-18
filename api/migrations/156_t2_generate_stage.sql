-- Migration 156: AA-620 (epic AA-616) — split a dedicated `t2_generate` stage for the tenant (T2)
-- self-service rewrite, separate from the shared `s1_generate` stage.
--
-- Context (S187/S188, Nghiệp): A1 admin batch-rewrite and T2 tenant self-service rewrite run the
-- SAME S1 LangGraph and, until now, the SAME `s1_generate` stage row. But they are different
-- products:
--   * A1 admin  = generic internal Master Content, brand 'default' empty -> judge brand-fit
--                 SKIPPED. Batch of 763 tours -> should be cheap (Haiku).
--   * T2 tenant = a paying tenant's real rewrite-to-publish, with their OWN brand identity + DFS
--                 keywords -> judge brand-fit ACTUALLY scores (bug fixed in AA-612a). Brand-voice
--                 quality matters -> may warrant Sonnet.
-- With one shared row we could not set "A1=Haiku, T2=Sonnet" by config alone. This migration adds
-- the `t2_generate` row so admin can tune the T2 writer independently in Settings > LLM Models.
--
-- SEEDED TO HAIKU on purpose (identical to s1_generate today) — this migration only creates the
-- lever, it does NOT change model behaviour. Whether T2 moves to Sonnet is decided AFTER a
-- brand-voice A/B on real tenant tours (AA-620 step 4); admin flips it via /admin/llm-config then.
--
-- role/provider/account_route match s1_generate's seed (writer/claude/acc3) so the fallback chain
-- and the SAFE_DEFAULTS entry stay consistent. s1_flag_fix / s1_itinerary_nudge remain SHARED
-- between A1 and T2 (Haiku is fine for both) — intentionally NOT split here (see AA-620).

BEGIN;

INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, updated_by) VALUES
    ('t2_generate', 'writer', 'claude', 'haiku', 'acc3', 'system-migration-aa620')
ON CONFLICT (stage) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('156', now(), 'AA-620: add t2_generate stage (tenant T2 writer, seeded Haiku like s1_generate)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
