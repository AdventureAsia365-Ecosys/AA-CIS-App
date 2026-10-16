-- Job table DDL template — ADR 0003a decision 2, roadmap X6 (AA-751), data standards C5.1b (as amended by AA-751),
-- C5.2e, C6.1d.
--
-- The reference shape of every owner's <schema>.job. No runner applies this file. An owner copies the
-- rendered statements into its OWN migration (CIS NNN, TripPlanner tp-NNN, Booking bk-NNN; add IF NOT EXISTS
-- there), and its CI renders this file into a scratch schema and compares its live table with it through
-- pg_catalog (shared/jobs/job_table_shape.py): columns, CHECKs, FK, indexes by definition, the updated_at
-- trigger and reloptions. Names of indexes / constraints / the trigger function are not compared.
--
-- Placeholders (job_table_shape.render; full-line comments are dropped before they are filled):
--   {{schema}}                owner schema (shared, booking, tripplanner, ...)
--   {{kind_pattern}}          owner kind regex: CIS '^(?!bk_|tp_)[a-z][a-z0-9_]*$' (unprefixed, ADR 0003a d3),
--                             Booking '^bk_[a-z0-9_]+$', TripPlanner '^tp_[a-z0-9_]+$'
--   {{extra_statuses}}        owner-only statuses, e.g. ", 'stopped_budget'" (CIS) or "" (others)
--   {{max_attempts_default}}  CIS 3, Booking 5
--
-- Byte copies: AA-Booking backend/tests/job_template/job_table_template.sql (drift-checked by its CI).
-- Changing this file = copy it there in the same session.

CREATE TABLE {{schema}}.job (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             TEXT NOT NULL,
    payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'queued',
    attempt          INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT {{max_attempts_default}},
    run_after        TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by        TEXT,
    locked_until     TIMESTAMPTZ,
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    idempotency_key  TEXT,
    progress         JSONB NOT NULL DEFAULT '{}'::jsonb,
    result           JSONB,
    cost_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0,
    error            TEXT,
    parent_job_id    UUID REFERENCES {{schema}}.job(id),
    created_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    CONSTRAINT job_kind_chk     CHECK (kind ~ '{{kind_pattern}}'),
    CONSTRAINT job_status_chk   CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'{{extra_statuses}})),
    CONSTRAINT job_attempts_chk CHECK (attempt >= 0 AND max_attempts >= 1),
    CONSTRAINT job_lease_chk    CHECK (status <> 'running' OR (locked_by IS NOT NULL AND locked_until IS NOT NULL))
);

-- One job per idempotency key, ever.
CREATE UNIQUE INDEX job_idempotency_key_uq ON {{schema}}.job (idempotency_key) WHERE idempotency_key IS NOT NULL;
-- Claim path: next due queued job.
CREATE INDEX job_queued_due_idx ON {{schema}}.job (run_after, created_at) WHERE status = 'queued';
-- Reaper + per-kind running count.
CREATE INDEX job_running_idx ON {{schema}}.job (kind, locked_until) WHERE status = 'running';
-- Admin Jobs page.
CREATE INDEX job_kind_created_idx ON {{schema}}.job (kind, created_at DESC);
CREATE INDEX job_parent_idx ON {{schema}}.job (parent_job_id) WHERE parent_job_id IS NOT NULL;
-- Retention keyset walk (NAM-15, C5.2e).
CREATE INDEX job_finished_idx ON {{schema}}.job (finished_at, id) WHERE finished_at IS NOT NULL;

CREATE FUNCTION {{schema}}.job_set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER job_set_updated_at
    BEFORE UPDATE ON {{schema}}.job
    FOR EACH ROW EXECUTE FUNCTION {{schema}}.job_set_updated_at();

-- High-churn queue (heartbeat UPDATEs), C5.1b as amended by AA-751: 50 + 2 % of rows is never later than the
-- default 50 + 20 %, at any table size (retention keeps job tables at a few thousand rows).
ALTER TABLE {{schema}}.job SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_threshold = 50);
