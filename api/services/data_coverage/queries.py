"""AA-992 — the SQL of the data coverage page (design §2), one statement per source, all counts.

Every statement returns ONE row of named integers (or a few named timestamps), takes ``$1`` = the AA master tenant
id where it filters by tenant, and is read as ``cis_app`` on ``app.state.pool`` (CIS base tables and the owner
contract views ``tripplanner.v_ops_*`` / ``booking.v_ops_*`` / ``booking.v_supply_*``) unless the name says
``TENANT_`` (read as ``aa_app_user`` through :func:`services.catalog_http.reader.catalog_connection`: the
``catalog.*`` views, which ``cis_app`` has no USAGE on). No statement returns an id, a name or free text, and none
reads ``shared.llm_call_log`` or ``shared.decision_log`` (the two big tables; guarded by the integration test).

Definitions (live tour, active atom, place atom, served destination) come from :mod:`.defs`; the statements are the
S252 audit's (``coverage.py``) with the sample arrays and per-country ROLLUPs dropped.
"""
from __future__ import annotations

from .defs import ACTIVE_ATOMS, LIVE, PLACE_ATOM, SERVED_DEST

# ── 1.1 Inputs (S0) ─────────────────────────────────────────────────────────────────────────────────────────────────
IN_FILES = """
SELECT count(*)::int AS files
  FROM silver_aa_internal.raw_sources WHERE tenant_id = $1::uuid"""

# Ingest batches = pipeline_runs rows written by services/ingestion/handler.process_file (s3_source_path under
# raw-inbox/). parsed = landed + staged (near-duplicates sent to upload_staging) + dropped (critic B1).
IN_BATCHES = """
WITH b AS (
    SELECT batch_id, status, ingest_details
      FROM shared.pipeline_runs
     WHERE tenant_id = $1::uuid AND s3_source_path LIKE 'raw-inbox/%')
SELECT count(*)::int AS batches,
       count(*) FILTER (WHERE ingest_details IS NULL)::int AS batches_without_details,
       count(*) FILTER (WHERE status = 'ingest_failed')::int AS batches_failed,
       coalesce(sum((ingest_details->>'rows_parsed')::int), 0)::int AS rows_parsed,
       coalesce(sum((ingest_details->>'rows_landed')::int), 0)::int AS rows_landed,
       coalesce(sum((ingest_details->>'rows_dropped')::int), 0)::int AS rows_dropped,
       (SELECT count(*) FROM silver_aa_internal.upload_staging s
         WHERE s.tenant_id = $1::uuid
           AND s.batch_id IN (SELECT b2.batch_id FROM b b2 WHERE b2.ingest_details IS NOT NULL))::int AS rows_staged
  FROM b"""

# One row per drop reason (pivoted in Python: the known A0 gate reasons by name, every other reason is an insert
# failure whose text is an exception message, so only its count leaves the database side).
IN_DROPS = """
SELECT d->>'reason' AS reason, sum((d->>'count')::int)::int AS n
  FROM shared.pipeline_runs r
 CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(r.ingest_details->'drops') = 'array'
                                              THEN r.ingest_details->'drops' ELSE '[]'::jsonb END) d
 WHERE r.tenant_id = $1::uuid AND r.s3_source_path LIKE 'raw-inbox/%'
 GROUP BY 1"""
DROP_REASONS = ("empty_itinerary", "duplicate_in_file", "duplicate_of_existing", "not_a_tour", "thin_itinerary")

IN_STAGING = """
SELECT count(*) FILTER (WHERE decision = 'pending')::int AS pending,
       count(*) FILTER (WHERE decision = 'pending' AND created_at < now() - interval '7 days')::int AS pending_over_7d,
       count(*) FILTER (WHERE decision <> 'pending')::int AS decided,
       count(*)::int AS staged
  FROM silver_aa_internal.upload_staging WHERE tenant_id = $1::uuid"""

# The S3 keys the ingest recorded, compared in Python with the raw-inbox listing (keys never leave the API).
IN_S3_PATHS = "SELECT s3_path FROM silver_aa_internal.raw_sources WHERE tenant_id = $1::uuid"

# ── 1.2 Raw tours (A0) ──────────────────────────────────────────────────────────────────────────────────────────────
RAW = """
SELECT count(*)::int AS rows,
       count(*) FILTER (WHERE source_status = 'trashed' OR deleted_at IS NOT NULL)::int AS trashed,
       count(*) FILTER (WHERE source_status = 'superseded' AND deleted_at IS NULL)::int AS superseded,
       count(*) FILTER (WHERE pipeline_status::text = 'hitl_rejected')::int AS hitl_rejected,
       count(*) FILTER (WHERE source_status = 'active' AND deleted_at IS NULL)::int AS live,
       count(*) FILTER (WHERE source_status = 'active' AND deleted_at IS NULL
                          AND (src_itineraries IS NULL OR btrim(src_itineraries) = ''))::int AS live_no_itinerary,
       count(*) FILTER (WHERE source_status = 'active' AND deleted_at IS NULL
                          AND (country_iso2 IS NULL OR country_raw_unresolved IS NOT NULL))::int
           AS live_country_unresolved,
       count(*) FILTER (WHERE source_status = 'active' AND deleted_at IS NULL
                          AND lifecycle_stage::text <> 'active')::int AS live_lifecycle_not_active
  FROM silver_aa_internal.raw_tours WHERE tenant_id = $1::uuid"""

# ── 1.3 S1 and Master (A1–A3) ───────────────────────────────────────────────────────────────────────────────────────
# The S1 page's floor (GET /admin/tours): live sources with a non-empty itinerary.
S1 = """
WITH e AS (
  SELECT EXISTS (SELECT 1 FROM gold_aa_internal.published_tours pt WHERE pt.tour_id = rt.tour_id
                    AND pt.master_status = 'active' AND pt.deleted_at IS NULL) AS has_master,
         EXISTS (SELECT 1 FROM silver_aa_internal.generated_content g WHERE g.tour_id = rt.tour_id) AS written,
         EXISTS (SELECT 1 FROM silver_aa_internal.review_queue q WHERE q.tour_id = rt.tour_id
                    AND q.review_status::text = 'pending') AS in_review
    FROM silver_aa_internal.raw_tours rt
   WHERE rt.tenant_id = $1::uuid AND rt.source_status = 'active' AND rt.deleted_at IS NULL
     AND rt.src_itineraries IS NOT NULL AND btrim(rt.src_itineraries) <> '')
SELECT count(*)::int AS eligible,
       count(*) FILTER (WHERE has_master)::int AS with_master,
       count(*) FILTER (WHERE NOT has_master AND NOT written)::int AS never_written,
       count(*) FILTER (WHERE NOT has_master AND in_review)::int AS in_review_no_master,
       count(*) FILTER (WHERE NOT has_master AND written AND NOT in_review)::int AS written_no_master_other
  FROM e"""

# Master of each live tour: current writer, quality score, newer generated version waiting / rejected. The kept set
# of AA-645 (masters kept on the v1 writer on purpose) is read from one shared.rerun_wave row; NULL when it is not
# recorded (the row then shows "expected gap unavailable").
KEPT_WAVE_NAME = "aa645-kept-on-v1"
MASTER = f"""
WITH {LIVE},
kept AS (
    SELECT expected_tour_ids FROM shared.rerun_wave WHERE name = '{KEPT_WAVE_NAME}'
     ORDER BY created_at DESC LIMIT 1),
kept_ids AS (SELECT unnest(expected_tour_ids) AS tour_id FROM kept),
cur AS (
    SELECT l.tour_id, l.quality_score_id, g.created_at AS cur_at, g.metadata->>'writer_version' AS writer_version
      FROM live l LEFT JOIN silver_aa_internal.generated_content g ON g.id = l.generated_content_id),
nw AS (
    SELECT c.*,
           (SELECT g2.status::text FROM silver_aa_internal.generated_content g2
             WHERE g2.tour_id = c.tour_id AND g2.created_at > c.cur_at
             ORDER BY g2.created_at DESC LIMIT 1) AS newest_status
      FROM cur c)
SELECT count(*)::int AS live,
       count(*) FILTER (WHERE writer_version = 'v2')::int AS writer_v2,
       CASE WHEN EXISTS (SELECT 1 FROM kept)
            THEN count(*) FILTER (WHERE tour_id IN (SELECT tour_id FROM kept_ids))::int END AS kept_live,
       count(*) FILTER (WHERE quality_score_id IS NOT NULL)::int AS with_quality_score,
       count(*) FILTER (WHERE newest_status IN ('pending', 'hitl'))::int AS newer_waiting_review,
       count(*) FILTER (WHERE newest_status IN ('rejected', 'failed'))::int AS newer_rejected,
       (SELECT count(*) FROM gold_aa_internal.published_tours p
         WHERE p.content_embedding IS NOT NULL AND p.id IN (SELECT published_id FROM live))::int AS content_embedding
  FROM nw"""

REVIEW = """
SELECT count(*) FILTER (WHERE review_status::text = 'pending')::int AS pending
  FROM silver_aa_internal.review_queue"""

SEO = f"""
WITH {LIVE}
SELECT count(*)::int AS live, count(s.tour_id)::int AS with_row,
       count(*) FILTER (WHERE s.cache_key LIKE '%:ideas_v3' AND s.fetched_at > now() - interval '180 days')::int
           AS fresh_ideas_v3,
       count(*) FILTER (WHERE jsonb_typeof(s.people_also_ask) = 'array'
                          AND jsonb_array_length(s.people_also_ask) > 0)::int AS with_paa
  FROM live l LEFT JOIN silver_aa_internal.seo_context s ON s.tour_id = l.tour_id"""

# ── 1.4 A-series ────────────────────────────────────────────────────────────────────────────────────────────────────
ATOMS = f"""
WITH {LIVE}, {ACTIVE_ATOMS},
agg AS (
    SELECT a.tour_id,
           count(*) FILTER (WHERE NOT a.is_empty_marker) AS atoms,
           count(*) FILTER (WHERE a.is_empty_marker) AS markers,
           count(*) FILTER (WHERE {PLACE_ATOM}) AS place_atoms,
           max(a.touched) AS last_touch
      FROM act a GROUP BY a.tour_id)
SELECT count(*)::int AS live,
       count(*) FILTER (WHERE g.atoms > 0)::int AS with_atoms,
       count(*) FILTER (WHERE g.atoms = 0 AND g.markers > 0)::int AS empty_marker_only,
       coalesce(sum(g.atoms), 0)::int AS atoms,
       coalesce(sum(g.place_atoms), 0)::int AS place_atoms,
       count(*) FILTER (WHERE g.last_touch < l.master_updated_at)::int AS atoms_older_than_master
  FROM live l LEFT JOIN agg g ON g.tour_id = l.tour_id"""

SEG = f"""
WITH {LIVE}, {ACTIVE_ATOMS},
seg AS (SELECT DISTINCT a.tour_id FROM act a JOIN acp_contract.atom_segment_member m ON m.atom_id = a.atom_id),
pl AS (SELECT DISTINCT a.tour_id FROM act a WHERE {PLACE_ATOM})
SELECT count(pl.tour_id)::int AS with_place_atoms,
       count(*) FILTER (WHERE pl.tour_id IS NOT NULL AND seg.tour_id IS NOT NULL)::int AS with_segment,
       (SELECT count(*) FROM acp_contract.atom_segment_member)::int AS member_rows,
       (SELECT count(*) FROM acp_contract.atom_segment_member m
          LEFT JOIN acp_contract.tour_atoms t ON t.atom_id = m.atom_id
         WHERE t.atom_id IS NULL OR t.deleted OR t.is_empty_marker)::int AS rows_on_dead_atoms
  FROM live l LEFT JOIN seg ON seg.tour_id = l.tour_id LEFT JOIN pl ON pl.tour_id = l.tour_id"""

# Slow tier, classified in Python with the deployed services.acp_contract.segment_matching.forms_segment(): the active
# non-marker atoms of live tours that have no segment member row, with whether their tour has any segment at all.
SEG_NO_MEMBER_ATOMS = f"""
WITH {LIVE}, {ACTIVE_ATOMS},
seg AS (SELECT DISTINCT a.tour_id FROM act a JOIN acp_contract.atom_segment_member m ON m.atom_id = a.atom_id)
SELECT a.atom_id, a.tour_id::text AS tour_id, a.itinerary_day, a.place, a.action, a.activity_type,
       (seg.tour_id IS NOT NULL) AS tour_has_segment
  FROM act a JOIN live l ON l.tour_id = a.tour_id
  LEFT JOIN seg ON seg.tour_id = a.tour_id
 WHERE NOT a.is_empty_marker
   AND NOT EXISTS (SELECT 1 FROM acp_contract.atom_segment_member m WHERE m.atom_id = a.atom_id)"""

GC = """
SELECT (result->'segment_gc'->>'orphans')::int AS orphans,
       (result->'segment_gc'->>'deletable')::int AS deletable,
       (result->'segment_gc'->>'kept')::int AS kept,
       finished_at
  FROM shared.job
 WHERE kind = 'recompute' AND status = 'succeeded' AND result->>'scope' = 'platform'
   AND jsonb_typeof(result->'segment_gc') = 'object'
 ORDER BY finished_at DESC NULLS LAST LIMIT 1"""

# Markets: the ranking markets only. ``search_demand`` also stores the admin SERP top-up under its own market
# (``services.acp_shared.real_questions.TOPUP_MARKET``, bound as $2), which is never ranked (r3 review).
RANK = f"""
WITH {LIVE},
rk AS (SELECT DISTINCT tour_id FROM acp_contract.atom_ranking WHERE superseded_at IS NULL),
seg AS (SELECT DISTINCT a.tour_id FROM acp_contract.v_active_tour_atoms a
          JOIN acp_contract.atom_segment_member m ON m.atom_id = a.atom_id WHERE NOT a.deleted)
SELECT count(seg.tour_id)::int AS with_segment,
       count(*) FILTER (WHERE seg.tour_id IS NOT NULL AND rk.tour_id IS NOT NULL)::int AS with_ranking,
       (SELECT count(DISTINCT market) FROM acp_contract.atom_ranking
         WHERE superseded_at IS NULL AND market <> $2)::int AS markets,
       (SELECT count(DISTINCT market) FROM acp_contract.search_demand WHERE market <> $2)::int AS markets_researched
  FROM live l LEFT JOIN rk ON rk.tour_id = l.tour_id LEFT JOIN seg ON seg.tour_id = l.tour_id"""

ROUTE = f"""
WITH {LIVE},
rt AS (SELECT DISTINCT tour_id FROM acp_contract.route WHERE superseded_at IS NULL)
SELECT count(*)::int AS live,
       count(rt.tour_id)::int AS with_route,
       (SELECT count(*) FROM acp_contract.route WHERE superseded_at IS NULL)::int AS current_routes,
       (SELECT count(*) FROM acp_contract.route WHERE superseded_at IS NULL AND hub_id IS NOT NULL)::int
           AS current_routes_with_hub
  FROM live l LEFT JOIN rt ON rt.tour_id = l.tour_id"""

# Search demand age over the markets that have current ranking rows (critic: no hard-coded market list).
PAA = """
WITH cur AS (SELECT DISTINCT segment_id FROM acp_contract.atom_ranking
              WHERE superseded_at IS NULL AND excluded_reason IS NULL),
mk AS (SELECT DISTINCT market FROM acp_contract.atom_ranking WHERE superseded_at IS NULL)
SELECT count(*)::int AS ranked_segments,
       count(*) FILTER (WHERE s.questions_count IS NOT NULL)::int AS computed,
       count(*) FILTER (WHERE s.questions_count > 0)::int AS with_questions,
       (SELECT max(age_days) FROM (
            SELECT current_date - max(d.retrieved_on)::date AS age_days
              FROM acp_contract.search_demand d WHERE d.market IN (SELECT market FROM mk)
             GROUP BY d.market) m)::int AS oldest_market_age_days
  FROM cur c JOIN acp_contract.atom_segment s ON s.segment_id = c.segment_id"""

EMB = """
WITH scope AS (
    SELECT DISTINCT a.atom_id FROM acp_contract.v_active_tour_atoms a
      JOIN acp_contract.atom_segment_member m ON m.atom_id = a.atom_id
      JOIN acp_contract.atom_segment s ON s.segment_id = m.segment_id
     WHERE NOT a.deleted AND s.questions_count > 0),
v AS (SELECT atom_id, count(DISTINCT view) AS views FROM acp_contract.atom_embedding GROUP BY atom_id)
SELECT count(*)::int AS landing_scope_atoms,
       count(*) FILTER (WHERE coalesce(v.views, 0) >= 2)::int AS with_both_views,
       (SELECT count(*) FROM acp_contract.atom_embedding)::int AS vector_rows,
       (SELECT count(*) FROM acp_contract.atom_embedding e
         WHERE NOT EXISTS (SELECT 1 FROM acp_contract.v_active_tour_atoms a
                            WHERE a.atom_id = e.atom_id AND NOT a.deleted))::int AS rows_on_inactive_atoms
  FROM scope LEFT JOIN v ON v.atom_id = scope.atom_id"""

# ── 1.5 Places, place cards, photos ─────────────────────────────────────────────────────────────────────────────────
GEO = f"""
WITH {LIVE},
gb AS (SELECT build_id FROM geo.graph_build WHERE is_current),
tp AS (SELECT gp.source_id, count(*) AS comps,
              count(*) FILTER (WHERE d.id IS NOT NULL AND {SERVED_DEST}) AS served,
              count(*) FILTER (WHERE gp.destination_id IS NULL) AS no_destination,
              count(*) FILTER (WHERE cardinality(gp.atom_ids) > 0 AND NOT EXISTS (
                     SELECT 1 FROM acp_contract.v_active_tour_atoms a
                      WHERE a.atom_id = ANY (gp.atom_ids) AND NOT a.deleted)) AS on_inactive_atoms
         FROM geo.tour_place gp LEFT JOIN shared.destinations d ON d.id = gp.destination_id
        GROUP BY gp.source_id),
st AS (SELECT DISTINCT s.source_id FROM geo.tour_stop s JOIN gb ON gb.build_id = s.build_id)
SELECT count(*) FILTER (WHERE b.source_id IS NOT NULL AND b.status <> 'retired')::int AS built,
       count(*) FILTER (WHERE tp.served > 0)::int AS with_served_component,
       count(*) FILTER (WHERE b.source_id IS NOT NULL AND b.status <> 'retired' AND coalesce(tp.served, 0) = 0)::int
           AS built_not_served,
       count(*) FILTER (WHERE b.source_id IS NOT NULL AND b.status <> 'retired' AND st.source_id IS NOT NULL)::int
           AS in_current_graph,
       coalesce(sum(tp.comps), 0)::int AS components,
       coalesce(sum(tp.no_destination), 0)::int AS components_no_destination,
       coalesce(sum(tp.on_inactive_atoms), 0)::int AS components_on_inactive_atoms,
       (SELECT count(*) FROM geo.place_name)::int AS names_total,
       (SELECT count(*) FROM geo.place_name WHERE status IN ('unknown', 'pending'))::int AS names_waiting,
       (SELECT count(*) FROM geo.place_shape WHERE kind = 'line')::int AS line_shapes,
       (SELECT count(*) FROM geo.place_shape WHERE kind = 'line' AND status IN ('ok', 'approximate', 'manual'))::int
           AS shapes_usable,
       (SELECT count(*) FROM geo.place_shape WHERE kind = 'line' AND status = 'not_found')::int AS shapes_not_found
  FROM live l
  LEFT JOIN geo.tour_build b ON b.source_id = l.source_key
  LEFT JOIN tp ON tp.source_id = l.source_key
  LEFT JOIN st ON st.source_id = l.source_key"""

CARD = f"""
WITH {LIVE}, {ACTIVE_ATOMS}
SELECT count(*)::int AS place_atoms,
       count(k.atom_id) FILTER (WHERE k.card_id IS NOT NULL)::int AS linked,
       count(*) FILTER (WHERE k.atom_id IS NULL)::int AS no_row,
       count(DISTINCT a.tour_id) FILTER (WHERE k.card_id IS NOT NULL)::int AS tours_with_a_card
  FROM act a JOIN live l ON l.tour_id = a.tour_id
  LEFT JOIN acp_shared.atom_place_card k ON k.atom_id = a.atom_id
 WHERE {PLACE_ATOM}"""

PHOTO = f"""
WITH {LIVE},
direct AS (SELECT DISTINCT tour_id FROM shared.v_tour_photos),
dest AS (SELECT DISTINCT gp.destination_id FROM geo.tour_place gp JOIN live l ON l.source_key = gp.source_id
          JOIN shared.destinations d ON d.id = gp.destination_id WHERE {SERVED_DEST})
SELECT (SELECT count(*) FROM live)::int AS live,
       (SELECT count(*) FROM live l JOIN direct d ON d.tour_id = l.tour_id)::int AS with_photo,
       (SELECT count(*) FROM dest)::int AS served_destinations,
       (SELECT count(*) FROM dest WHERE EXISTS (SELECT 1 FROM shared.v_destination_photos p
                                                 WHERE p.destination_id = dest.destination_id))::int
           AS dest_with_photo,
       (SELECT count(*) FROM shared.place_photo)::int AS photos,
       (SELECT count(*) FROM shared.place_photo WHERE status = 'unmatched')::int AS unmatched"""

# ── 1.6 Tenants and T-series ────────────────────────────────────────────────────────────────────────────────────────
# Canary (E2E / synthetic) tenants are counted but left out of the T0 targets (critic item 5).
TEN = """
WITH t AS (SELECT tenant_id, coalesce(is_canary, false) AS canary FROM shared.tenants
            WHERE is_active AND cancelled_at IS NULL AND tenant_id <> $1::uuid),
real AS (SELECT tenant_id FROM t WHERE NOT canary)
SELECT (SELECT count(*) FROM t)::int AS active,
       (SELECT count(*) FROM t WHERE canary)::int AS canary,
       (SELECT count(*) FROM real)::int AS active_real,
       (SELECT count(*) FROM shared.tenant_members m JOIN t USING (tenant_id) WHERE m.status = 'active')::int
           AS members_active,
       (SELECT count(DISTINCT b.tenant_id) FROM shared.tenant_brand_rules b JOIN real USING (tenant_id)
         WHERE b.is_active AND btrim(coalesce(b.system_prompt, '')) <> '')::int AS with_filled_brand,
       (SELECT count(*) FROM acp_shared.tenant_config c JOIN real USING (tenant_id)
         WHERE cardinality(c.markets) > 0 AND cardinality(c.channels) > 0)::int AS with_config"""

T2 = """
WITH latest AS (
  SELECT DISTINCT ON (v.tenant_id, v.published_tour_id) v.published_tour_id, v.created_at, v.qa_status, v.status
    FROM gold_aa_internal.tenant_tour_versions v ORDER BY v.tenant_id, v.published_tour_id, v.version_number DESC)
SELECT count(*)::int AS tenant_tours,
       count(*) FILTER (WHERE l.created_at < p.updated_at)::int AS older_than_master,
       count(*) FILTER (WHERE p.master_status <> 'active' OR p.deleted_at IS NOT NULL)::int AS on_inactive_master,
       count(*) FILTER (WHERE l.qa_status IN ('failed', 'escalated'))::int AS qa_failed_or_escalated,
       count(*) FILTER (WHERE l.status = 'approved')::int AS pool
  FROM latest l JOIN gold_aa_internal.published_tours p ON p.id = l.published_tour_id"""

TACT = """
WITH live_seg AS (SELECT DISTINCT m.segment_id FROM acp_contract.atom_segment_member m
                    JOIN acp_contract.v_active_tour_atoms a ON a.atom_id = m.atom_id AND NOT a.deleted)
SELECT (SELECT count(*) FROM acp_shared.quarter_plan)::int AS quarter_plans,
       count(*) FILTER (WHERE s.state = 'proposed')::int AS subjects_proposed,
       count(*) FILTER (WHERE s.state = 'picked')::int AS subjects_picked,
       count(*) FILTER (WHERE s.state = 'used')::int AS subjects_used,
       count(*) FILTER (WHERE s.state = 'cut')::int AS subjects_cut,
       count(*) FILTER (WHERE s.state IN ('proposed', 'picked'))::int AS subjects_open,
       count(*) FILTER (WHERE s.state IN ('proposed', 'picked') AND s.segment_id IS NOT NULL
                          AND NOT EXISTS (SELECT 1 FROM live_seg l WHERE l.segment_id = s.segment_id))::int
           AS subjects_on_dead_segment,
       (SELECT count(*) FROM acp_shared.angle_gate_request)::int AS angle_requests,
       (SELECT count(*) FROM acp_shared.angle_gate_request r
         WHERE NOT EXISTS (SELECT 1 FROM acp_contract.v_active_tour_atoms a
                            WHERE a.atom_id = r.atom_id AND NOT a.deleted))::int AS angle_requests_on_inactive_atom
  FROM acp_shared.subject s"""

T9 = """
WITH latest AS (SELECT DISTINCT ON (piece_id, channel) piece_id, status FROM acp_shared.publish_log
                 ORDER BY piece_id, channel, created_at DESC)
SELECT (SELECT count(*) FROM acp_shared.content_piece)::int AS pieces,
       (SELECT count(*) FROM acp_shared.content_piece WHERE status = 'approved')::int AS approved,
       (SELECT count(*) FROM acp_shared.content_piece WHERE status = 'held')::int AS held,
       (SELECT count(*) FROM acp_shared.content_piece WHERE content_embedding IS NOT NULL)::int AS embedded,
       (SELECT count(DISTINCT piece_id) FROM latest WHERE status = 'published')::int AS pieces_published,
       (SELECT count(*) FROM latest)::int AS publish_rows,
       (SELECT count(*) FROM latest WHERE status = 'failed')::int AS publish_failed,
       (SELECT count(*) FROM acp_deliver.tenant_tour_pages)::int AS trip_pages"""

AA_CONTENT = """
SELECT count(*)::int AS items,
       count(*) FILTER (WHERE i.retired_at IS NULL AND EXISTS (
           SELECT 1 FROM gold_aa_internal.aa_content_version v
            WHERE v.content_id = i.content_id AND v.status = 'published'))::int AS published_items,
       count(*) FILTER (WHERE i.retired_at IS NOT NULL)::int AS retired_items
  FROM gold_aa_internal.aa_content_item i"""

# ── 1.7 TripPlanner ─────────────────────────────────────────────────────────────────────────────────────────────────
# Owner contract views (tp-020), read as cis_app.
TP_TOTALS = "SELECT metric, n FROM tripplanner.v_ops_data_totals"
TP_PINS = "SELECT component_id, trips FROM tripplanner.v_ops_pin_components"
TP_ID_MAP = "SELECT old_id, new_id FROM tripplanner.v_ops_component_id_map"

# The pin chain on the CIS side, exactly as TripPlanner walks it (backend/shared/component_ids.id_map, AA-990): at each
# hop the TripPlanner id map wins over the CIS redirect of the same id, at most 5 hops, a cycle stops before the id
# repeats. $1 = MASTER (live set); the pins ($2 ids, $3 trips) and the map edges ($4 old, $5 new) from the two
# views above.
# A pin is served when its resolved component is a geo.tour_place row of a live tour on a served destination (the
# catalog.v_tour_component rule on base tables).
TP_PIN_CHAIN = f"""
WITH RECURSIVE {LIVE},
pins AS (SELECT * FROM unnest($2::text[], $3::bigint[]) AS p(cid, trips)),
edges AS (
    SELECT o, n, 1 AS pri FROM unnest($4::text[], $5::text[]) AS m(o, n)
    UNION ALL
    SELECT old_id::text, new_id::text, 2 FROM geo.component_redirect),
hop AS (SELECT DISTINCT ON (o) o, n FROM edges ORDER BY o, pri),
walk AS (
    SELECT p.cid AS raw, p.cid, 0 AS depth, ARRAY[p.cid] AS path FROM pins p
    UNION ALL
    SELECT w.raw, h.n, w.depth + 1, w.path || h.n
      FROM walk w JOIN hop h ON h.o = w.cid
     WHERE w.depth < 5 AND NOT h.n = ANY (w.path)),
r AS (SELECT DISTINCT ON (raw) raw, cid FROM walk ORDER BY raw, depth DESC),
k AS (
    SELECT p.trips, g.source_id,
           (g.source_id IS NOT NULL AND l.tour_id IS NOT NULL AND d.id IS NOT NULL AND {SERVED_DEST}) AS served
      FROM pins p JOIN r ON r.raw = p.cid
      LEFT JOIN geo.tour_place g ON g.component_id::text = r.cid
      LEFT JOIN live l ON l.source_key = g.source_id
      LEFT JOIN shared.destinations d ON d.id = g.destination_id)
SELECT coalesce(sum(trips), 0)::int AS pins,
       coalesce(sum(trips) FILTER (WHERE served), 0)::int AS served,
       coalesce(sum(trips) FILTER (WHERE source_id IS NULL), 0)::int AS no_component_row,
       coalesce(sum(trips) FILTER (WHERE source_id IS NOT NULL AND NOT served), 0)::int AS not_served
  FROM k"""

# catalog.* views over the tenant pool (aa_app_user; aa990_reader_checks.py without the sample arrays and without the
# objects aa_app_user cannot read: v_component_embedding, geo.*).
TENANT_TP_LAYERS = """
WITH t AS (SELECT source_id FROM catalog.v_tour),
c AS (SELECT source_id, count(*) AS n, count(*) FILTER (WHERE is_stale) AS stale
        FROM catalog.v_tour_component GROUP BY source_id),
d AS (SELECT source_id, count(*) AS n FROM catalog.v_tour_day GROUP BY source_id),
s AS (SELECT source_id, count(*) AS n FROM catalog.v_tour_stop GROUP BY source_id),
lg AS (SELECT source_id, count(*) AS n FROM catalog.v_tour_leg GROUP BY source_id)
SELECT count(*)::int AS served_tours,
       count(*) FILTER (WHERE c.n > 0)::int AS with_components,
       count(*) FILTER (WHERE c.stale > 0)::int AS with_stale_components,
       count(*) FILTER (WHERE d.n > 0)::int AS with_day_list,
       count(*) FILTER (WHERE s.n > 0)::int AS with_route_stops,
       count(*) FILTER (WHERE lg.n > 0)::int AS with_route_legs
  FROM t
  LEFT JOIN c ON c.source_id = t.source_id
  LEFT JOIN d ON d.source_id = t.source_id
  LEFT JOIN s ON s.source_id = t.source_id
  LEFT JOIN lg ON lg.source_id = t.source_id"""

# Its own source (tp_places, S252 live fix): catalog.v_tour_place resolves the places of every live tour when read
# whole (window functions partitioned by tour_id, three LATERAL lookups per tour-day place; services/catalog_http/
# reader.py), 6.1 s on Dev against 0.5 s for the five other views together, so inside TENANT_TP_LAYERS it timed out
# the whole statement (7 s) and turned eight rows n/a. served_tours is read again here (v_tour, ~20 ms) so the
# tp.located row depends on this one source only.
#
# Two equivalent forms, same numbers (tests/integration/test_aa992_data_coverage_sql.py compares them):
# - _WHOLE: one pass over the whole view, the shape the page has used since the first version (timed 6.1 s live);
# - _PER_TOUR: per served tour, the first located place of THAT tour (a tour_id filter Postgres pushes down through
#   the window functions to the raw_tours / tour_atoms scans, as the CRM reader's detail reads do; LIMIT 1 keeps the
#   subquery from being flattened into a semi-join over the whole view and can stop at the first located place).
# Both count a v_tour row when its tour has at least one place with coordinates: v_tour and v_tour_place take
# (source_id, tour_id) from the same catalog._live_tour rows in the same statement snapshot.
TENANT_TP_PLACES_WHOLE = """
WITH t AS (SELECT source_id FROM catalog.v_tour),
p AS (SELECT DISTINCT source_id FROM catalog.v_tour_place WHERE lat IS NOT NULL)
SELECT count(*)::int AS served_tours,
       count(p.source_id)::int AS with_located_places
  FROM t
  LEFT JOIN p ON p.source_id = t.source_id"""

TENANT_TP_PLACES_PER_TOUR = """
SELECT count(*)::int AS served_tours,
       count(l.located)::int AS with_located_places
  FROM catalog.v_tour t
  LEFT JOIN LATERAL (SELECT true AS located
                       FROM catalog.v_tour_place p
                      WHERE p.tour_id = t.tour_id AND p.lat IS NOT NULL
                      LIMIT 1) l ON true"""

TENANT_TP_PLACES = TENANT_TP_PLACES_WHOLE

TENANT_TP_COMPONENTS = """
SELECT count(*)::int AS served_components,
       count(*) FILTER (WHERE embedding IS NULL)::int AS unembedded_components
  FROM catalog.v_tour_component"""

TENANT_TP_RETIRED = """
WITH gone AS (SELECT source_id FROM catalog.v_tour_retired WHERE state IN ('retired', 'paused'))
SELECT (SELECT count(*) FROM gone)::int AS gone_keys,
       ((SELECT count(*) FROM catalog.v_tour v JOIN gone USING (source_id))
        + (SELECT count(*) FROM catalog.v_tour_component c JOIN gone USING (source_id))
        + (SELECT count(*) FROM catalog.v_tour_stop s JOIN gone USING (source_id))
        + (SELECT count(*) FROM catalog.v_tour_day d JOIN gone USING (source_id)))::int AS retired_served"""

TENANT_TP_OUTBOX = "SELECT idempotency_key FROM catalog.v_tour_lifecycle_outbox"

# The AA-987 purge: one tripplanner_purge job per lifecycle event (cis_app, keys from the tenant-pool read above).
TP_PURGE = """
SELECT count(*)::int AS events,
       count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM shared.job j
                                           WHERE j.idempotency_key = 'tripplanner_purge:' || k))::int
           AS events_without_job
  FROM unnest($1::text[]) AS k"""

# ── 1.8 Booking ─────────────────────────────────────────────────────────────────────────────────────────────────────
BK_TOTALS = "SELECT metric, n FROM booking.v_ops_data_totals"

# Trip plans whose catalog key is no longer a live tour (bk-025 keys view x the CIS live set).
BK_PLAN_KEYS = f"""
WITH {LIVE}
SELECT count(*)::int AS plans,
       count(*) FILTER (WHERE k.sellable)::int AS sellable,
       count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM live l WHERE l.source_key = k.source_id))::int AS orphaned
  FROM booking.v_ops_trip_plan_keys k WHERE k.source_system = 'cis'"""

SUPPLY = """
WITH bk AS (
  SELECT 'hotel'::text AS t, id::text AS id, (status = 'active') AS active, (destination_id IS NULL) AS no_dest
    FROM booking.v_supply_hotels
  UNION ALL
  SELECT 'service', id::text, (status = 'active'), (destination_id IS NULL) FROM booking.v_supply_services)
SELECT count(*) FILTER (WHERE bk.active)::int AS bk_active,
       count(*) FILTER (WHERE bk.active AND bk.t = 'hotel')::int AS hotels_active,
       count(*) FILTER (WHERE bk.active AND e.id IS NOT NULL)::int AS mirrored_active,
       count(*) FILTER (WHERE bk.active AND e.content_status = 'published')::int AS published_active,
       count(*) FILTER (WHERE bk.active AND bk.t = 'hotel' AND bk.no_dest)::int AS hotels_no_destination
  FROM bk LEFT JOIN shared.supply_entity e
         ON e.source_system = 'booking' AND e.entity_type = bk.t AND e.source_id = bk.id"""

BK_CASES = """
SELECT coalesce(sum(n) FILTER (WHERE status NOT IN ('completed', 'cancelled')), 0)::int AS open_cases
  FROM booking.v_ops_case_status"""

BK_FUNNEL = """
SELECT coalesce(sum(n) FILTER (WHERE metric = 'inquiry_received'), 0)::int AS received,
       coalesce(sum(n) FILTER (WHERE metric IN ('inquiry_parked', 'inquiry_failed')), 0)::int AS stuck
  FROM booking.v_ops_funnel_daily"""

# ── 1.9 CRM: over HTTP (crm.ops.v1 1.4), see sources.crm. ─────────────────────────────────────────────────
# The CIS live count the CRM cache is compared with, as of the CRM's last full pass (critic B12-3).
LIVE_COUNT = f"WITH {LIVE} SELECT count(*)::int AS live FROM live"
LIVE_SINCE = f"""
WITH {LIVE}
SELECT count(*)::int AS live,
       count(*) FILTER (WHERE p.published_at > $2::timestamptz)::int AS published_after
  FROM live l JOIN gold_aa_internal.published_tours p ON p.id = l.published_id"""
