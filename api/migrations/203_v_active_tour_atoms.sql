-- Migration 203: AA-713 — atoms follow their tour's Master Content status.
--
-- Problem: setting a tour to inactive/trashed (gold_aa_internal.published_tours.master_status)
-- never touched its atoms. The A3 reads (segment matching, atom ranking, route detection) filtered
-- only on the atom's own flags (NOT deleted AND NOT is_empty_marker), never on master_status, so an
-- inactivated tour kept feeding segments, ranking (said/demand/recurrence) and tenant slates.
--
-- Fix: one view that is the ONLY active-atom source. INNER JOIN to published_tours drops the row
-- outright for a non-active or soft-deleted master (v_trip_registry became a LEFT JOIN in 195 and
-- cannot be reused — it keeps the row with nulled brand fields). The four A3 queries read this view
-- instead of tour_atoms directly; a status change then re-runs ranking + route (DELETE+INSERT /
-- supersede), which evicts the now-inactive atoms from the caches the Slate reads.
--
-- A tour with NO published_tours row yet (ingested, not published) has no atoms in A3 anyway, so an
-- INNER JOIN loses nothing real. owner_scope/text/evidence/etc. stay available for every reader.

BEGIN;

CREATE OR REPLACE VIEW acp_contract.v_active_tour_atoms AS
SELECT ta.*
FROM acp_contract.tour_atoms ta
JOIN gold_aa_internal.published_tours pt
  ON pt.tour_id = ta.tour_id
 AND pt.master_status = 'active'
 AND pt.deleted_at IS NULL
WHERE NOT ta.deleted
  AND NOT ta.is_empty_marker;

GRANT SELECT ON acp_contract.v_active_tour_atoms TO aa_app_user;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('203', now(), 'AA-713: v_active_tour_atoms — atom reads follow published_tours.master_status')
ON CONFLICT (version) DO NOTHING;

COMMIT;
