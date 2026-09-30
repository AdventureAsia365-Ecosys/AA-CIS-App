-- Migration 190: AA-694 A3-2 — reword a3_activity_type to match what the transit rule means.
--
-- classify_exclusion()'s "transit" (ported from Ms. Thư's transit-verbs.toml, ADR 0019/0020) is "not something
-- to write about": getting between places AND the frame around the trip (check-in/out, a generic overnight or
-- meal, free time, briefings and meetings). Migration 189's criteria put meals and stays under "experience", so
-- Jev picked "experience" for "stay overnight" / "eat lunch" / "attend welcome meeting" (S205 sample). Labels
-- keep their keys (transit / experience) so code is unchanged; the new wording hash drops the cached Verdicts.
-- Still shadow. Idempotent.

BEGIN;

UPDATE shared.decision_question
SET instructions = 'Is this moment of a trip something a travel writer could write about at this place, or trip logistics?',
    criteria = '{"transit": "Trip logistics: getting between places (travel, a transfer, a flight, a drive, a border crossing, passing through on the way, arriving, departing), checking in or out, a plain overnight stay, a plain meal, free time, or a briefing, meeting or welcome/farewell gathering.",
                 "experience": "Something to do or see at this place: a visit, a sight, an activity, a ceremony or performance, a named or distinctive meal or stay (a homestay, a ger camp, a local dish), or a journey that is itself the activity (a trek, a cycling stage, a river cruise)."}',
    notes = 'AA-694 A3-2: asked only where the transit rule and the atoms'' activity_type disagree; a confident pick decides the exclusion once enforced. Reworded in 190 to match the rule''s meaning (logistics incl. plain meals/stays/meetings).',
    updated_at = now(), updated_by = 'migration-190'
WHERE question_key = 'a3_activity_type';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('190', now(), 'AA-694: reword a3_activity_type to the transit rule''s meaning (logistics vs experience)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
