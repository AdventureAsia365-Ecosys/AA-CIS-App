-- Migration 178: AA-687 — shared.job_worker, liveness of the job-runner workers (Jobs page).
--
-- A worker only touched shared.job while it held a lease, so an idle worker left no trace and the
-- reaper's work (expired leases re-queued / failed) was only in CloudWatch. Each worker now upserts
-- one row here: at start, about every 15 s from its loop, and at shutdown (stopped_at). The Jobs
-- page reads it for the worker health panel. Nothing in the queue logic reads this table — a
-- failed write here never affects claiming or running jobs.
--
-- Rows of stopped workers are kept 7 days (pruned by the worker at start) so the page can show
-- recent restarts, e.g. the task replaced by a deploy.
-- Additive; apply BEFORE the deploy — the worker writes this table from its first cycle
-- (a missing table only logs a warning, but the health panel stays empty).

BEGIN;

CREATE TABLE IF NOT EXISTS shared.job_worker (
    worker_id        TEXT PRIMARY KEY,           -- "<hostname>:<pid>:<random>" (worker.py)
    host             TEXT NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    stopped_at       TIMESTAMPTZ,
    max_parallel     INTEGER NOT NULL,
    caps             JSONB NOT NULL DEFAULT '{}'::jsonb,  -- per-kind concurrency caps it claims with
    running_jobs     INTEGER NOT NULL DEFAULT 0,
    reaped_requeued  INTEGER NOT NULL DEFAULT 0,          -- totals since this worker started
    reaped_failed    INTEGER NOT NULL DEFAULT 0,
    last_reap_at     TIMESTAMPTZ,                         -- last reap that changed any job
    last_reaped      JSONB                                -- {"requeued": [ids], "failed": [ids]}
);

CREATE INDEX IF NOT EXISTS job_worker_last_seen_idx ON shared.job_worker (last_seen_at DESC);

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('178', now(), 'AA-687: shared.job_worker (worker liveness for the Jobs page)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
