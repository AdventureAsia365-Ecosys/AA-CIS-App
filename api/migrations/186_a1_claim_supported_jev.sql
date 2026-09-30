-- Migration 186: AA-691 — the A1 master-content grounding Jev Question, seeded in SHADOW.
--
-- Design: docs/architecture/at-series-v2-design.md §4.1 A1-1 / §8 Q8. One sentence of the S1
-- rewrite (subtitle / summary / highlights / itineraries) against the raw source tour. A confident
-- "no" (reject zone, once enforced with a Calibration Record) sends the sentence back to flag_fix
-- quoted as UNSUPPORTED_CLAIM; a confident "yes" clears a deterministic novel-number hit (unit
-- conversion, sums). State = {source, sentence}. Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a1_claim_supported', 's1_grounding', 'noul',
     'A travel writer rewrote a tour from the source text. Is every fact in this sentence stated or clearly implied by the source?',
     '{"true": "Every place, activity, number, meal, service, timing, record or historical fact in the sentence is in the source, possibly paraphrased, converted to other units or added up. Mood and colour about something the source names count as supported.",
       "false": "The sentence states a fact the source does not give: a place, activity, number, meal, service, timing, record or historical claim a traveller could rely on."}',
     'shadow', 'AA-691 A1-1: asked per sentence of the S1 rewrite; a confident no is quoted to flag_fix as UNSUPPORTED_CLAIM.',
     'migration-186')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('186', now(), 'AA-691: A1 grounding Jev question a1_claim_supported (shadow)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
