-- Migration 180: AA-674 — let AA-TripPlanner-Web read the atoms of published tours.
--
-- TripPlanner rebuilds tripplanner.itinerary_components (the pinnable place + activity per tour day
-- that its map, search and "suggest next" use) from acp_contract.tour_atoms, in its assembly Lambda
-- as the `tripplanner` role (AA-TripPlanner-Web backend/extraction). Today the role has no access to
-- acp_contract, and the components on Dev all belong to the pre-reset tour set (0 rows for the 121
-- active tours), so the Tour Graph (AA-673) has no activities per stop.
-- Least privilege: USAGE on the schema, SELECT on the columns the mapping reads (not the editorial
-- fields: hooks, persona fit, media, usage log, notes, evidence).
--
-- Additive; apply before the TripPlanner component extraction runs.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tripplanner') THEN
        GRANT USAGE ON SCHEMA acp_contract TO tripplanner;
        GRANT SELECT (atom_id, tour_id, owner_scope, itinerary_day, place, activity_type, action, text,
                      season_note, deleted, is_empty_marker)
            ON acp_contract.tour_atoms TO tripplanner;
    END IF;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('180', now(), 'AA-674: tripplanner may read tour_atoms place/activity columns')
ON CONFLICT (version) DO NOTHING;

COMMIT;
