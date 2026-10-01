-- Migration 201: ADR 0002 (root) — only CIS writes content; other apps read views.
--
-- The TripPlanner role was the only non-CIS role with write rights on content (checked 01/10/2026):
-- INSERT, UPDATE on shared.destinations. It keeps INSERT on shared.llm_call_log (telemetry, allowed
-- by the ADR) and full rights on its own schema `tripplanner`. It gets SELECT on the AA-708 photo
-- views, the read contract for photos. Guarded: a database without the role is left unchanged.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tripplanner') THEN
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON shared.destinations FROM tripplanner;
        GRANT SELECT ON shared.v_tour_photos, shared.v_destination_photos TO tripplanner;
    END IF;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('201', now(), 'ADR 0002: tripplanner role read-only on shared.destinations; SELECT on photo views')
ON CONFLICT (version) DO NOTHING;

COMMIT;
