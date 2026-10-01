-- Migration 195: acp_contract.v_trip_registry — active sources only (AA-702)
--
-- The view excluded only 'trashed' sources, so 'superseded' rows (older re-uploads and the S206
-- near-duplicates) were still offered to every consumer: tenant tour pool (v1_tours), trip pages,
-- N4/N5/N6 planning, marketplace estimates. Measured on Dev 01/10/2026: 761 view rows, 25 of them
-- superseded; 3 of those carry an active master that duplicates the active version's tour.
--
-- source_status is NOT NULL DEFAULT 'active' (migration 052), so `= 'active'` is the complete
-- rule. A superseded row comes back by promoting it in Dup Review (source_status -> 'active').
-- Columns, names and order unchanged from migration 087 — only the WHERE clause changes.

BEGIN;

CREATE OR REPLACE VIEW acp_contract.v_trip_registry AS
SELECT
    rt.tour_id              AS id,
    rt.sku                  AS sku,
    rt.src_name              AS name,
    pt.aa_name               AS aa_name,
    rt.duration               AS duration_raw,
    rt.period                  AS period,
    rt.price_raw               AS price_raw,
    rt.country                 AS destination,
    rt.src_itineraries        AS itinerary_source,
    pt.aa_itineraries         AS itinerary_brand,
    pt.aa_summary, pt.aa_highlights,
    pt.seo_title, pt.seo_meta, pt.seo_keywords_used,
    pt.quality_score,
    pt.content_embedding,
    ttp.url                    AS trip_url,
    ttp.url_alive,
    rt.inclusions               AS inclusions,
    rt.exclusions               AS exclusions,
    rt.tenant_id                AS tenant_id,
    rt.lifecycle_stage          AS lifecycle_stage
FROM silver_aa_internal.raw_tours rt
LEFT JOIN gold_aa_internal.published_tours pt
    ON pt.tour_id = rt.tour_id
   AND pt.master_status = 'active'
   AND pt.deleted_at IS NULL
LEFT JOIN acp_deliver.tenant_tour_pages ttp ON ttp.tour_id = rt.tour_id
WHERE rt.source_status = 'active'
  AND rt.src_itineraries IS NOT NULL AND trim(rt.src_itineraries) != '';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('195', now(), 'AA-702: v_trip_registry excludes superseded sources (active only)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
