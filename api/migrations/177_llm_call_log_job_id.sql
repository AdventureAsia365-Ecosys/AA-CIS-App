-- Migration 177: AA-652 follow-up — attribute LLM spend to the durable job that caused it.
--
-- shared.llm_call_log.job_id  the shared.job running when the call was made (NULL for calls made
--                             outside a job: request handlers, scripts, Lambdas).
-- The worker binds the job id in a contextvar around each handler; call_log.py writes it on every
-- row. The Jobs page then shows job cost = shared.job.cost_usd (non-LLM spend the handler reports,
-- e.g. DataForSEO) + SUM(llm_call_log.cost_usd WHERE job_id = job.id).
-- No FK on purpose: a log write must never fail because of the job table (fire-and-forget log).
-- Additive; apply BEFORE the deploy — the new INSERT names this column.

BEGIN;

ALTER TABLE shared.llm_call_log
    ADD COLUMN IF NOT EXISTS job_id UUID;

CREATE INDEX IF NOT EXISTS llm_call_log_job_idx
    ON shared.llm_call_log (job_id) WHERE job_id IS NOT NULL;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('177', now(), 'AA-652: llm_call_log.job_id (job cost attribution)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
