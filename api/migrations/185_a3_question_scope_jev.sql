-- Migration 185: AA-694 — the two country-scope Jev Questions for People Also Ask landing, in SHADOW.
--
-- Design: docs/architecture/at-series-v2-design.md §4.2 / §8 (Q3, Q4). Wording ported from Ms. Thư's
-- ticket 04 (aa-soscial-media, `_scope_asks`); the countries travel in the state, not the wording, so
-- the wording hash stays fixed across country sets. State = {question, countries} where countries is
-- the Segment's own tour countries. A question stops counting when a3_question_foreign is a confident
-- yes OR a3_question_about_here is a confident no — only once each is enforced with a Calibration
-- Record (AA-661). Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a3_question_foreign', 'a3_question_scope', 'noul',
     'Does this question name a place, landmark or region outside the countries given?',
     '{"true": "It names somewhere outside those countries, or asks about another country''s culture, history or travel.",
       "false": "Every place it names is in those countries, or it names no place at all."}',
     'shadow', 'AA-694 (Ms. Thư ticket 04, loose rule). A confident yes drops the question from landing.', 'migration-185'),
    ('a3_question_about_here', 'a3_question_scope', 'noul',
     'A travel brand writes for people planning a trip to the countries given. Does this question name a place in those countries, or an activity, custom or subject particular to them?',
     '{"true": "It names a specific place, landmark, region or city in those countries, or an activity, food, custom or subject that belongs to them in particular.",
       "false": "It names no place at all and could be asked about any country, or it names somewhere outside those countries. A question about a kind of thing rather than a particular thing (a hot spring, a market, a peninsula) is out."}',
     'shadow', 'AA-694 (Ms. Thư ticket 04, strict rule; generic questions are out — Q3). A confident no drops the question.', 'migration-185')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('185', now(), 'AA-694: country-scope Jev questions a3_question_foreign / a3_question_about_here (shadow)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
