-- Migration 200: AA-708 — read views for AA photos, the contract other apps use.
--
-- CIS owns shared.place_photo (migration 198) and the S3 files. TripPlanner, the tenant portal and
-- AA-Booking read photos ONLY through these two views, never the table: its columns can change, the
-- views stay. `path` is relative to the CIS public API base (https://api-cis.lumiguides.it.com on
-- Dev): `{base}{path}?size=large|small` returns the image (302 to S3; CloudFront later, same path).
-- Rejected, unmatched and file-less photos are excluded; manual assignments sort first.

BEGIN;

CREATE OR REPLACE VIEW shared.v_tour_photos AS
SELECT p.tour_id, p.id AS photo_id, '/content/photos/' || p.id::text AS path, p.place_label,
       p.destination_id, p.width, p.height, p.match_source, p.updated_at,
       row_number() OVER (PARTITION BY p.tour_id
                          ORDER BY (p.match_source = 'manual') DESC, p.file_name) AS position
  FROM shared.place_photo p
 WHERE p.status = 'matched' AND p.tour_id IS NOT NULL AND p.s3_key_large IS NOT NULL;

CREATE OR REPLACE VIEW shared.v_destination_photos AS
SELECT p.destination_id, p.id AS photo_id, '/content/photos/' || p.id::text AS path, p.place_label,
       p.tour_id, p.width, p.height, p.match_source, p.updated_at,
       row_number() OVER (PARTITION BY p.destination_id
                          ORDER BY (p.match_source = 'manual') DESC, p.file_name) AS position
  FROM shared.place_photo p
 WHERE p.status = 'matched' AND p.destination_id IS NOT NULL AND p.s3_key_large IS NOT NULL;

COMMENT ON VIEW shared.v_tour_photos IS 'AA-708 — matched AA photos per tour (read contract for TripPlanner / portal / AA-Booking).';
COMMENT ON VIEW shared.v_destination_photos IS 'AA-708 — matched AA photos per destination (read contract; cover = position 1).';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('200', now(), 'AA-708: shared.v_tour_photos / v_destination_photos (photo read contract)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
