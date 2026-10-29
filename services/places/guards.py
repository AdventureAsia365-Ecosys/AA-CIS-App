"""AA-712 — quality guards for coordinates (design §4).

Hard (a point failing these is never stored):
  - the country is an ISO 3166-1 country (AA-990: shared.countries through the in-code mirror shared/countries, any
    country — no hand-made "AA country" list), and the point lies inside THAT country's bounding box and its polygon
    (10 km border buffer, services/places/shape.py). The country checked is the PLACE's own country (the model's
    answer, confirmed by the geocoder), never the tour's: a tour sold as India may climb Kilimanjaro in Tanzania, a
    Thailand tour may sleep in Siem Reap (cross-border, normal);
  - when a geocoder confirms, its country must equal the model's country.
Soft (per destination, across tours): the transit-aware 600 km outlier rule — an outlier is
recorded per (destination, tour, day) when the stop is > OUTLIER_KM from every other stop
within ±2 days and neither day's text says flight/train/sleeper/overnight bus/ferry. A destination is
`suspect` when it is an outlier in ≥ 2 tours, or in ≥ 1 tour and only the model located it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from shared import countries as _countries

# Views of the mirror for the few importers (lower-case ISO alpha-2 keys, as the place code has always used them):
#   COUNTRY_ISO2  every lower-case spelling (name, ISO name, alias; 'tibet' -> 'cn') -> alpha-2
#   ISO2_COUNTRY  alpha-2 -> canonical name ('vn' -> 'Vietnam', 'tz' -> 'Tanzania')
#   ISO2_ISO3 / ISO3_ISO2, BBOX (min_lat, max_lat, min_lng, max_lng), generous by ~0.3°; the 20 boxes tuned by hand
#   before AA-990 are kept as they were (scripts/aa990_country_data.py BBOX_TUNED).
COUNTRY_ISO2 = {s: c.iso2.lower() for c in _countries.all_countries() for s in sorted(c.spellings())}
ISO2_COUNTRY = {c.iso2.lower(): c.name for c in _countries.all_countries()}
ISO2_ISO3 = {c.iso2.lower(): c.iso3 for c in _countries.all_countries()}
ISO3_ISO2 = {v: k for k, v in ISO2_ISO3.items()}
BBOX = {c.iso2.lower(): c.bbox for c in _countries.all_countries() if c.bbox}

OUTLIER_KM = 600.0
# point_ok's polygon check: a point this close to the country's border still counts (border rivers, coarse polygons).
POLYGON_BUFFER_KM = 10.0
# Geocoders whose accepted result replaces the model's point (services/places/geocoder.CONFIRMED_BY).
CONFIRMED_BY = frozenset({"geoapify"})


def canonical_country(name: Optional[str]) -> Optional[str]:
    """'thailand' / 'SRI-LANDKA' / 'Tibet' / 'tanzania' -> 'Thailand' / 'Sri Lanka' / 'China' / 'Tanzania'; None when
    the text names no ISO country (a compound such as 'India and Pakistan' included)."""
    return _countries.name(name)


def iso2(name: Optional[str]) -> str:
    c = _countries.resolve(name)
    return c.iso2.lower() if c else ""


def country_spellings(code: Optional[str]) -> list[str]:
    """Every lower-case country spelling :func:`iso2` maps to one ISO code, aliases included
    ('cn' -> ['china', "people's republic of china", 'prc', 'tibet']); [] for an unknown code.
    Used to filter raw_tours.country by ISO code (catalog.http.v1, AA-976)."""
    c = _countries.by_iso2(code)
    return sorted(c.spellings()) if c else []


def km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


def in_country(lat: float, lng: float, country: Optional[str]) -> bool:
    iso = iso2(country)
    if not iso or iso not in BBOX:
        return False
    lo_lat, hi_lat, lo_lng, hi_lng = BBOX[iso]
    return lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng


def point_ok(lat: Optional[float], lng: Optional[float], country: Optional[str]) -> Optional[str]:
    """None when the point may be stored, else the reason it may not (hard guard). `country` is the PLACE's own
    country (any ISO country), not the tour's."""
    if lat is None or lng is None:
        return "no_point"
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return "out_of_range"
    if canonical_country(country) is None:
        return "unknown_country"
    if not in_country(lat, lng, country):
        return "outside_country_bbox"
    # AA-990: the box alone accepts wrong-country answers next to the border (Kilimanjaro as Kenya, Siem Reap as
    # Thailand, Skardu as India): the point must also lie in the country's polygon (10 km border buffer). Lazy import:
    # shape imports only geomatch, so there is no cycle.
    from services.places import shape
    if not shape.in_country(lat, lng, iso2(country), buffer_km=POLYGON_BUFFER_KM):
        return "outside_country_polygon"
    return None


# ── AA-997: route coherence ─────────────────────────────────────────────────────────────────────────────────────────
# A place a tour visits on day d lies near that tour's route on days d-1..d+1: the overnight towns of those days
# (only geocoder-located or admin-fixed ones) joined in day order. The S254 audit found the wrong pins mostly 5-100 km
# off the route (Kilimanjaro huts, Kong Lor, Jait Sagar) or in another city with the same landmark name (Xi'an's City
# Wall / Bell and Drum Tower placed in Beijing). The tour-median rule `tour_spread` caught them at 23 % precision; the
# distance to the polyline of the neighbouring days' stops is the audit's replacement (fix plan step 2). The distance
# is to the SEGMENTS, not only to the stops, so a site on a long transfer day (between two towns 300 km apart) passes.
ROUTE_KM = 50.0          # a site / village
ROUTE_LONG_KM = 100.0    # a long feature (lake, river, valley, park, trail, road, national ...)
# A day trip from the base town: a geocoder answer off ROUTE_KM but within this of the route is still taken when it
# also agrees with the model's point (geomatch.reference_km `day_trip`; Ayutthaya from Bangkok ~75 km, Mutianyu from
# Beijing ~70 km, Kanchanaburi from Bangkok ~130 km). A homonym in another city (Beijing for Xi'an, ~900 km) is not.
DAY_TRIP_KM = 150.0
Route = tuple            # tuple[tuple[float, float], ...]: (lat, lng) stops in day order


class TourRoute(tuple):
    """A Route (the (lat, lng) stops in day order) that knows where it comes from (AA-997 S258, store.route_anchors):
    `tour` = the tour source id, `anchored` = the day that uses the name has its own located overnight stop in it.
    Everything that takes a Route takes it (it IS the tuple of stops); the strict acceptance rule aa997.3 reads the
    two attributes (geomatch.evidence_routes, route_cover per_tour). A plain tuple counts as anchored."""

    def __new__(cls, stops, *, tour: Optional[str] = None, anchored: bool = True):
        obj = super().__new__(cls, stops)
        obj.tour, obj.anchored = tour, anchored
        return obj

    def __reduce__(self):
        return (_tour_route, (tuple(self), self.tour, self.anchored))


def _tour_route(stops, tour, anchored):
    return TourRoute(stops, tour=tour, anchored=anchored)


def route_radius(name: Optional[str]) -> float:
    from services.places import geomatch
    return ROUTE_LONG_KM if geomatch.radius_km(name or "") > geomatch.ACCEPT_KM else ROUTE_KM


def _segment_km(lat: float, lng: float, a: tuple, b: tuple) -> float:
    """Distance from the point to segment a-b (equirectangular projection around the point: good to a few % for
    segments under ~1,000 km, which is all a guard needs)."""
    k = math.cos(math.radians(lat))
    ax, ay = (a[1] - lng) * k * 111.32, (a[0] - lat) * 110.57
    bx, by = (b[1] - lng) * k * 111.32, (b[0] - lat) * 110.57
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px, py)


def route_km(lat: float, lng: float, routes: Iterable[Route]) -> Optional[float]:
    """The distance (km) from the point to the nearest route (any tour that uses the name); None when no route has a
    stop (then no route check is possible)."""
    best: Optional[float] = None
    for r in routes or ():
        pts = [p for p in r if p is not None]
        if not pts:
            continue
        d = km(lat, lng, pts[0][0], pts[0][1]) if len(pts) == 1 else min(
            _segment_km(lat, lng, pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        best = d if best is None else min(best, d)
    return best


def has_route(routes: Iterable[Route]) -> bool:
    """At least one route with a stop."""
    return any(p is not None for r in (routes or ()) for p in r)


def on_route(lat: float, lng: float, routes: Iterable[Route], name: Optional[str] = None) -> Optional[bool]:
    """True / False against the routes; None when there is no route to check against."""
    d = route_km(lat, lng, routes)
    return None if d is None else d <= route_radius(name)


def route_bias(routes: Iterable[Route]) -> Optional[tuple[float, float]]:
    """A (lat, lng) to bias a geocoder search toward: the middle stop of the first route that has one."""
    for r in routes or ():
        pts = [p for p in r if p is not None]
        if pts:
            return pts[len(pts) // 2]
    return None


def search_bias(routes: Iterable[Route], lat: Optional[float], lng: Optional[float],
                name: Optional[str] = None) -> Optional[tuple[float, float]]:
    """AA-997 v2 (strategy v2): where to bias a search. The model's point when there is no route (it is the only
    reference — v1 sent no bias at all then) or when it lies on the route (closer to the site than a route stop);
    else the stop of the routes nearest to the model's point (the model put the place in another town: search the
    tour's own town), else route_bias."""
    has_hint = lat is not None and lng is not None
    if not has_route(routes):
        return (lat, lng) if has_hint else None
    if not has_hint:
        return route_bias(routes)
    if (route_km(lat, lng, routes) or 0.0) <= route_radius(name):
        return (lat, lng)
    stops = [p for r in routes or () for p in r if p is not None]
    return min(stops, key=lambda p: km(lat, lng, p[0], p[1])) if stops else route_bias(routes)


# ── AA-997: Hong Kong / Macao inside "China", and lenient country text ──────────────────────────────────────────────
# The model (and the tour) says "China" for Hong Kong and Macao; the audit found all 13 HK and 7 MO rows stored as CN.
# Geoapify codes them hk / mo, so a China search must include them, and a point inside their polygon is theirs.
COUNTRY_FAMILY = {"cn": ("cn", "hk", "mo"), "hk": ("hk", "cn"), "mo": ("mo", "cn")}


def search_codes(country: Optional[str]) -> list[str]:
    """ISO alpha-2 codes a search for this country should cover ('China' -> ['cn', 'hk', 'mo'])."""
    code = iso2(country)
    return list(COUNTRY_FAMILY.get(code, (code,))) if code else []


def same_country(a: Optional[str], b: Optional[str]) -> bool:
    """Same ISO country, or one of the CN / HK / MO family (a confirming result in Hong Kong for 'China' agrees)."""
    ia, ib = iso2(a), iso2(b)
    return bool(ia and ib) and (ia == ib or ib in COUNTRY_FAMILY.get(ia, ()))


def refine_country(lat: float, lng: float, country: str) -> str:
    """'China' -> 'Hong Kong' / 'Macao' for a point inside their polygon (no buffer); any other country unchanged."""
    if iso2(country) != "cn":
        return country
    from services.places import shape
    for code in ("hk", "mo"):
        if shape.in_country(lat, lng, code, buffer_km=0):
            return ISO2_COUNTRY[code]
    return country


def lenient_country(text: Optional[str]) -> Optional[str]:
    """canonical_country, then — for compound text such as 'Tibet, China' or 'Lhasa (China)' — the one ISO country its
    parts name (all parts that resolve must agree; 'India and Pakistan' stays None)."""
    one = canonical_country(text)
    if one or not text:
        return one
    import re
    found = {canonical_country(p) for p in re.split(r"[,/;()\[\]]|\s+-\s+", text) if p.strip()}
    found.discard(None)
    return found.pop() if len(found) == 1 else None


@dataclass(frozen=True)
class DayStop:
    day_index: int
    destination_id: str
    lat: float
    lng: float
    transit_day: bool


def outlier_observations(stops: Iterable[DayStop], max_km: float = OUTLIER_KM,
                         window: int = 2) -> list[tuple[str, int, float]]:
    """(destination_id, day_index, km) for each stop farther than `max_km` from EVERY other stop
    within `window` days, where no day of that jump (both ends and any day between) is a transit
    day. Window 2, not 1: with day ±1 only, the correct stop next to a wrong one has a single
    neighbour — the wrong one — and would be flagged too."""
    by_day = {s.day_index: s for s in stops}
    transit = {d for d, s in by_day.items() if s.transit_day}
    out = []
    for d, s in sorted(by_day.items()):
        neighbours = [by_day[n] for n in range(d - window, d + window + 1) if n != d and n in by_day]
        if not neighbours:
            continue
        jumps = []
        for n in neighbours:
            lo, hi = sorted((d, n.day_index))
            dist = km(s.lat, s.lng, n.lat, n.lng)
            if dist <= max_km or any(x in transit for x in range(lo, hi + 1)):
                break
            jumps.append(dist)
        else:
            out.append((s.destination_id, d, round(min(jumps), 1)))
    return out


def is_suspect(outlier_sources: int, located_by: Optional[str], geo_status: Optional[str] = None) -> bool:
    """Design §4: outlier in ≥ 2 tours, or ≥ 1 when only the model located it. Never for 'manual'."""
    if geo_status == "manual":
        return False
    return outlier_sources >= 2 or (outlier_sources >= 1 and located_by == "llm")


def may_overwrite(old_located_by: Optional[str], old_geo_status: Optional[str], new_located_by: str,
                  force: bool = False) -> bool:
    """Coordinates of an existing destination are replaced only when the new point is confirmed by a
    geocoder (geoapify) and the old one was the model's or a legacy TripPlanner 'mapbox' row (stored from
    Mapbox Temporary calls, design §4(b): re-confirm), or the row is unlocated, or an admin forces it. An
    admin-fixed ('manual') row is never overwritten by a job."""
    if old_geo_status == "manual" or old_located_by == "manual":
        return False
    if force:
        return True
    if old_located_by is None:
        return True
    return new_located_by in CONFIRMED_BY and old_located_by in ("llm", "mapbox")
