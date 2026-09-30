-- Migration 194: AA-701 — T10 gate Jev Questions for allow-listed test tenants, in SHADOW (observe only).
--
-- Design: docs/architecture/at-series-v2-design.md §4.3 (T10-1 … T10-5). Asked by
-- services/acp_content_writing/jev_observe.py after each T9 attempt's run_quality_gates(); answers land in
-- decision_log next to the gate results and change nothing. Calibrated from the rerun, then wired into each gate.
-- decide() skips non-allow-listed tenants (design C2); t10_same_piece is asked only when BOTH tenants are allowed.
--   t10_rubric_met        {post, criterion}          — per F8 rubric item (goal framework)
--   t10_brand_voice       {post, brand}              — F9 style half
--   t10_cta_clear         {post, call_to_action}     — F9 CTA half
--   t10_same_piece        {post_a, post_b, cosine}   — nearest other-tenant piece at cosine >= 0.85
--   t10_offer_as_certain  {offered_moment, sentence} — each sentence gate_promises_an_option flags
--   t10_faq_restates      {body, faq_answer}         — each FAQ answer of a blog piece
-- Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('t10_rubric_met', 't10_gates', 'noul',
     'Does this social media post meet this criterion?',
     '{"true": "A reader can point to the part of the post that does what the criterion asks.",
       "false": "Nothing in the post does what the criterion asks, or it only gestures at it."}',
     'shadow', 'AA-701 T10-1: per F8 framework rubric item; would replace the GPT F8 call once calibrated.',
     'migration-194'),
    ('t10_brand_voice', 't10_gates', 'noul',
     'Does this post read in the voice the brand describes, without generic AI travel wording?',
     '{"true": "The tone and word choice match the brand description; no stock phrases like hidden gem, breathtaking, embark on a journey.",
       "false": "The tone clashes with the brand description, or the post leans on generic AI travel phrases."}',
     'shadow', 'AA-701 T10-2: F9 style half (a warn today).',
     'migration-194'),
    ('t10_cta_clear', 't10_gates', 'noul',
     'Does the post carry this call to action as one clear action for the reader?',
     '{"true": "The call to action appears and reads as a single, unambiguous next step.",
       "false": "The call to action is missing, reworded beyond recognition, or buried in a vague sign-off."}',
     'shadow', 'AA-701 T10-2: F9 CTA half (blocking today).',
     'migration-194'),
    ('t10_same_piece', 't10_gates', 'noul',
     'Would a reader who saw both posts think they are the same post, lightly reworded?',
     '{"true": "Same structure, same points in the same order, much of the same wording.",
       "false": "Same place or trip, but a different angle, different points, or clearly different writing."}',
     'shadow', 'AA-701 T10-3: cross-tenant cannibalization confirm (gate blocks at cosine 0.92; asked from 0.85).',
     'migration-194'),
    ('t10_offer_as_certain', 't10_gates', 'noul',
     'Does this sentence tell the reader they will definitely do the offered (optional or extra-cost) moment?',
     '{"true": "The sentence states the optional moment as a fixed part of the trip the reader will do.",
       "false": "The sentence is about something else, or presents the moment as a choice, a possibility or an extra."}',
     'shadow', 'AA-701 T10-4: per sentence gate_promises_an_option flags (known false-positive surface).',
     'migration-194'),
    ('t10_faq_restates', 't10_gates', 'noul',
     'Does this FAQ answer only repeat what the post body already says?',
     '{"true": "Everything in the answer is already in the body; it adds nothing a reader would not already know.",
       "false": "The answer adds at least one useful detail or angle the body does not give."}',
     'shadow', 'AA-701 T10-5: per FAQ answer of a blog piece (F7 heuristic uses token overlap > 0.85).',
     'migration-194')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('194', now(), 'AA-701: T10 gate Jev questions (shadow, observe only)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
