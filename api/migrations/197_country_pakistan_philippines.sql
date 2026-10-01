-- Migration 197: AA-653 — add Pakistan and Philippines to chk_raw_tours_country.
--
-- The content team's Jira CON board holds raw tour sheets for Pakistan (18 tours, two suppliers)
-- and the Philippines (72 rows, T.R.I.P.S Travel + Philippines Starter Pack). Nghiep approved
-- uploading every tour missing from the system (S207, 01/10/2026). Every row was rejected by this
-- constraint on commit, so both countries are added. shared/country_resolver.py already maps
-- both names. NULL stays allowed.

BEGIN;

ALTER TABLE silver_aa_internal.raw_tours DROP CONSTRAINT IF EXISTS chk_raw_tours_country;

ALTER TABLE silver_aa_internal.raw_tours
    ADD CONSTRAINT chk_raw_tours_country CHECK (
        country IS NULL OR country = ANY (ARRAY[
            'Bhutan', 'Cambodia', 'China', 'India', 'Indonesia', 'Japan', 'Kyrgyzstan', 'Laos',
            'Malaysia', 'Mongolia', 'Myanmar', 'Nepal', 'Pakistan', 'Philippines', 'South Korea',
            'Sri Lanka', 'Taiwan', 'Thailand', 'Uzbekistan', 'Vietnam'
        ])
    );

COMMENT ON CONSTRAINT chk_raw_tours_country ON silver_aa_internal.raw_tours IS
    'AA-571 + AA-653 (migration 197) -- restricts country to AA''s confirmed operating-country '
    'list; Pakistan and Philippines added 01/10/2026. NULL allowed (missing, not invalid). Extend '
    'this list only after Nghiep confirms a new operating country -- do not guess.';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('197', now(), 'AA-653: chk_raw_tours_country adds Pakistan and Philippines (20 values)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
