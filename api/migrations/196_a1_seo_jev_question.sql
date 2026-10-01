-- Migration 196: AA-706 — Jev question for S1 keyword ideas, seeded in SHADOW mode.
--
-- Asked by the s1_seo_prefetch job for each tour's candidate DataForSEO keyword ideas (up to 30 per
-- tour). In shadow it only logs verdicts — the substring relevance rule (AA-702) still decides; once
-- calibrated and set to `enforce` on /admin/decisions, a confident accept keeps an idea the substring
-- rule missed (synonyms) and a confident reject drops one it kept.

BEGIN;

INSERT INTO shared.decision_question (question_key, stage, kind, instructions, criteria, mode, notes, updated_by)
VALUES
    ('a1_keyword_about_tour', 'a1_seo', 'noul',
     'Would a traveller searching this keyword be looking for this particular tour — its places, route or activity — rather than something else in the country?',
     '{"true": "The keyword names a place, route or activity this tour actually covers (other names for the same place count).",
       "false": "The keyword is about another place or region, a generic country search, or something this tour does not do."}',
     'shadow', 'AA-706: asked in the s1_seo_prefetch job before ideas are stored; state = {tour, country, title_places, activity, keyword}.',
     'migration-196')
ON CONFLICT (question_key) DO NOTHING;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('196', now(), 'AA-706: a1_keyword_about_tour Jev question (shadow)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
