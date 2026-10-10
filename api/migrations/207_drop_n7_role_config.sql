-- Migration 207: AA-753 — drop the dead N7 stages from shared.llm_role_config.
--
-- The N7 produce pipeline (services/acp_produce/gates.py, its n7_draft/adapt/faq/repair/
-- gap_research/judge stages) had 0 live callers and 0 calls in 90 days; the live content path is
-- services/acp_content_writing/quality_gates.py (t10_judge). The gate stack and its SAFE_DEFAULTS
-- entries are removed in the same change; this deletes the matching config rows so the admin
-- Settings › LLM Models route editor and the Shadow A/B report no longer list stages nothing reads.
--
-- Idempotent: DELETE of rows that may already be gone is a no-op on re-run.

DELETE FROM shared.llm_role_config
WHERE stage IN ('n7_adapt', 'n7_draft', 'n7_faq', 'n7_gap_research', 'n7_judge', 'n7_repair');

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('207', now(),
    'AA-753: delete the dead N7 stages (n7_adapt/n7_draft/n7_faq/n7_gap_research/n7_judge/n7_repair) '
    'from shared.llm_role_config')
ON CONFLICT (version) DO NOTHING;
