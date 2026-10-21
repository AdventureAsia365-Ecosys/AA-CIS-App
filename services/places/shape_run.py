"""AA-847 — one run of the place_shape job (D1: lines are drawn): due line destinations → candidates (Geoapify Place
Details, Overpass) → the acceptance rule (services/places/shape.py) → geo.place_shape; when no OSM feature passes, the
anchored approximate route of a tour-day; when there are no anchors either, not_found (retried, listed on
/admin/places "Lines").

Single writer: this run is the only writer of geo.place_shape apart from the admin's manual GeoJSON; it NEVER moves a
destination's lat / lng (place_geocode is the single coordinate writer — no "snap" onto the line).

Clients are injected (`details`, `overpass`; either may be None = that source is off: GEOCODER_PROVIDER=model_only
turns both off and only the no-call anchored route is built).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import structlog

from services.places import geomatch, guards, shape, store
from services.places.geocoder import CreditLimitReached, GeocoderUnavailable

logger = structlog.get_logger()

RETRY_OK_DAYS = 365          # an ok shape is not re-fetched by the job (admin backfill can force it)
RETRY_NOT_FOUND_DAYS = 30
RETRY_FAILED_DAYS = 1
RETRY_APPROXIMATE_DAYS = 30  # an approximate route is re-checked for an OSM feature


@dataclass
class Outcome:
    destination_id: str
    status: str                       # ok / approximate / not_found / failed / skipped
    source: Optional[str] = None
    length_km: Optional[float] = None
    osm: Optional[str] = None
    reasons: list = field(default_factory=list)
    error: Optional[str] = None


def anchor_points(row: dict, info: dict) -> list:
    """(lat, lng) the line must pass near: the destination's own point and, for every tour-day using it, the previous
    stop, the day's stop and the day's other point places."""
    pts = [(row["lat"], row["lng"])]
    stops = info.get("stops") or {}
    points = info.get("points") or {}
    for source_id, day in info.get("uses") or []:
        prev = _prev_stop(stops, source_id, day)
        for a in ([prev] if prev else []) + ([stops[(source_id, day)]] if (source_id, day) in stops else []) \
                + list(points.get((source_id, day), [])):
            pts.append((a[1], a[2]))
    return list(dict.fromkeys((round(a, 5), round(b, 5)) for a, b in pts))


def _prev_stop(stops: dict, source_id: str, day: int):
    earlier = [d for (s, d) in stops if s == source_id and d < day]
    return stops[(source_id, max(earlier))] if earlier else None


def day_routes(info: dict) -> list:
    """For each tour-day using the line: its ordered anchors (previous stop → the day's point places ordered by
    distance from it → the day's stop), as (lat, lng) lists — the input of the approximate route."""
    stops = info.get("stops") or {}
    points = info.get("points") or {}
    out = []
    for source_id, day in info.get("uses") or []:
        prev = _prev_stop(stops, source_id, day)
        this = stops.get((source_id, day))
        mids = [(p[1], p[2]) for p in points.get((source_id, day), [])
                if not this or p[0] != this[0]]
        start = (prev[1], prev[2]) if prev else (mids[0] if mids else None)
        if start is None:
            continue
        seq = ([start] if prev else []) + shape.order_along(start, mids) + ([(this[1], this[2])] if this else [])
        out.append(seq)
    return out


def best_anchored(info: dict, iso2: str) -> list:
    """The longest anchored polyline over the line's tour-days ([] when none has 2 anchors ≥ 2 km apart)."""
    best: list = []
    for seq in day_routes(info):
        parts = shape.anchored_line(seq, iso2)
        if parts and (not best or shape.length_km(parts) > shape.length_km(best)):
            best = parts
    return best


async def candidates_for(row: dict, anchors: list, iso2: str, details, overpass) -> tuple:
    """(verdict, tried): Place Details first; Overpass when details found nothing acceptable or only a single way
    (a way of a river is one OSM piece — the relation or the merged same-name ways win, shape.choose)."""
    name = row["name"]
    tried: list = []
    cands: list = []
    if details is not None and row.get("geocoder_place_id") and row.get("geocoder_provider") in (None, "geoapify"):
        c = await details.line(row["geocoder_place_id"])
        tried.append({"source": "geoapify_details", "found": c is not None})
        if c is not None:
            cands.append(c)
    verdict = shape.accept(name, cands, anchors, iso2) if cands else shape.ShapeVerdict(None)
    single_way = verdict.accepted is not None and verdict.accepted.osm_type == "way" \
        and len(verdict.accepted.way_ids) <= 1
    if overpass is not None and (verdict.accepted is None or single_way):
        more = await overpass.lines(name, iso2, anchors, geomatch.line_class(name))
        tried.append({"source": "overpass", "found": len(more)})
        if more:
            cands += more
            verdict = shape.accept(name, cands, anchors, iso2)
    return verdict, tried


async def resolve_one(conn, row: dict, info: dict, details, overpass) -> Outcome:
    did = row["destination_id"]
    iso2 = guards.iso2(row["country"])
    anchors = anchor_points(row, info)
    verdict, tried = (shape.ShapeVerdict(None), [])
    if details is not None or overpass is not None:
        verdict, tried = await candidates_for(row, anchors, iso2, details, overpass)
    meta = {"tried": tried, "rejected": verdict.reasons[:20], "anchors": len(anchors)}
    if verdict.accepted is not None:
        c = verdict.accepted
        lv = shape.levels(verdict.parts)
        meta.update({"sim": verdict.sim, "anchor_km": verdict.anchor_km, "names": c.names[:12],
                     "line_class": c.line_class, "way_ids": c.way_ids[:50],
                     "attribution": "© OpenStreetMap contributors"})
        await store.write_shape(conn, did, status="ok", source=c.source, levels=lv,
                                bbox=shape.bbox(verdict.parts), length_km=verdict.length_km,
                                osm_type=c.osm_type, osm_id=c.osm_id, meta=meta, retry_days=RETRY_OK_DAYS)
        return Outcome(did, "ok", c.source, verdict.length_km, f"{c.osm_type}/{c.osm_id}" if c.osm_id else None,
                       verdict.reasons)
    if row.get("status") == "approximate":
        await store.write_shape_miss(conn, did, status="not_found", error=None, meta={"recheck": meta},
                                     retry_days=RETRY_APPROXIMATE_DAYS)
        return Outcome(did, "approximate", "anchors", reasons=verdict.reasons)
    parts = best_anchored(info, iso2)
    if parts:
        meta["approximate"] = "anchored polyline of a tour-day (no single OSM feature passed)"
        length = shape.length_km(parts)
        await store.write_shape(conn, did, status="approximate", source="anchors", levels=shape.levels(parts),
                                bbox=shape.bbox(parts), length_km=length, osm_type=None, osm_id=None, meta=meta,
                                retry_days=RETRY_APPROXIMATE_DAYS)
        return Outcome(did, "approximate", "anchors", round(length, 1), reasons=verdict.reasons)
    await store.write_shape_miss(conn, did, status="not_found", error="no feature passed and no anchors",
                                 meta=meta, retry_days=RETRY_NOT_FOUND_DAYS)
    return Outcome(did, "not_found", reasons=verdict.reasons)


async def resolve_shapes(pool, *, details, overpass, limit: int, progress=None, budget=None,
                         cost_per_credit: float = 0.0) -> dict:
    """Resolve up to `limit` due line destinations. Stops (names stay due, no try counted) on the daily credit guard
    or a geocoder setup failure, like place_geocode."""
    out: dict = {"names": 0, "ok": 0, "approximate": 0, "not_found": 0, "failed": 0, "items": []}
    async with pool.acquire() as conn:
        rows = await store.due_shapes(conn, limit)
        infos = await store.shape_uses(conn, [r["destination_id"] for r in rows])
    for i, row in enumerate(rows):
        if budget is not None and details is not None:
            budget.check(cost_per_credit * 2)
        before = details.credits if details is not None else 0
        try:
            async with pool.acquire() as conn:
                o = await resolve_one(conn, row, infos.get(row["destination_id"]) or {}, details, overpass)
        except CreditLimitReached:
            out["stopped_credit_limit"] = True
            break
        except GeocoderUnavailable as e:
            out["stopped_geocoder_unavailable"] = str(e)[:200]
            break
        except Exception as e:  # noqa: BLE001 — one name's failure never stops the run
            async with pool.acquire() as conn:
                await store.write_shape_miss(conn, row["destination_id"], status="failed",
                                             error=f"{type(e).__name__}: {str(e)[:300]}", meta={},
                                             retry_days=RETRY_FAILED_DAYS)
            o = Outcome(row["destination_id"], "failed", error=type(e).__name__)
        finally:
            if budget is not None and details is not None:
                budget.charge(cost_per_credit * (details.credits - before))
        out["names"] += 1
        out[o.status] = out.get(o.status, 0) + 1
        out["items"].append({"destination_id": o.destination_id, "name": row["name"], "country": row["country"],
                             "status": o.status, "source": o.source, "length_km": o.length_km, "osm": o.osm,
                             "error": o.error})
        if progress is not None and (i + 1) % 5 == 0:
            await progress(step="shapes", done=i + 1, total=len(rows))
    out["items"] = out["items"][:100]
    return out
