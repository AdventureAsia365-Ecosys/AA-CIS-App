-- Migration 175: AA-652 — T2 tenant rewrite runs as a durable job (shared.job, migration 174).
--
-- tenant_tour_versions.job_id  the job writing this version (NULL for tenant manual edits and
--                              for rows written before AA-652).
-- status 'failed'              the rewrite job ended without success (all attempts used, or
--                              cancelled). Before, a failed or interrupted rewrite left the row
--                              'pending' forever and the portal showed "Writing…" forever.
-- Additive; apply before the AA-652 deploy.

BEGIN;

ALTER TABLE gold_aa_internal.tenant_tour_versions
    ADD COLUMN IF NOT EXISTS job_id UUID REFERENCES shared.job(id);

CREATE INDEX IF NOT EXISTS tenant_tour_versions_job_idx
    ON gold_aa_internal.tenant_tour_versions (job_id) WHERE job_id IS NOT NULL;

ALTER TABLE gold_aa_internal.tenant_tour_versions
    DROP CONSTRAINT IF EXISTS tenant_tour_versions_status_check,
    ADD CONSTRAINT tenant_tour_versions_status_check
        CHECK (status = ANY (ARRAY['pending', 'approved', 'rejected', 'needs_review', 'ai_generated',
                                   'failed']));

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('175', now(), 'AA-652: tenant_tour_versions.job_id + status failed (T2 rewrite as a job)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
