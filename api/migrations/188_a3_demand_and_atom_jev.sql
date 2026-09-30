-- Migration 188: AA-694 — the A3-6 demand-ownership and A3-1 atom-grounding Jev Questions, in SHADOW.
--
-- Design: docs/architecture/at-series-v2-design.md §4.2 A3-1 / A3-6.
-- a3_demand_belongs: asked on a Segment's best-claimed keyword per market in run_atom_ranking(); a confident
--   no (once enforced) moves to the next candidate keyword. State = {keyword, moment}.
-- a3_atom_in_text: asked per extracted atom before it is stored (t5_atomize, per day); a confident no
--   (once enforced) drops the atom. State = {day_text, atom}. Tenant content only for allow-listed tenants.
-- Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a3_demand_belongs', 'a3_demand', 'noul',
     'Is someone searching this keyword looking for this particular moment of a trip: this place, something at it, or this activity here?',
     '{"true": "The keyword names this place (or a spelling of it), something at it, or doing this activity here.",
       "false": "The keyword names a wider area around this place (the city or country it sits in), a different place, or only the kind of thing it is."}',
     'shadow', 'AA-694 A3-6: a confident no moves the Segment to its next claimed keyword for that market.', 'migration-188'),
    ('a3_atom_in_text', 'a3_atomize', 'noul',
     'Is this place-and-activity stated in this day''s itinerary text?',
     '{"true": "The day text names this place (or clearly the same place) and says the traveller does this there.",
       "false": "The place or the activity is not in this day''s text: invented, taken from another day, or only implied by a general description."}',
     'shadow', 'AA-694 A3-1: asked per atom before it is stored; a confident no drops the atom.', 'migration-188')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('188', now(), 'AA-694: A3 demand-ownership and atom-grounding Jev questions (shadow)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
