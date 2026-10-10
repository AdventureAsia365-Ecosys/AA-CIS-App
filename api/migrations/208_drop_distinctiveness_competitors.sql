-- Migration 208: AA-754 step 4 — drop the atom `distinctiveness` column and the tenant Competitors
-- tables after the code stopped reading/writing them (PR #622, deployed as api :500 / worker :55).
--
-- DRY RUN (S223, 10/10/2026): the only object depending on tour_atoms.distinctiveness is the view
-- acp_contract.v_active_tour_atoms (migration 203, `SELECT ta.*` — Postgres expands * at creation, so
-- the column cannot be dropped under it); one index idx_tour_atoms_distinctiveness; no FK points at
-- either competitor table (competitor_index_cache 4 rows, competitor_inputs 0 rows).
-- Restore snapshot: s3://aa-cis-bronze-005097885195/scripts/restore/s223_aa754_distinctiveness_competitors.json
--
-- The view is dropped and recreated with the identical definition (no CASCADE: if anything new
-- depends on it, the migration fails loudly instead of silently dropping it).

BEGIN;

DROP VIEW IF EXISTS acp_contract.v_active_tour_atoms;

DROP INDEX IF EXISTS acp_contract.idx_tour_atoms_distinctiveness;
ALTER TABLE acp_contract.tour_atoms DROP COLUMN IF EXISTS distinctiveness;

CREATE VIEW acp_contract.v_active_tour_atoms AS
SELECT ta.*
FROM acp_contract.tour_atoms ta
JOIN gold_aa_internal.published_tours pt
  ON pt.tour_id = ta.tour_id
 AND pt.master_status = 'active'
 AND pt.deleted_at IS NULL
WHERE NOT ta.deleted
  AND NOT ta.is_empty_marker;

GRANT SELECT ON acp_contract.v_active_tour_atoms TO aa_app_user;

DROP TABLE IF EXISTS acp_shared.competitor_index_cache;
DROP TABLE IF EXISTS acp_silver_s2.competitor_inputs;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('208', now(), 'AA-754: drop tour_atoms.distinctiveness (+ index, view recreated) and the Competitors tables')
ON CONFLICT (version) DO NOTHING;

COMMIT;
