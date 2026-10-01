-- Migration 202: AA-708 — keep the original photo file in S3.
--
-- The first syncs stored only 1600w / 600w WebP renditions, and the thumbnail-host download asked for
-- 2000 px. Originals on the CON board go up to 8256 x 5504 (14 MB, measured 01/10/2026); they are
-- needed for print, large banners and re-cropping. `s3_key_original` holds the untouched file
-- (photos/<country>/<file id>-orig.<ext>); rows without it are downloaded again once.

BEGIN;

ALTER TABLE shared.place_photo ADD COLUMN IF NOT EXISTS s3_key_original TEXT;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('202', now(), 'AA-708: shared.place_photo.s3_key_original (keep the original file)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
