-- Migration 211: AA-756 (S224) — Jev question isolating type-B writer claims, seeded in SHADOW.
--
-- a1_claim_supported ("is every fact in the source?") flags both kinds of addition the writer can
-- make: A = short well-known background about a place the source names (allowed by writer rule 6,
-- #629) and B = a promise about THIS tour the source does not make (forbidden). This question asks
-- only about B, in the same s1_grounding call (same state {source, sentence}), so the B rate can be
-- measured before/after writer changes. Shadow: logged only, no effect on grounding. Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a1_promise_unsupported', 's1_grounding', 'noul',
     'A travel writer rewrote a tour from the source text. Does this sentence promise the traveller something about THIS tour that the source does not state?',
     '{"true": "The sentence says the tour includes or delivers something the source does not state: an activity or experience, food or drink, a service (guide, orientation, support), a transport mode or travel time, a clock-time, a view or wildlife sighting, an accommodation feature, or an inclusion.",
       "false": "Everything the sentence says will happen on this tour is in the source. General background about a place the source names (what it is, its history, architecture or setting) does not count as a promise."}',
     'shadow', 'AA-756 S224: type-B writer additions (writer rule 6); asked with a1_claim_supported in s1_grounding. Shadow — measurement only.',
     'migration-211')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('211', now(), 'AA-756: a1_promise_unsupported Jev question (shadow) — type-B writer promises')
ON CONFLICT (version) DO NOTHING;

COMMIT;
