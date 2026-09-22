-- Migration 163: AA-610 (Sub 2 — scope fix) — acp_contract.atom_segment.questions_count cache.
--
-- A live re-test of the Sub 2 redesign (migration 162, PR #419) found `precompute_question_
-- landings()` still not completing within several hours on a single tour-triggered recompute —
-- not because of the 6x-per-market duplication that redesign already fixed, but because it
-- re-lands PAA questions for EVERY platform Segment on EVERY call, regardless of which tour's
-- atoms actually changed. `recompute_segment_score_route()` (services/export/handler.py) fires
-- per-tour (one PATCH, one atomize run), but the function it calls was scanning the whole
-- platform's Segment set every single time.
--
-- The real fix is scope, not just further de-duplication: only the Segments that contain the
-- triggering tour's atoms need their `questions` count recomputed right now — every OTHER
-- Segment's landing has nothing to do with this tour and did not change. But `run_atom_ranking()`
-- still rebuilds `atom_ranking` for the WHOLE platform on every call (its own docstring: "DELETE+
-- INSERT... over the WHOLE PLATFORM Segment set") and needs a `questions` count for EVERY
-- Segment, not just the ones just touched — so the count for an untouched Segment must be
-- READ FROM SOMEWHERE, not silently written as 0 (that would silently zero out every other
-- tour's `questions` rank-sum axis on every recompute, a correctness regression far worse than
-- the performance problem being fixed).
--
-- This column IS that somewhere: computed once per Segment (whenever a tour touching it last
-- triggered a recompute), persisted here, and simply read back for every Segment
-- `precompute_question_landings()` was not asked to recompute this call.

BEGIN;

ALTER TABLE acp_contract.atom_segment
    ADD COLUMN questions_count INTEGER,
    ADD COLUMN questions_computed_at TIMESTAMPTZ;

COMMENT ON COLUMN acp_contract.atom_segment.questions_count IS
    'AA-610 (Sub 2 scope fix) — cached land_questions_for_segment() result, so a recompute
    triggered by one tour does not have to re-land PAA questions for every OTHER platform
    Segment too. NULL means never computed (a brand-new Segment) — precompute_question_
    landings() always computes those regardless of the segment_ids scope it was given.';

COMMENT ON COLUMN acp_contract.atom_segment.questions_computed_at IS
    'AA-610 (Sub 2 scope fix) — when questions_count was last computed. Informational only
    (no TTL/staleness check reads it yet) — kept so a future staleness policy has the data it
    would need without another migration.';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('163', now(),
    'AA-610 (Sub 2 scope fix): acp_contract.atom_segment.questions_count/questions_computed_at
    — cache PAA landing counts per Segment so a tour-scoped recompute only re-lands questions
    for that tour''s own Segments, reading every other Segment''s count back from cache instead
    of re-scanning the whole platform')
ON CONFLICT (version) DO NOTHING;

COMMIT;
