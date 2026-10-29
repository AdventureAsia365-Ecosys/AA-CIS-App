"""AA-671 — the cover photo of a pool tour: one SQL chain for GET /v1/tours/pool (both pools) and the
photo_destination_match job's coverage report, so the report counts exactly what the pool serves.

The chain, first answer wins:
1. tour     — the tour's position-1 photo (a Drive folder of its own; S256: 162 of 892 pool tours).
2. place    — a photo of its primary place: among the usable places of its graph components that have a photo, the one
              the tour spends the most days in, then the earliest day, then the destination id (S257, CIS #1131).
3. nearby   — (S258) a photo of the photo place nearest to the tour's primary place, at most NEARBY_COVER_KM away
              ("Hoi An" for a tour that spends its days in "Hoi An Ancient Town").
4. country  — (S258) a photo of a photo place in the primary place's country (the destination's own country, one
              canonical shared.countries name since AA-990; so a cross-border tour takes the country it spends the most
              days in). Compared by name, not country_iso2: the tenant view carries country_iso2 only from 279's
              catalog part, which the CI replay skips; the name is in every shape of catalog.v_destination.
A tour without graph components (no primary place) has no cover; the portal shows its placeholder.

Photo within a place: md5(tour id || path), stable for a tour, spread across tours (lesson S253, duplicate covers).
Levels 3 and 4 pick the place the same way (nearest first, then md5(tour id || destination id)), so the tours of one
country do not all show one picture. Every row of the photo read views is a matched photo with its renditions.

The templates take the relations of one pool: the tenant pool (aa_app_user) reads the catalog contract views, the admin
pool (cis_app) their source tables (ADR 0015 d3); equal answers are tested in
tests/integration/test_aa671_pool_facets.py.
"""
from __future__ import annotations

NEARBY_COVER_KM = 25
LEVELS = ("tour", "place", "nearby", "country")

# The places that have a served photo, located and not flagged, computed once per statement (a few hundred rows).
# A merged duplicate has no photo (place_dedupe repoints place_photo.destination_id), so no duplicate_of test is
# needed, and the tenant view does not expose it.
_COVER_PLACE_CTE = """cover_place AS MATERIALIZED (
        SELECT d.{dest_id} AS destination_id, d.lat, d.lng, lower(btrim(d.country)) AS country
          FROM {destination} d
         WHERE d.geo_status <> 'suspect'
           AND d.{dest_id} IN (SELECT dp.destination_id FROM {destination_photo} dp)
    )"""
_TOUR_LEVEL = """SELECT ph.path FROM {tour_photo} ph
              WHERE {tour_photo_cond} ph.tour_id = pt.tour_id AND ph.position = 1
              LIMIT 1"""
_PLACE_LEVEL = """SELECT place_photo.path
               FROM (SELECT c.destination_id, count(DISTINCT c.day_index) AS days, min(c.day_index) AS first_day
                       FROM {components}
                      GROUP BY c.destination_id) place
              CROSS JOIN LATERAL (
                    SELECT dp.path FROM {destination_photo} dp
                     WHERE dp.destination_id = place.destination_id
                     ORDER BY md5(pt.tour_id::text || dp.path)
                     LIMIT 1) place_photo
              ORDER BY place.days DESC, place.first_day, place.destination_id
              LIMIT 1"""
# Levels 3 + 4 in one read (path, km): from the primary place (with or without a photo), the nearest photo place within
# NEARBY_COVER_KM, else a photo place of the primary place's country. Distance: equirectangular, exact enough at
# this scale (an error well under 1 % at 25 km).
_NEAR_LEVEL = """SELECT pick.path, alt.km
               FROM (SELECT c.destination_id, count(DISTINCT c.day_index) AS days, min(c.day_index) AS first_day
                       FROM {components}
                      GROUP BY c.destination_id
                      ORDER BY days DESC, first_day, c.destination_id
                      LIMIT 1) prim
               JOIN {destination} pd ON pd.{dest_id} = prim.destination_id
              CROSS JOIN LATERAL (
                    SELECT cp.destination_id, g.km
                      FROM cover_place cp
                     CROSS JOIN LATERAL (
                           SELECT 111.2 * sqrt(power(cp.lat - pd.lat, 2)
                                  + power(cos(radians((cp.lat + pd.lat) / 2)) * (cp.lng - pd.lng), 2)) AS km) g
                     WHERE g.km <= {near_km} OR cp.country = lower(btrim(pd.country))
                     ORDER BY g.km <= {near_km} DESC, CASE WHEN g.km <= {near_km} THEN g.km END,
                              md5(pt.tour_id::text || cp.destination_id::text)
                     LIMIT 1) alt
              CROSS JOIN LATERAL (
                    SELECT dp.path FROM {destination_photo} dp
                     WHERE dp.destination_id = alt.destination_id
                     ORDER BY md5(pt.tour_id::text || dp.path)
                     LIMIT 1) pick"""

_POOL_RELATIONS = {
    # tenant pool (aa_app_user): the catalog contract views
    True: dict(tour_photo="catalog.v_tour_photo", tour_photo_cond="ph.source_system = 'cis' AND",
               components="catalog.v_tour_component c WHERE c.source_system = 'cis' AND c.tour_id = pt.tour_id",
               destination_photo="catalog.v_destination_photo", destination="catalog.v_destination",
               dest_id="destination_id"),
    # admin pool (cis_app): the facet join's source rows (geo.tour_place on a usable destination, by the tour's
    # source key written as the expression of raw_tours.source_key, see v1_tours._POOL_FACET_JOIN)
    False: dict(tour_photo="shared.v_tour_photos", tour_photo_cond="",
                components="""geo.tour_place c
                       JOIN shared.destinations d ON d.id = c.destination_id
                                                 AND d.geo_status <> 'suspect' AND d.duplicate_of IS NULL
                      WHERE c.source_id = (COALESCE(rt.source_group_id, rt.tour_id))::text""",
                destination_photo="shared.v_destination_photos", destination="shared.destinations", dest_id="id"),
}


def _fmt(template: str, tenant_pool: bool) -> str:
    return template.format(near_km=NEARBY_COVER_KM, **_POOL_RELATIONS[tenant_pool])


def cover_cte(tenant_pool: bool) -> str:
    """The `cover_place` CTE (without WITH) the cover join reads; the statement must define it."""
    return _fmt(_COVER_PLACE_CTE, tenant_pool)


def cover_join(tenant_pool: bool) -> str:
    """LEFT JOIN LATERAL giving `cover.path` (or NULL) for each row of published_tours `pt` / raw_tours `rt`.
    COALESCE reads a level only when the ones before it gave nothing."""
    return ("LEFT JOIN LATERAL (\n        SELECT COALESCE(\n            (" + _fmt(_TOUR_LEVEL, tenant_pool) + "),\n"
            "            (" + _fmt(_PLACE_LEVEL, tenant_pool) + "),\n"
            "            (SELECT near.path FROM (" + _fmt(_NEAR_LEVEL, tenant_pool) + ") near)\n"
            "        ) AS path\n    ) cover ON true")


def coverage_sql(conditions: tuple[str, ...] | list[str]) -> str:
    """Admin pool: every pool tour (`conditions` on pt / rt, the pool's base conditions) with the answer of each level,
    for the job's report. Columns: tour_id, country, tour_path, place_path, near_path, near_km, destination_ids."""
    return (
        "WITH " + cover_cte(False) + "\n"
        "SELECT pt.tour_id::text AS tour_id, rt.country,\n"
        "       (" + _fmt(_TOUR_LEVEL, False) + ") AS tour_path,\n"
        "       (" + _fmt(_PLACE_LEVEL, False) + ") AS place_path,\n"
        "       near.path AS near_path, near.km AS near_km,\n"
        "       ARRAY(SELECT DISTINCT c.destination_id::text FROM " + _POOL_RELATIONS[False]["components"]
        + ") AS destination_ids\n"
        "  FROM gold_aa_internal.published_tours pt\n"
        "  LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id\n"
        "  LEFT JOIN LATERAL (" + _fmt(_NEAR_LEVEL, False) + ") near ON true\n"
        " WHERE " + " AND ".join(conditions) + "\n"
        " ORDER BY 1"
    )


def level_of(row) -> str | None:
    """The level that gives this coverage row its cover, None when it has none."""
    if row["tour_path"]:
        return "tour"
    if row["place_path"]:
        return "place"
    if row["near_path"]:
        return "nearby" if row["near_km"] is not None and row["near_km"] <= NEARBY_COVER_KM else "country"
    return None


def summarise(rows, planned_destinations=()) -> dict:
    """The coverage report: pool tours per level (and distinct photos per level: one photo on many cards shows as a
    low ratio), tours without a cover per country, tours without graph places, and — for a dry run — how many tours
    below the place level have a place the run would give a photo (they move to the place level after the apply)."""
    planned = {str(d) for d in planned_destinations}
    levels = {lv: 0 for lv in LEVELS}
    paths: dict[str, set] = {lv: set() for lv in LEVELS}
    none_by_country: dict[str, int] = {}
    out = {"pool_tours": 0, "none": 0, "no_places": 0, "to_place_after_apply": 0}
    for r in rows:
        out["pool_tours"] += 1
        lv = level_of(r)
        dests = set(r["destination_ids"] or [])
        if not dests:
            out["no_places"] += 1
        if lv is None:
            out["none"] += 1
            key = r["country"] or "?"
            none_by_country[key] = none_by_country.get(key, 0) + 1
        else:
            levels[lv] += 1
            paths[lv].add(r[{"tour": "tour_path", "place": "place_path"}.get(lv, "near_path")])
        if lv not in ("tour", "place") and dests & planned:
            out["to_place_after_apply"] += 1
    out["levels"] = levels
    out["distinct_photos"] = {lv: len(p) for lv, p in paths.items()}
    out["with_cover"] = sum(levels.values())
    out["none_by_country"] = dict(sorted(none_by_country.items(), key=lambda kv: (-kv[1], kv[0])))
    return out
