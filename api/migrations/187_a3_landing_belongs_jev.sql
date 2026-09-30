-- Migration 187: AA-694 (A3-5) — the PAA landing Jev Question, seeded in SHADOW.
--
-- Design: docs/architecture/at-series-v2-design.md §4.2 A3-5 / §8 Q5. Ms. Thư's ticket 05 wording.
-- Asked once per (Segment moment, candidate question) before landing — every atom of a Segment
-- shares its canonical place + action, so the Verdict does not depend on which atom the question
-- lands on. State = {query, moment} with moment = "<place> — <action>". A confident no (reject zone,
-- once enforced with a Calibration Record) keeps the question from counting for that Segment.
-- Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a3_landing_belongs', 'a3_question_landing', 'noul',
     'Would someone searching this query be well served by an article about this moment on a guided trip?',
     '{"true": "The query asks about this place (or something at it) or about doing this activity here, so an article about the moment answers it.",
       "false": "The query is about a different place or thing (even one of the same kind), or about a topic this moment does not cover."}',
     'shadow', 'AA-694 A3-5 (Ms. Thư ticket 05). Asked per (Segment moment, question) before landing; a confident no stops the question counting.',
     'migration-187')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('187', now(), 'AA-694: A3 PAA landing Jev question a3_landing_belongs (shadow)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
