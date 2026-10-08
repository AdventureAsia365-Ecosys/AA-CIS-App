-- Migration 206: AA-742 — count Jev cache hits in a daily rollup instead of a decision_log row each.
--
-- Problem (S218, 08/10/2026): shared.decision_log held ~780k rows (495 MB), ~85% of them cache hits
-- (cached=true, $0, no Jev call). A3 recomputes re-ask every segment's question after each atomized
-- tour, and decide() inserted a full ledger row for every answer it served from cache
-- (a3_activity_type: ~392 rows per subject in 7 days). Those rows also refreshed created_at, so the
-- 180-day cache expiry never fired for a verdict that kept being re-read.
--
-- From this migration on, decide() writes ledger rows only for verdicts that were actually asked
-- (or errored/skipped) and adds cache hits to this table (+n per (day, stage, question, mode, zone)).
-- mode + zone are kept so /admin/decisions/summary can still show the same zone/acted counts.

CREATE TABLE IF NOT EXISTS shared.decision_cache_hits_daily (
    day          date        NOT NULL,
    stage        text        NOT NULL,
    question_key text        NOT NULL,
    mode         text        NOT NULL,
    zone         text        NOT NULL,
    hits         bigint      NOT NULL DEFAULT 0,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, stage, question_key, mode, zone)
);

COMMENT ON TABLE shared.decision_cache_hits_daily IS
    'AA-742: Jev verdicts served from the decide() cache, counted per day (no decision_log row each).';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('206', now(),
    'AA-742: shared.decision_cache_hits_daily — Jev cache hits counted per day/stage/question/mode/zone '
    'instead of one decision_log row per hit')
ON CONFLICT (version) DO NOTHING;
