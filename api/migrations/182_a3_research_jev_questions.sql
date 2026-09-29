-- Migration 182: AA-693 — the two Jev questions of A3 segment research, seeded in SHADOW mode.
--
-- Design: docs/architecture/at-series-v2-design.md §4.2 A3-3 / A3-4. In shadow they only log
-- verdicts (calibration data for AA-661); a question drops keywords/ideas only once an admin sets it
-- to `enforce` with calibrated floors (/admin/decisions). The a3_keyword_belongs row may already
-- exist from the S203 live smoke (same wording) — ON CONFLICT keeps it.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a3_keyword_belongs', 'a3_research', 'noul',
     'Is this search keyword about this particular place, not just the kind of thing the place is?',
     '{"true": "The keyword names or clearly targets this specific place.",
       "false": "The keyword is a generic category or somewhere else (e.g. \"hot springs\" for one named onsen)."}',
     'shadow', 'AA-693 A3-3: asked before DataForSEO volumes are bought; state = {place, keyword}.',
     'migration-182'),
    ('a3_idea_traveller', 'a3_research', 'noul',
     'Is this a search by a traveller who wants to see or do something at a destination, rather than a search for a hotel, resort, homestay or a booking?',
     '{"true": "Sightseeing, activities, events, things to do, how to get there.",
       "false": "A named hotel/resort/lodge, accommodation, or a booking/price search."}',
     'shadow', 'AA-693 A3-4: asked before a keywords_for_keywords idea is stored; state = {keyword, seed_places}.',
     'migration-182')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('182', now(), 'AA-693: A3 research Jev questions (a3_keyword_belongs, a3_idea_traveller) in shadow')
ON CONFLICT (version) DO NOTHING;

COMMIT;
