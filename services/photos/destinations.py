"""AA-671 S257 — give a tour-matched photo the destination it shows (shared.place_photo.destination_id).

Why: the photo read views by place (shared.v_destination_photos, catalog.v_destination_photo) need destination_id,
which no photo had (the sync only matched destinations on request, and the request was never made after the S207
reset). Without it the pool cover fallback (services/photos/covers.py) and the TripPlanner destination
pages find no photo of a place.

Rule: a photo is compared only with the places of ITS OWN tour — the geo.tour_place rows of the tour's source key
(COALESCE(source_group_id, tour_id), like the catalog) plus the stops of the current graph build, on a destination
that is not a merged duplicate. services.photos.match.resolve_destination decides; only an unambiguous answer is
written. Used by the photo sync (new photos, at sync time) and by the photo_destination_match backfill job.

Reads raw_tours with app.tenant_id pinned to the AA master tenant for its own transaction (services.places.store
_catalog_fetch, like the AA-860 read it replaces): raw_tours is FORCE-RLS, and cis_app's USING-true policy (244) is
not the only way this runs (the CI replay skips 244; a plain reader role sees the master rows through the pin).
"""
from __future__ import annotations

from typing import Iterable

# Per photo tour id: (destination id, a name of it). Two names per destination when the itinerary spelling differs
# from the destination name ("Tiger's Nest" vs "Paro Taktsang"); a stop of the graph build carries the destination
# name only.
CANDIDATES_SQL = """
    WITH tours AS (
        SELECT rt.tour_id, (COALESCE(rt.source_group_id, rt.tour_id))::text AS source_id
          FROM silver_aa_internal.raw_tours rt
         WHERE rt.tour_id = ANY($1::uuid[])
    ), cand AS (
        SELECT t.tour_id, tp.destination_id, tp.name AS place_name
          FROM tours t
          JOIN geo.tour_place tp ON tp.source_id = t.source_id
         WHERE tp.destination_id IS NOT NULL
        UNION
        SELECT t.tour_id, ts.destination_id, NULL::text
          FROM tours t
          JOIN geo.tour_stop ts ON ts.source_id = t.source_id
          JOIN geo.graph_build gb ON gb.build_id = ts.build_id AND gb.is_current
    )
    SELECT c.tour_id::text AS tour_id, d.id::text AS destination_id, d.name AS destination_name, c.place_name
      FROM cand c
      JOIN shared.destinations d ON d.id = c.destination_id AND d.duplicate_of IS NULL
     ORDER BY 1, 2, 4
"""


async def tour_candidates(conn, tour_ids: Iterable[str]) -> dict[str, list[tuple[str, str]]]:
    """tour id -> [(destination id, name)] of that tour's places. `conn` is a connection (the read runs in its own
    transaction or savepoint). Raises on a DB error (the caller decides whether to fail open)."""
    from services.places.store import _catalog_fetch

    ids = sorted({str(t) for t in tour_ids if t})
    if not ids:
        return {}
    out: dict[str, set[tuple[str, str]]] = {}
    for r in await _catalog_fetch(conn, CANDIDATES_SQL, ids):
        pairs = out.setdefault(r["tour_id"], set())
        pairs.add((r["destination_id"], r["destination_name"]))
        if r["place_name"]:
            pairs.add((r["destination_id"], r["place_name"]))
    return {t: sorted(p) for t, p in out.items()}


# ── AA-671 S258: what matcher version 2 needs (services.photos.match.resolve_destination `places` / `nearby`) ──────
# Per photo tour id: its places' coordinates and how many itinerary days the tour spends at each (geo.tour_place
# day_index; a graph-build stop that is not an itinerary place counts 0 days). Same tour scope as CANDIDATES_SQL.
PLACES_SQL = """
    WITH tours AS (
        SELECT rt.tour_id, (COALESCE(rt.source_group_id, rt.tour_id))::text AS source_id
          FROM silver_aa_internal.raw_tours rt
         WHERE rt.tour_id = ANY($1::uuid[])
    ), cand AS (
        SELECT t.tour_id, tp.destination_id, tp.day_index
          FROM tours t
          JOIN geo.tour_place tp ON tp.source_id = t.source_id
         WHERE tp.destination_id IS NOT NULL
        UNION
        SELECT t.tour_id, ts.destination_id, NULL::int
          FROM tours t
          JOIN geo.tour_stop ts ON ts.source_id = t.source_id
          JOIN geo.graph_build gb ON gb.build_id = ts.build_id AND gb.is_current
    )
    SELECT c.tour_id::text AS tour_id, d.id::text AS destination_id, d.lat, d.lng,
           count(DISTINCT c.day_index) AS days, min(c.day_index) AS first_day
      FROM cand c
      JOIN shared.destinations d ON d.id = c.destination_id AND d.duplicate_of IS NULL
     GROUP BY 1, 2, 3, 4
     ORDER BY 1, 2
"""
# Places outside the tours whose identity key equals a label key (destinations_name_country_uidx leads with name_norm):
# located, not merged, not flagged suspect. The resolver keeps only those near the photo's own tour.
NEARBY_SQL = """
    SELECT d.id::text AS id, d.name, d.name_norm, d.lat, d.lng
      FROM shared.destinations d
     WHERE d.name_norm = ANY($1::text[]) AND d.duplicate_of IS NULL AND d.name_norm IS NOT NULL
       AND d.geo_status <> 'suspect'
     ORDER BY 1
"""


async def tour_places(conn, tour_ids: Iterable[str]) -> dict:
    """tour id -> {destination id -> match.Place} (coordinates, itinerary days). Raises on a DB error."""
    from services.photos.match import Place
    from services.places.store import _catalog_fetch

    ids = sorted({str(t) for t in tour_ids if t})
    if not ids:
        return {}
    out: dict[str, dict] = {}
    for r in await _catalog_fetch(conn, PLACES_SQL, ids):
        out.setdefault(r["tour_id"], {})[r["destination_id"]] = Place(
            r["lat"], r["lng"], int(r["days"] or 0), r["first_day"])
    return out


async def nearby_by_key(conn, labels: Iterable[str]) -> dict:
    """label key -> [(destination id, name, match.Place)] of the places named by any of the labels. Raises on a DB
    error."""
    from services.photos.match import Place, label_keys

    keys = sorted({k for lab in labels for k in label_keys(lab)})
    if not keys:
        return {}
    out: dict[str, list] = {}
    for r in await conn.fetch(NEARBY_SQL, keys):
        out.setdefault(r["name_norm"], []).append((r["id"], r["name"], Place(r["lat"], r["lng"])))
    return out
