"""AA-992 — shared definitions of the data coverage page: the live-tour rule and the atom / place / served-destination
fragments, as SQL text with ``$1`` = the AA master tenant id. Standard library only.

These are the S252 propagation audit's definitions (``coverage.py``, run as ``cis_app`` 25/10/2026), so the page shows
the numbers the audit measured:

- ``LIVE`` — the ``catalog._live_tour`` rule (CIS 253 / 258) on the base tables: the AA master Master
  (``published_tours.tenant_id = MASTER``, ``master_status = 'active'``, not deleted) joined to its raw source
  (``source_status = 'active'``, not deleted). One row per live tour.
- ``ACTIVE_ATOMS`` — ``acp_contract.v_active_tour_atoms`` (CIS 203, already filtered by the Master's status), not
  deleted.
- ``PLACE_ATOM`` — an active atom that names a place and is not an empty-day marker.
- ``SERVED_DEST`` — a destination ``catalog.v_tour_component`` serves (not suspect, not merged into another).

Design open question 10 proposes making this module the one definition the AA-991 freshness probe imports too (a
byte-identical copy in AA-CIS-Infra ``db/coverage_defs.py`` under the Migration Copy Guard). Not done in this change:
the probe keeps its own definitions until that is decided.
"""
from __future__ import annotations

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"

LIVE = """live AS (
    SELECT pt.tour_id, pt.id AS published_id, rt.source_key,
           pt.updated_at AS master_updated_at, pt.generated_content_id, pt.quality_score_id
      FROM gold_aa_internal.published_tours pt
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
     WHERE pt.tenant_id = $1::uuid
       AND pt.master_status = 'active' AND pt.deleted_at IS NULL
       AND rt.source_status = 'active' AND rt.deleted_at IS NULL)"""

ACTIVE_ATOMS = """act AS (
    SELECT a.atom_id, a.tour_id, a.place, a.action, a.activity_type, a.itinerary_day, a.is_empty_marker,
           coalesce(a.updated_at, a.created_at) AS touched
      FROM acp_contract.v_active_tour_atoms a
     WHERE NOT a.deleted)"""

PLACE_ATOM = "a.place IS NOT NULL AND btrim(a.place) <> '' AND NOT a.is_empty_marker"

SERVED_DEST = "d.geo_status <> 'suspect' AND d.duplicate_of IS NULL"
