-- Migration 176: AA-652 PR-B — T9 write runs as a durable job (shared.job, migration 174).
--
-- content_piece.job_id  the job writing this piece (NULL for pieces written before AA-652 and
--                       for the buffer-retry piece a job inserts itself).
-- A write interrupted by a deploy used to leave the piece 'processing' forever ("Writing…" in the
-- Social Content wizard and My Content). Now the job re-queues, and a job that ends without
-- success marks the piece 'failed' (existing status, migration 118), which the portal already
-- shows with its Retry button.
-- Additive; apply before the AA-652 PR-B deploy.

BEGIN;

ALTER TABLE acp_shared.content_piece
    ADD COLUMN IF NOT EXISTS job_id UUID REFERENCES shared.job(id);

CREATE INDEX IF NOT EXISTS content_piece_job_idx
    ON acp_shared.content_piece (job_id) WHERE job_id IS NOT NULL;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('176', now(), 'AA-652: content_piece.job_id (T9 write as a job)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
