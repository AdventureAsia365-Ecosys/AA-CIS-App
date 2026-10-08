-- Migration 205: AA-660 follow-up — covering indexes for /admin/decisions/summary.
--
-- Problem (S218, 08/10/2026): /admin/decisions/summary?days=7 returned 504 at the 29 s API Gateway
-- limit. shared.decision_log holds ~780k rows (~700k inside a 7-day window during the A-series
-- rerun waves; ~85% are cache hits from A3 recomputes). The summary reads every row of the window
-- through decision_log_question_created_idx / decision_log_stage_created_idx and then fetches each
-- heap page for zone/mode/cost/latency: EXPLAIN showed 26.7k cold buffer reads, 15.8 s of I/O for
-- the per-question query alone (18 s), ~26 s for the five queries run one after another.
--
-- Fix: the same two btree keys, plus INCLUDE of every column the summary aggregates, so the
-- aggregates are served by index-only scans (insert-only table → autovacuum keeps the visibility
-- map current). The old indexes are dropped after the new ones exist; their key prefix is the same,
-- so every other reader (log page filters, cache lookup uses decision_log_cache_idx) is unaffected.
--
-- Apply OUTSIDE a transaction, one statement at a time (CREATE/DROP INDEX CONCURRENTLY).

CREATE INDEX CONCURRENTLY IF NOT EXISTS decision_log_question_created_cov_idx
    ON shared.decision_log (question_key, created_at DESC)
    INCLUDE (zone, mode, cached, cost_usd, latency_ms, probability);

CREATE INDEX CONCURRENTLY IF NOT EXISTS decision_log_stage_created_cov_idx
    ON shared.decision_log (stage, created_at DESC)
    INCLUDE (question_key, zone, mode, cached, cost_usd, latency_ms);

DROP INDEX CONCURRENTLY IF EXISTS shared.decision_log_question_created_idx;
DROP INDEX CONCURRENTLY IF EXISTS shared.decision_log_stage_created_idx;

VACUUM (ANALYZE) shared.decision_log;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('205', now(),
    'AA-660 follow-up: covering indexes on shared.decision_log (question_key|stage, created_at) '
    'INCLUDE the aggregated columns, so /admin/decisions/summary uses index-only scans (504 at 7d)')
ON CONFLICT (version) DO NOTHING;
