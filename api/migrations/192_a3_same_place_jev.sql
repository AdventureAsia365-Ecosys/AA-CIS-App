-- Migration 192: AA-695 A3-7 — the Segment "same real-world place?" Jev Question, in SHADOW.
--
-- Design: docs/architecture/at-series-v2-design.md §4.2 A3-7. Measured S205: 384 of 3,337 Segments hold atoms naming
-- different places; about a quarter of a 40-sample were wrong merges (Bukchon vs Jeonju Hanok Village, Wat Saket vs
-- Wat Si Saket, different trek legs). Asked in run_segment_matching() for place pairs the moment rule would join
-- although written differently (same country or unknown; different countries are kept apart without asking).
-- Once enforced, a confident no keeps the pair apart. State = {place_a, place_b, country}. Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a3_same_place', 'a3_segment_match', 'noul',
     'Do these two names refer to the same real-world place?',
     '{"true": "The same place written differently: a longer or shorter name, a spelling variant, or the place plus the town it is in.",
       "false": "Two different places, even if the names share words (two villages, two temples, two stretches of a route), or one is only a generic kind of place."}',
     'shadow', 'AA-695 A3-7: asked before two differently-named places join one Segment; a confident no keeps them apart once enforced.',
     'migration-192')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('192', now(), 'AA-695: Segment same-place Jev question a3_same_place (shadow)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
