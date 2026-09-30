-- Migration 189: AA-694 A3-2 — the Segment activity-type Jev Question, in SHADOW.
--
-- Design: docs/architecture/at-series-v2-design.md §4.2 A3-2.
-- a3_activity_type: asked in resolve_exclusions() (atom_ranking.py) only for a Segment whose verb rule
--   (classify_exclusion -> transit) and its atoms' activity_type (majority transit) disagree. Once enforced,
--   a confident pick decides whether the Segment is excluded as transit (Score, landing, Route).
--   State = {moment}. Platform content only.
-- Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a3_activity_type', 'a3_segment_type', 'choice',
     'Is this moment of a trip getting from one place to another, or something the traveller does at a place?',
     '{"transit": "Travel, a transfer, a flight, arrival, departure, check-in or check-out: moving between places or logistics.",
       "experience": "An activity, visit, meal, stay or sight at a place, including an activity that is itself a journey such as a trek, a ride or a cruise."}',
     'shadow', 'AA-694 A3-2: asked only where the transit verb rule and the atoms'' activity_type disagree; a confident pick decides the transit exclusion once enforced.',
     'migration-189')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('189', now(), 'AA-694: A3 segment activity-type Jev question (shadow)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
