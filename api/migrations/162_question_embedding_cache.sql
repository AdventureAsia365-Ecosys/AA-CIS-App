-- Migration 162: AA-610 (Sub 2 redesign) — acp_contract.question_embedding.
--
-- Sub 2's first live test found land_questions_for_segment() re-embeds the SAME PAA question
-- text every time it is a shortlist candidate for a DIFFERENT Segment (or the same Segment on a
-- later run) — the question's wording never changes, only which Segment's atoms it's compared
-- against does, so nothing about its own embedding needs recomputing. Combined with
-- run_atom_ranking() previously calling this per-market (6x/atomize run, market-independent
-- work redone 6 times, closed by the same redesign this migration is part of), a live re-atomize
-- test on 5 tours never completed within several minutes even after AA-610's first pacing fix
-- (PR #418) — the real fix is not calling Cohere Embed v4 that many times in the first place,
-- not just spacing the calls out further.
--
-- One row per distinct question text (not per (question, atom) — that's what atom_matches
-- already is). Looked up by a hash of the normalised text rather than the raw text as the key,
-- matching this codebase's own atom_id/segment_id precedent (content_hash_atom_id(),
-- segment_matching.py's _mint()) of hashing rather than using free text as a primary key.

BEGIN;

CREATE TABLE acp_contract.question_embedding (
    question_hash TEXT PRIMARY KEY,
    question_text TEXT NOT NULL,
    embedding     VECTOR(1536) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE acp_contract.question_embedding IS
    'AA-610 (Sub 2 redesign) — one row per distinct PAA question text, keyed by sha256(normalised '
    'text). Looked up before calling compute_embedding() so the same question asked by several '
    'Segments (or re-ranked on a later run) is embedded via Cohere Embed v4 at most once ever, '
    'not once per Segment that shortlists it.';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('162', now(),
    'AA-610 (Sub 2 redesign): acp_contract.question_embedding — cache PAA question embeddings '
    'by text hash, closing the re-embed-the-same-question-many-times gap PR #417/418 left open')
ON CONFLICT (version) DO NOTHING;

COMMIT;
