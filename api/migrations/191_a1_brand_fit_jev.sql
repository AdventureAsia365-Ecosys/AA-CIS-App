-- Migration 191: AA-692 A1-4 — the A1 judge tie-break Jev Question, in SHADOW.
--
-- Design: docs/architecture/at-series-v2-design.md §4.1 A1-4.
-- a1_brand_fit: asked in judge_node only when the A1 judge score is within ±0.5 of MIN_QUALITY (7.0) and validate
--   already passed, i.e. the judge alone decides retry/HITL. Once enforced, a confident yes lifts the score to 7.0,
--   a confident no drops it just below. State = {brand: core idea / who / wants / voice examples, content: name /
--   subtitle / summary / highlights}. Platform content only (A1). Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a1_brand_fit', 's1_judge_tiebreak', 'noul',
     'Does this tour copy fit this brand: its core idea, the traveller it is for, and the way it speaks?',
     '{"true": "The copy speaks to the traveller the brand describes, in a voice close to its examples, and nothing in it works against the core idea.",
       "false": "The copy is generic or in a different voice (hype, discount, mass-market), or it addresses a different kind of traveller than the brand describes."}',
     'shadow', 'AA-692 A1-4: tie-break when the A1 judge score is within ±0.5 of 7.0; a confident verdict decides pass vs retry once enforced.',
     'migration-191')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('191', now(), 'AA-692: A1 judge tie-break Jev question a1_brand_fit (shadow)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
