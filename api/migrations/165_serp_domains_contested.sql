-- Migration 165: AA-631 (Debate stage, standard #1 "contested") — acp_contract.search_demand.
-- serp_domains + acp_contract.atom_segment.contested/contested_computed_at cache.
--
-- Context: `services/seo_intelligence/dataforseo_client.py::_serp_advanced()` already fetches
-- the full DFS SERP response (organic + people_also_ask + related_searches items) for every
-- keyword this platform researches -- today only people_also_ask/related_searches are read,
-- the `organic` items (ranked domain results) are fetched and thrown away. AA-631's `contested`
-- standard ("every operator has already written this") needs exactly that data -- how much of
-- a keyword's SERP is owned by "claims-everything" aggregator domains (Wikipedia/TripAdvisor/
-- etc, `everywhere_domains()`, platform-wide) -- at ZERO new DataForSEO cost (same call already
-- being made, just also keeping a field that used to be parsed away).
--
-- serp_domains stores the ranked organic domain list for a (keyword, market) -- the raw signal.
-- contested (on atom_segment, same cache pattern as questions_count, migration 163) is the
-- DERIVED per-Segment score computed from it via the same claim-by-name test _candidate_
-- questions()/_demand() already use -- cached here for the identical reason questions_count is:
-- so a tour-triggered recompute does not have to recompute Debate's contested score for every
-- OTHER platform Segment too. NULL means never computed OR invalidated (same "always
-- recompute regardless of scope" semantic AA-630 established for questions_count).

BEGIN;

ALTER TABLE acp_contract.search_demand
    ADD COLUMN serp_domains JSONB;

COMMENT ON COLUMN acp_contract.search_demand.serp_domains IS
    'AA-631 (Debate contested) -- ranked organic-result domains for this (keyword, market)''s '
    'SERP, e.g. ["wikipedia.org","tripadvisor.com",...] in rank order. Parsed from the same '
    'DFS serp/google/organic/live/advanced response people_also_ask already comes from -- no '
    'new DataForSEO call. NULL means never fetched (older rows, or a keyword whose SERP call '
    'predates this column).';

ALTER TABLE acp_contract.atom_segment
    ADD COLUMN contested REAL,
    ADD COLUMN contested_computed_at TIMESTAMPTZ;

COMMENT ON COLUMN acp_contract.atom_segment.contested IS
    'AA-631 (Debate standard #1) -- cached domain-share-by-aggregator score (0..1, higher = '
    'more "every operator has already written this") for the keyword(s) this Segment claims '
    'by name, same claim-by-name test _candidate_questions() uses. NULL means never computed '
    '(brand-new Segment) or invalidated (a claimed keyword''s serp_domains changed) -- same '
    '"always recompute regardless of scope" semantic questions_count (migration 163) and its '
    'invalidation (AA-630) already established; Debate''s recompute step reuses that exact '
    'pattern rather than inventing a second one.';

COMMENT ON COLUMN acp_contract.atom_segment.contested_computed_at IS
    'AA-631 -- when contested was last computed. Informational only, mirrors questions_
    computed_at (migration 163).';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('165', now(),
    'AA-631 (Debate contested): acp_contract.search_demand.serp_domains (ranked organic '
    'domains, zero new DFS cost) + acp_contract.atom_segment.contested/contested_computed_at '
    '(cached per-Segment domain-share score, same cache/invalidation pattern as questions_count)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
