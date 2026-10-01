-- ============================================================================
-- Clean rerun — reset derived data, keep raw_tours (AA-653 / AA-594)  |  S207, 2026-10-01
-- ============================================================================
-- Replaces docs/audit-cis-g3-reset.sql (S183, root repo) — that script predates migrations
-- 153–195 (job runner, Jev, research, embeddings, Debate cache, TripPlanner).
--
-- KEEP: raw_tours (only pipeline_status reset), raw_sources, upload_staging, every config/cache
--   table (tenants, brand rules, tenant_config, competitor_index_cache, facts, destinations,
--   search_demand + segment_research_log + question_embedding = PAID research cache), every log
--   (llm_call_log, dfs_call_log, decision_log, job, pipeline_runs, notifications, audit_log),
--   Jev config (decision_question, allow-list), model catalog/role config, and ALL tripplanner.*
--   (no FK into CIS tours; re-extract after the CIS rerun).
-- DELETE: everything derived from raw_tours by S1 / A-series / T-series.
-- Order = child → parent from the live FK graph (pg_constraint, 01/10/2026). No CASCADE used.
--
-- Run through the ECS exec runner, never by hand: part 1 alone first (read-only counts), then
-- part 2 only after Nghiệp approves the counts. Part 2 runs in ONE transaction and checks
-- the post-delete counts before COMMIT.
-- ============================================================================

-- ############################################################################
-- PART 1 — DRY RUN (read-only)
-- ############################################################################
SELECT 'acp_shared.publish_log' AS tbl, count(*) FROM acp_shared.publish_log
UNION ALL SELECT 'acp_shared.content_piece', count(*) FROM acp_shared.content_piece
UNION ALL SELECT 'acp_shared.angle_gate_option', count(*) FROM acp_shared.angle_gate_option
UNION ALL SELECT 'acp_shared.angle_gate_request', count(*) FROM acp_shared.angle_gate_request
UNION ALL SELECT 'acp_shared.subject', count(*) FROM acp_shared.subject
UNION ALL SELECT 'acp_shared.debate_brand_fit_cache', count(*) FROM acp_shared.debate_brand_fit_cache
UNION ALL SELECT 'acp_shared.tenant_atom_state', count(*) FROM acp_shared.tenant_atom_state
UNION ALL SELECT 'acp_contract.route_pick', count(*) FROM acp_contract.route_pick
UNION ALL SELECT 'acp_contract.atom_ranking', count(*) FROM acp_contract.atom_ranking
UNION ALL SELECT 'acp_contract.atom_embedding', count(*) FROM acp_contract.atom_embedding
UNION ALL SELECT 'acp_contract.atom_matches', count(*) FROM acp_contract.atom_matches
UNION ALL SELECT 'acp_contract.atom_segment_member', count(*) FROM acp_contract.atom_segment_member
UNION ALL SELECT 'acp_contract.atom_segment_alias', count(*) FROM acp_contract.atom_segment_alias
UNION ALL SELECT 'acp_contract.route', count(*) FROM acp_contract.route
UNION ALL SELECT 'acp_contract.hub', count(*) FROM acp_contract.hub
UNION ALL SELECT 'acp_contract.atom_segment', count(*) FROM acp_contract.atom_segment
UNION ALL SELECT 'acp_contract.atomize_day_fingerprint', count(*) FROM acp_contract.atomize_day_fingerprint
UNION ALL SELECT 'acp_contract.s1_from_atom_runs', count(*) FROM acp_contract.s1_from_atom_runs
UNION ALL SELECT 'acp_contract.tour_atoms', count(*) FROM acp_contract.tour_atoms
UNION ALL SELECT 'silver_aa_internal.review_queue', count(*) FROM silver_aa_internal.review_queue
UNION ALL SELECT 'silver_aa_internal.quality_scores', count(*) FROM silver_aa_internal.quality_scores
UNION ALL SELECT 'silver_aa_internal.seo_context (old format only)', count(*) FROM silver_aa_internal.seo_context WHERE cache_key IS NULL OR cache_key NOT LIKE '%:ideas_v3'
UNION ALL SELECT 'gold_aa_internal.tenant_tour_versions', count(*) FROM gold_aa_internal.tenant_tour_versions
UNION ALL SELECT 'gold_aa_internal.published_tours', count(*) FROM gold_aa_internal.published_tours
UNION ALL SELECT 'silver_aa_internal.generated_content', count(*) FROM silver_aa_internal.generated_content
UNION ALL SELECT 'raw_tours (UPDATE pipeline_status, NOT deleted)', count(*)
          FROM silver_aa_internal.raw_tours WHERE pipeline_status <> 'ingested';

-- ############################################################################
-- PART 2 — RESET (one transaction; the runner COMMITs only if the checks below pass)
-- ############################################################################
-- BEGIN;
-- -- Tenant / social
-- DELETE FROM acp_shared.publish_log;
-- DELETE FROM acp_shared.content_piece;
-- DELETE FROM acp_shared.angle_gate_option;
-- DELETE FROM acp_shared.angle_gate_request;
-- DELETE FROM acp_shared.subject;
-- DELETE FROM acp_shared.debate_brand_fit_cache;
-- DELETE FROM acp_shared.tenant_atom_state;
-- DELETE FROM acp_contract.route_pick;
-- -- Atoms / segments / routes / hubs
-- DELETE FROM acp_contract.atom_ranking;
-- DELETE FROM acp_contract.atom_embedding;
-- DELETE FROM acp_contract.atom_matches;
-- DELETE FROM acp_contract.atom_segment_member;
-- DELETE FROM acp_contract.atom_segment_alias;
-- DELETE FROM acp_contract.route;
-- DELETE FROM acp_contract.hub;
-- DELETE FROM acp_contract.atom_segment;
-- DELETE FROM acp_contract.atomize_day_fingerprint;
-- DELETE FROM acp_contract.s1_from_atom_runs;
-- DELETE FROM acp_contract.tour_atoms;
-- -- Master content (review_queue before tenant_tour_versions — FK)
-- DELETE FROM silver_aa_internal.review_queue;
-- DELETE FROM silver_aa_internal.quality_scores;
-- -- keep the S1 long-term SEO cache (AA-653): rows in the current format are reused for a year
-- DELETE FROM silver_aa_internal.seo_context WHERE cache_key IS NULL OR cache_key NOT LIKE '%:ideas_v3';
-- DELETE FROM gold_aa_internal.tenant_tour_versions;
-- DELETE FROM gold_aa_internal.published_tours;
-- DELETE FROM silver_aa_internal.generated_content;
-- -- raw_tours: keep rows, reset status (lifecycle_stage/source_status untouched)
-- UPDATE silver_aa_internal.raw_tours SET pipeline_status = 'ingested' WHERE pipeline_status <> 'ingested';
-- -- Checks (runner asserts): every table above = 0; raw_tours = 793; search_demand, destinations,
-- -- tenants, decision_log, llm_call_log unchanged from part 1.
-- COMMIT;
