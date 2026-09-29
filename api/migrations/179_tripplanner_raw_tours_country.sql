-- Migration 179: AA-675 — let AA-TripPlanner-Web read a published tour's country.
--
-- TripPlanner's tour-day extraction (AA-TripPlanner-Web backend/extraction/tour_days.py, run in the
-- assembly Lambda as the `tripplanner` role) geocodes each day's overnight place with the tour's
-- country as the region hint. published_tours has no country column (join raw_tours on tour_id,
-- see the CIS CLAUDE.md rule), and the `tripplanner` role had no access to silver_aa_internal.
-- Least privilege: USAGE on the schema, SELECT on the two columns only (not the source content).
--
-- Additive; apply before the TripPlanner extraction runs.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tripplanner') THEN
        GRANT USAGE ON SCHEMA silver_aa_internal TO tripplanner;
        GRANT SELECT (tour_id, country) ON silver_aa_internal.raw_tours TO tripplanner;
    END IF;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('179', now(), 'AA-675: tripplanner may read raw_tours.tour_id/country')
ON CONFLICT (version) DO NOTHING;

COMMIT;
