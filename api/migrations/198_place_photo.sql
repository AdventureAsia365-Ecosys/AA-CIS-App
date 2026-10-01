-- Migration 198: AA-708 — shared.place_photo, AA marketing photos synced from Google Drive.
--
-- The content team keeps photos in public Google Drive folders linked from the Jira Contents
-- board (PHOTOS column): country folder → one folder per tour → images named by place
-- ("Olkhon Island1.jpeg"). The `photo_sync` job walks those folders, stores two WebP sizes in S3
-- (photos/<country>/<file id>-{1600,600}.webp) and records one row per Drive file here, matched to
-- a tour (folder name ↔ raw_tours.src_name) and/or a destination (file label ↔
-- shared.destinations.name, same country). The admin Photos page shows coverage and the unmatched
-- queue; a manual assignment (match_source='manual') is never overwritten by a later sync.

BEGIN;

CREATE TABLE IF NOT EXISTS shared.place_photo (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    drive_file_id     TEXT NOT NULL UNIQUE,
    drive_root_id     TEXT NOT NULL,
    folder_path       TEXT NOT NULL,          -- folder names under the country root, '/'-joined
    country           TEXT NOT NULL,
    tour_folder       TEXT,                   -- first folder under the country root (NULL = root)
    file_name         TEXT NOT NULL,
    place_label       TEXT,                   -- file name without extension / trailing digits
    tour_id           UUID REFERENCES silver_aa_internal.raw_tours(tour_id) ON DELETE SET NULL,
    destination_id    UUID REFERENCES shared.destinations(id) ON DELETE SET NULL,
    match_source      TEXT NOT NULL DEFAULT 'auto' CHECK (match_source IN ('auto', 'manual')),
    status            TEXT NOT NULL DEFAULT 'unmatched'
                      CHECK (status IN ('matched', 'unmatched', 'rejected', 'error')),
    s3_key_large      TEXT,
    s3_key_small      TEXT,
    width             INT,
    height            INT,
    bytes             BIGINT,
    sha256            TEXT,
    credit            TEXT,
    drive_modified_at TIMESTAMPTZ,
    error             TEXT,
    synced_at         TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS place_photo_country_status_idx ON shared.place_photo (country, status);
CREATE INDEX IF NOT EXISTS place_photo_tour_idx ON shared.place_photo (tour_id) WHERE tour_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS place_photo_destination_idx
    ON shared.place_photo (destination_id) WHERE destination_id IS NOT NULL;

COMMENT ON TABLE shared.place_photo IS
    'AA-708 — AA marketing photos from the Jira CON PHOTOS Drive folders, stored in S3 and matched '
    'to raw_tours / shared.destinations. Written by the photo_sync job and the admin Photos page.';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('198', now(), 'AA-708: shared.place_photo (Drive photo sync, tour/destination matching)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
