-- Migration 193: AA-700 — T7 / T8 / T9 Jev Questions for allow-listed test tenants, in SHADOW.
--
-- Design: docs/architecture/at-series-v2-design.md §4.3 (T7-1, T8-1, T9-1). Tenant content: decide() asks TypeSafe
-- only for tenants in shared.jev_tenant_allowlist (design C2); others log zone='skipped'. S206: the test tenants hold
-- too little data to calibrate now (5 rewrites, 3 pieces), so these run in shadow and are calibrated from the rerun.
--   t7_topic_fits_brand   — Debate (acp_shared/debate.py), per candidate topic before the Luna brand-fit call.
--                           State = {brand, topic{place, action, evidence}}. Enforced: accept passes, reject cuts,
--                           grey → Luna as today.
--   t8_angle_answers      — Angle Gate (acp_angle_gate/service.py), per (angle, PAA question the angle claims and
--                           the code matched verbatim). State = {angle{name, why_it_works}, question}. Enforced: a
--                           confident no drops that answer before the ranking.
--   t9_fact_relevant      — T9 write (acp_content_writing/service.py), per Facts Entry before it enters the writer
--                           prompt. State = {moment, angle, trip, fact}. Enforced: a confident no leaves it out.
-- Additive.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('t7_topic_fits_brand', 't7_debate', 'noul',
     'Would this brand''s travellers genuinely want this travel experience?',
     '{"true": "The experience is a natural match for who the brand is for and what they want, judged from the brand profile and the evidence.",
       "false": "The experience clashes with who the brand is for or what they want, or the evidence gives the brand nothing its travellers would care about."}',
     'shadow', 'AA-700 T7-1: Debate prefilter before the Luna brand-fit call; once enforced, only the grey zone reaches Luna.',
     'migration-193'),
    ('t8_angle_answers', 't8_angle_rank', 'noul',
     'Would a post written from this angle actually answer this traveller question?',
     '{"true": "The angle is about what the question asks, so a post built on it would give the reader the answer.",
       "false": "The angle only shares a place or a word with the question, or would leave the question unanswered."}',
     'shadow', 'AA-700 T8-1: verifies each PAA answer an angle claims before rank_angles counts it.',
     'migration-193'),
    ('t9_fact_relevant', 't9_facts', 'noul',
     'Is this fact useful to a post about this travel moment, from this angle?',
     '{"true": "A reader of this post would benefit from the fact: it is about this place, this trip, or travelling there (season, entry, price, transfer).",
       "false": "The fact is about another place or topic and would not belong in this post."}',
     'shadow', 'AA-700 T9-1: per Facts Entry before it is injected into the T9 writer prompt.',
     'migration-193')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('193', now(), 'AA-700: T-series Jev questions t7_topic_fits_brand, t8_angle_answers, t9_fact_relevant (shadow)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
