-- Migration 174: AA-650 — shared.job, the durable job queue (ADR 0001 decision 3).
--
-- Long work (research, rewrite, write, atomize) used to run as asyncio tasks inside the API
-- container and died with it on every deploy/restart. A job is now a row here: a worker claims it
-- with FOR UPDATE SKIP LOCKED, keeps a lease (locked_until) alive with a heartbeat, and the reaper
-- re-queues or fails jobs whose lease expired.
--
-- Status: queued -> running -> succeeded | failed | stopped_budget | cancelled.
-- Additive; apply before the AA-650 deploy.

BEGIN;

CREATE TABLE IF NOT EXISTS shared.job (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             TEXT NOT NULL,
    payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'succeeded', 'failed',
                                       'stopped_budget', 'cancelled')),
    attempt          INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    run_after        TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by        TEXT,
    locked_until     TIMESTAMPTZ,
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    idempotency_key  TEXT,
    progress         JSONB NOT NULL DEFAULT '{}'::jsonb,
    result           JSONB,
    cost_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0,
    error            TEXT,
    parent_job_id    UUID REFERENCES shared.job(id),
    created_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);

-- One job per idempotency key, ever (a finished job is returned instead of enqueuing again).
CREATE UNIQUE INDEX IF NOT EXISTS job_idempotency_key_uq
    ON shared.job (idempotency_key) WHERE idempotency_key IS NOT NULL;
-- Claim path: next queued job that is due.
CREATE INDEX IF NOT EXISTS job_queued_due_idx
    ON shared.job (run_after, created_at) WHERE status = 'queued';
-- Reaper + per-kind concurrency count.
CREATE INDEX IF NOT EXISTS job_running_idx
    ON shared.job (kind, locked_until) WHERE status = 'running';
-- Admin Jobs page.
CREATE INDEX IF NOT EXISTS job_kind_created_idx ON shared.job (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS job_parent_idx ON shared.job (parent_job_id) WHERE parent_job_id IS NOT NULL;

COMMENT ON TABLE shared.job IS
    'AA-650 — durable job queue (ADR 0001 decision 3). Claimed with FOR UPDATE SKIP LOCKED; '
    'locked_until is a lease kept alive by the worker heartbeat.';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('174', now(), 'AA-650: shared.job — durable job queue')
ON CONFLICT (version) DO NOTHING;

COMMIT;
