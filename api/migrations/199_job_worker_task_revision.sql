-- Migration 199: AA-711 — shared.job_worker.task_revision (ECS task-definition revision).
--
-- The worker runs inside the API task. After a deploy the old task drains for ~5 min and its worker
-- kept claiming jobs, running them on the old code (S207: a prefetch job wrote keywords the new code
-- filters out). A worker now yields — stops claiming — while a live worker of a newer revision exists.
-- NULL = unknown (local / non-ECS run): such a worker never yields and never makes others yield.

BEGIN;

ALTER TABLE shared.job_worker ADD COLUMN IF NOT EXISTS task_revision INT;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('199', now(), 'AA-711: shared.job_worker.task_revision (old task stops claiming after a deploy)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
