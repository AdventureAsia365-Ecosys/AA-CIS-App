"""AA-847 (owner decision S246, D1) — geometry of LINE places: rivers, roads and trekking stretches are DRAWN on the
TripPlanner map, never a pin, a circle or hidden. Pure functions only (no I/O, no network): the job kind
`place_shape` (services/jobs/place_shape_job.py) fetches candidates and calls these; tour_graph_build cuts the
per-day stretches with them; the automatic duplicate merge asks `same_line_place`.

Coordinates are GeoJSON order (lng, lat). A line is `Parts` = a list of parts, each a list of (lng, lat) — one part
for a LineString, several for a MultiLineString (a river clipped to a country can leave in several pieces).

  acceptance   `accept(...)`: line geometry ≥ 2 vertices; name match on name / name:en / int_name / name:* after
               diacritics and the river words of the region (song, mae nam, sungai, jiang …) are stripped; within
               25 km of the anchors or the point (50 km for a river, highway or railway); inside the country by
               POLYGON (Natural Earth admin-0 1:10m, geodata/country_polygons.json — guards.BBOX is a rectangle and
               Thailand's holds the Luang Prabang – Vientiane Mekong); a kind-aware minimum length; a single way of a
               relation-backed river loses to the relation (`choose`).
  levels       `levels(parts)`: geom ≤ 2,000 vertices; geom_mid ≤ 300 vertices and ≤ 12 KB; geom_lo ≤ 60 — the
               CHECKs of migration 255.
  stretch      `day_segment(...)`: the part of the line a tour-day follows — between the previous and this day's
               stop when both are ≤ 15 km from the line and ≥ 2 km apart (day_anchors); a ±15 km window around the
               one stop that fits or an out-and-back (anchor_window); otherwise NULL (feature: the reader draws the
               clipped feature).
  approximate  `anchored_line(points)`: the polyline of a day's ordered anchors, for a conceptual route with no
               single OSM feature (Ha Giang Loop, the Laos-side Ho Chi Minh Trail, Tea Horse Route); stored
               status 'approximate', source 'anchors', drawn dashed and labelled "approximate route".
  duplicates   `same_line_place(a, b)` (D2): same country, same kind and class, name match, AND the same OSM id or
               a line overlap (≥ 70 % of the shorter geom_mid within 1 km of the other); two different names merge
               only when both are names of the same OSM feature ("Red River" / "Song Hong"). The point-distance rule
               is never used for lines.
"""
from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Optional, Sequence

from services.places import geomatch

Pt = tuple  # (lng, lat)
Parts = list  # list[list[Pt]]

# ── limits (migration 255 CHECKs; plan §2 / §4) ─────────────────────────────────────────────────────────────────────
MAX_VERTICES = 2000
MID_VERTICES = 300
MID_BYTES = 12288
LO_VERTICES = 60
SEGMENT_VERTICES = 300
DECIMALS = 5
ANCHOR_KM = 25.0                 # the line must pass this close to an anchor or the destination point …
LONG_ANCHOR_KM = 50.0            # … or this close for a river, highway or railway
STOP_ON_LINE_KM = 15.0           # a stop "is on" the line (stretch cutting)
MIN_STRETCH_KM = 2.0             # two stops closer than this along the line are an out-and-back
WINDOW_KM = 15.0                 # ± around one anchor
BORDER_BUFFER_KM = 3.0           # a river that IS the border (Mekong: Laos / Thailand) belongs to both
OVERLAP_KM = 1.0
OVERLAP_SHARE = 0.70
NAME_SIM_MIN = 0.999             # every distinctive token must match ("Nam Ou" ≠ "Nam Khan")
DEFAULT_MIN_KM = 1.0
# kind-aware minimum length by the name's linear word ("Barkhor circuit" is a ~1 km kora; a 300 m way is not a river)
MIN_KM_BY_WORD = {"circuit": 0.5, "path": 0.5, "stream": 0.5, "creek": 0.5, "kora": 0.5,
                  "river": 5.0, "highway": 5.0, "expressway": 5.0, "railway": 5.0, "railroad": 5.0}
LONG_WORDS = frozenset({"river", "highway", "expressway", "railway", "railroad"})
# Region words that mean "river" / "stream" and are not part of the distinctive name (both sides are stripped the
# same way, so "Nam Ou" still differs from "Nam Khan").
RIVER_WORDS = frozenset({"song", "mae", "nam", "sungai", "jiang", "kawa", "gawa", "gang", "cheon", "khlong",
                         "chu", "tsangpo", "nadi", "he", "stung", "tonle", "suoi"})
# … and the words for "road" / "street" ("Đường Hồ Chí Minh" is the Ho Chi Minh road / trail in Vietnam).
ROAD_WORDS = frozenset({"duong", "jalan", "thanon", "lu", "gonglu", "gaosu", "dori", "michi", "kaido", "sadak",
                        "marg", "gil", "daero", "hwy", "rd", "expy", "trl", "rte"})
# … and activity words a trail name carries ("Annapurna Circuit Trek" is the Annapurna Circuit).
ACTIVITY_WORDS = frozenset({"trek", "trekking", "hike", "hiking", "walk", "walking"})
_TOKENS = re.compile(r"[a-z0-9]+")


# ── GeoJSON in / out ──────────────────────────────────────────────────────────────────────────────────────────────

def parts_from_geojson(g: Optional[dict]) -> Parts:
    """(Multi)LineString → parts; anything else → []."""
    if not isinstance(g, dict):
        return []
    t, c = g.get("type"), g.get("coordinates")
    try:
        if t == "LineString":
            raw = [c]
        elif t == "MultiLineString":
            raw = list(c)
        else:
            return []
        out = []
        for part in raw:
            pts = [(float(p[0]), float(p[1])) for p in part]
            pts = _dedupe_consecutive(pts)
            if len(pts) >= 2:
                out.append(pts)
        return out
    except (TypeError, ValueError, IndexError):
        return []


def to_geojson(parts: Parts) -> Optional[dict]:
    parts = [[(round(x, DECIMALS), round(y, DECIMALS)) for x, y in p] for p in parts]
    parts = [q for q in (_dedupe_consecutive(p) for p in parts) if len(q) >= 2]
    if not parts:
        return None
    coords = [[[x, y] for x, y in p] for p in parts]
    if len(coords) == 1:
        return {"type": "LineString", "coordinates": coords[0]}
    return {"type": "MultiLineString", "coordinates": coords}


def _dedupe_consecutive(pts: Sequence[Pt]) -> list:
    out = []
    for p in pts:
        if not out or (p[0], p[1]) != out[-1]:
            out.append((p[0], p[1]))
    return out


def vertex_count(parts: Parts) -> int:
    return sum(len(p) for p in parts)


def json_bytes(g: Optional[dict]) -> int:
    """Size the way PostgreSQL measures it in the CHECK: octet_length(jsonb::text) — jsonb prints ", " and ": "."""
    return len(json.dumps(g, separators=(", ", ": ")).encode()) if g else 0


# ── measures ──────────────────────────────────────────────────────────────────────────────────────────────────────

def km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


def part_length_km(part: Sequence[Pt]) -> float:
    return sum(km(a[1], a[0], b[1], b[0]) for a, b in zip(part, part[1:]))


def length_km(parts: Parts) -> float:
    return sum(part_length_km(p) for p in parts)


def bbox(parts: Parts) -> tuple:
    """(min_lat, min_lng, max_lat, max_lng)."""
    xs = [x for p in parts for x, _ in p]
    ys = [y for p in parts for _, y in p]
    return (min(ys), min(xs), max(ys), max(xs))


def _xy(lat0: float):
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574

    def f(p):
        return p[0] * kx, p[1] * ky
    return f


def _seg_closest(px, py, ax, ay, bx, by):
    """Closest point of segment AB to P in a local plane: (distance, t in [0, 1])."""
    dx, dy = bx - ax, by - ay
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / n))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), t


@dataclass(frozen=True)
class Projection:
    km: float          # distance of the point to the line
    part: int          # which part
    along: float       # km from the start of that part


def project(parts: Parts, lat: float, lng: float) -> Optional[Projection]:
    """Closest point of the line to (lat, lng)."""
    if not parts:
        return None
    f = _xy(lat)
    px, py = f((lng, lat))
    best = None
    for pi, part in enumerate(parts):
        along = 0.0
        for a, b in zip(part, part[1:]):
            ax, ay = f(a)
            bx, by = f(b)
            d, t = _seg_closest(px, py, ax, ay, bx, by)
            seg = km(a[1], a[0], b[1], b[0])
            if best is None or d < best.km:
                best = Projection(d, pi, along + t * seg)
            along += seg
    return best


def distance_km(parts: Parts, lat: float, lng: float) -> float:
    p = project(parts, lat, lng)
    return p.km if p else float("inf")


def substring(part: Sequence[Pt], start_km: float, end_km: float) -> list:
    """The piece of one part between two along-positions (km), in the order asked (end < start reverses it)."""
    rev = end_km < start_km
    lo, hi = (end_km, start_km) if rev else (start_km, end_km)
    total = part_length_km(part)
    lo, hi = max(0.0, lo), min(total, hi)
    out: list = []
    along = 0.0
    for a, b in zip(part, part[1:]):
        seg = km(a[1], a[0], b[1], b[0])
        nxt = along + seg
        if nxt >= lo and along <= hi and seg > 0:
            t0 = max(0.0, (lo - along) / seg)
            t1 = min(1.0, (hi - along) / seg)
            p0 = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
            p1 = (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)
            if not out:
                out.append(p0)
            out.append(p1)
        along = nxt
    out = _dedupe_consecutive(out)
    return list(reversed(out)) if rev else out


# ── simplification ────────────────────────────────────────────────────────────────────────────────────────────────

def _vw(parts: Parts, target: int) -> Parts:
    """Visvalingam-Whyatt over all parts at once (heap, O(n log n)): the vertex whose triangle with its neighbours
    has the smallest area goes first; part ends always stay. Stops at `target` vertices."""
    import heapq
    xy = []
    for part in parts:
        f = _xy(sum(p[1] for p in part) / len(part))
        xy.append([f(p) for p in part])
    prev = [list(range(-1, len(p) - 1)) for p in parts]
    nxt = [list(range(1, len(p) + 1)) for p in parts]
    alive = [[True] * len(p) for p in parts]
    ver = [[0] * len(p) for p in parts]

    def area(pi, i):
        a, b = prev[pi][i], nxt[pi][i]
        if a < 0 or b >= len(parts[pi]):
            return float("inf")
        (x1, y1), (x2, y2), (x3, y3) = xy[pi][a], xy[pi][i], xy[pi][b]
        return abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)) / 2

    heap = [(area(pi, i), pi, i, 0) for pi, p in enumerate(parts) for i in range(1, len(p) - 1)]
    heapq.heapify(heap)
    count = vertex_count(parts)
    while count > target and heap:
        a, pi, i, v = heapq.heappop(heap)
        if not alive[pi][i] or v != ver[pi][i]:
            continue
        alive[pi][i] = False
        count -= 1
        l, r = prev[pi][i], nxt[pi][i]
        nxt[pi][l] = r
        prev[pi][r] = l
        for j in (l, r):
            if 0 < j < len(parts[pi]) - 1 and alive[pi][j]:
                ver[pi][j] += 1
                heapq.heappush(heap, (max(a, area(pi, j)), pi, j, ver[pi][j]))
    return [[p for p, k in zip(part, alive[pi]) if k] for pi, part in enumerate(parts)]


def simplify(parts: Parts, max_vertices: int, max_bytes: Optional[int] = None) -> Parts:
    """Visvalingam-Whyatt down to the vertex cap (and the GeoJSON byte cap, by shrinking the target 15 % at a time).
    When the parts' ends alone exceed the cap, the shortest pieces are dropped first; the longest always stays."""
    if not parts:
        return []

    def fits(ps):
        if vertex_count(ps) > max_vertices:
            return False
        return max_bytes is None or json_bytes(to_geojson(ps)) <= max_bytes

    rounded = [[(round(x, DECIMALS), round(y, DECIMALS)) for x, y in p] for p in parts]
    rounded = [q for q in (_dedupe_consecutive(p) for p in rounded) if len(q) >= 2]
    if not rounded:
        return []
    if fits(rounded):
        return rounded
    rounded.sort(key=part_length_km, reverse=True)
    while 2 * len(rounded) > max_vertices and len(rounded) > 1:
        rounded.pop()                          # drop the shortest piece first
    target = max_vertices
    if max_bytes is not None:                  # ≈ 23 bytes per "[lng, lat], " at 5 decimals
        target = min(target, max(2 * len(rounded), (max_bytes - 64) // 23))
    while target >= 2 * len(rounded):
        out = [q for q in (_dedupe_consecutive(p) for p in _vw(rounded, target)) if len(q) >= 2]
        if out and fits(out):
            return out
        target = int(target * 0.85)
    longest = rounded[0]
    return [[longest[0], longest[-1]]]


def levels(parts: Parts) -> tuple:
    """(geom, geom_mid, geom_lo) GeoJSON dicts within the migration-255 CHECKs."""
    return (to_geojson(simplify(parts, MAX_VERTICES)),
            to_geojson(simplify(parts, MID_VERTICES, MID_BYTES)),
            to_geojson(simplify(parts, LO_VERTICES)))


# ── country polygons (Natural Earth admin-0 1:10m, public domain) ──────────────────────────────────────────────────

_BAND = 0.25    # degrees of latitude per edge band


@dataclass
class _Country:
    bbox: tuple                                    # (min_lat, min_lng, max_lat, max_lng)
    bands: dict = field(default_factory=dict)      # band index -> [(x1, y1, x2, y2)]


def _polygons_path() -> str:
    return os.path.join(os.path.dirname(__file__), "geodata", "country_polygons.json")


@lru_cache(maxsize=1)
def _countries() -> dict:
    with open(_polygons_path(), encoding="utf-8") as fh:
        data = json.load(fh)
    out = {}
    for iso2, polys in data["countries"].items():
        xs, ys, bands = [], [], {}
        for poly in polys:
            for ring in poly:
                for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                    xs += [x1, x2]
                    ys += [y1, y2]
                    for b in range(math.floor(min(y1, y2) / _BAND), math.floor(max(y1, y2) / _BAND) + 1):
                        bands.setdefault(b, []).append((x1, y1, x2, y2))
        out[iso2] = _Country((min(ys), min(xs), max(ys), max(xs)), bands)
    return out


def country_isos() -> list:
    return sorted(_countries())


def _inside_polygon(c: _Country, lat: float, lng: float) -> bool:
    inside = False
    for x1, y1, x2, y2 in c.bands.get(math.floor(lat / _BAND), ()):
        if (y1 > lat) != (y2 > lat):
            x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if x > lng:
                inside = not inside
    return inside


def _near_border(c: _Country, lat: float, lng: float, buffer_km: float) -> bool:
    dlat = buffer_km / 110.574
    f = _xy(lat)
    px, py = f((lng, lat))
    seen = set()
    for b in range(math.floor((lat - dlat) / _BAND), math.floor((lat + dlat) / _BAND) + 1):
        for e in c.bands.get(b, ()):
            if e in seen:
                continue
            seen.add(e)
            x1, y1, x2, y2 = e
            ax, ay = f((x1, y1))
            bx, by = f((x2, y2))
            if _seg_closest(px, py, ax, ay, bx, by)[0] <= buffer_km:
                return True
    return False


def in_country(lat: float, lng: float, iso2: str, buffer_km: float = BORDER_BUFFER_KM) -> bool:
    """Inside the country's polygon, or within `buffer_km` of its border (a border river belongs to both sides).
    An unknown country → False."""
    c = _countries().get((iso2 or "").lower())
    if c is None:
        return False
    pad = buffer_km / 100.0
    if not (c.bbox[0] - pad <= lat <= c.bbox[2] + pad and c.bbox[1] - pad <= lng <= c.bbox[3] + pad):
        return False
    return _inside_polygon(c, lat, lng) or (buffer_km > 0 and _near_border(c, lat, lng, buffer_km))


def clip_to_country(parts: Parts, iso2: str, buffer_km: float = BORDER_BUFFER_KM) -> Parts:
    """The pieces of the line inside the country (polygon + border buffer). A crossing is cut at the border to within
    ~50 m (bisection), so "≈ n km in Laos" is the length inside Laos."""
    out: Parts = []
    for part in parts:
        flags = [in_country(y, x, iso2, buffer_km) for x, y in part]
        cur: list = []
        for i, (p, ok) in enumerate(zip(part, flags)):
            if ok:
                if not cur and i > 0 and not flags[i - 1]:
                    cur.append(_crossing(part[i - 1], p, iso2, buffer_km))
                cur.append(p)
            elif cur:
                cur.append(_crossing(part[i - 1], p, iso2, buffer_km, inside_first=True))
                if len(_dedupe_consecutive(cur)) >= 2:
                    out.append(_dedupe_consecutive(cur))
                cur = []
        if len(_dedupe_consecutive(cur)) >= 2:
            out.append(_dedupe_consecutive(cur))
    return out


def _crossing(a: Pt, b: Pt, iso2: str, buffer_km: float, inside_first: bool = False) -> Pt:
    """Bisection on segment a→b for the last inside / first inside point."""
    lo, hi = 0.0, 1.0
    for _ in range(12):
        mid = (lo + hi) / 2
        p = (a[0] + (b[0] - a[0]) * mid, a[1] + (b[1] - a[1]) * mid)
        if in_country(p[1], p[0], iso2, buffer_km) == inside_first:
            lo = mid
        else:
            hi = mid
    t = lo if inside_first else hi
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


# ── names ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _strip(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return s.replace("đ", "d").replace("Đ", "D").lower()


def name_tokens(name: Optional[str]) -> list:
    """Distinctive tokens: diacritics stripped, the bake-off stop words (river, trail …), the linear words and the
    region's river words dropped — unless nothing would be left."""
    toks = [t for t in _TOKENS.findall(_strip(re.sub(r"\([^)]*\)", " ", name or ""))) if len(t) > 1]
    drop = geomatch.SIM_STOP | geomatch.LINEAR_FEATURE_WORDS | RIVER_WORDS | ROAD_WORDS | ACTIVITY_WORDS
    kept = [t for t in toks if t not in drop]
    return kept or [t for t in toks if t not in geomatch.SIM_STOP] or toks


def name_sim(query: str, candidate: Optional[str]) -> float:
    """Share of the query's distinctive tokens found in the candidate's (a typo-tolerant 0.85 ratio per token)."""
    import difflib
    qt = name_tokens(query)
    ct = set(name_tokens(candidate))
    if not qt or not ct:
        return 0.0
    hit = sum(1 for t in qt if t in ct or (len(t) >= 4 and any(
        difflib.SequenceMatcher(None, t, c).ratio() >= 0.85 for c in ct)))
    return hit / len(qt)


def names_match(query: str, names: Iterable[str]) -> float:
    """Best two-way similarity over a feature's names (name, name:en, int_name, name:*): both directions must
    agree, so "Mekong" matches "Mekong River" but "Nam Ou" does not match "Nam Ou Bridge Road" in the reverse
    direction only. Returns the best min(forward, backward)."""
    best = 0.0
    for n in names:
        if not n:
            continue
        s = min(name_sim(query, n), name_sim(n, query))
        best = max(best, s)
    return best


# ── kinds ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def name_words(name: Optional[str]) -> set:
    return set(_TOKENS.findall(_strip(name or "")))


def min_length_km(name: Optional[str]) -> float:
    w = name_words(name)
    vals = [v for k, v in MIN_KM_BY_WORD.items() if k in w]
    return min(vals) if vals else DEFAULT_MIN_KM


def anchor_radius_km(name: Optional[str], line_class: Optional[str] = None) -> float:
    if name_words(name) & LONG_WORDS or line_class in ("water", "road"):
        return LONG_ANCHOR_KM
    return ANCHOR_KM


def tag_class(tags: Optional[dict]) -> Optional[str]:
    """water / road / trail from OSM tags; None when the tags say nothing linear."""
    t = tags or {}
    if t.get("waterway") or t.get("type") == "waterway":
        return "water"
    route = t.get("route")
    if route in ("hiking", "foot", "bicycle", "mtb") or t.get("highway") in ("path", "footway", "track", "bridleway"):
        return "trail"
    if t.get("railway") or route in ("road", "railway", "train") or t.get("highway"):
        return "road"
    return None


def class_compatible(query_class: str, feature_class: Optional[str]) -> bool:
    """A river name needs a waterway; a road name a road / railway; a trail name (or a conceptual route) may follow
    a hiking route, a path or a road (the Ho Chi Minh Trail is mostly roads)."""
    if feature_class is None:
        return False
    if query_class == "water":
        return feature_class == "water"
    if query_class == "road":
        return feature_class == "road"
    return feature_class in ("trail", "road")


# ── candidates and acceptance ─────────────────────────────────────────────────────────────────────────────────────

@dataclass
class LineCandidate:
    parts: Parts
    names: list                              # name, name:en, int_name, name:* …
    osm_type: Optional[str] = None           # node / way / relation
    osm_id: Optional[int] = None
    tags: dict = field(default_factory=dict)
    source: str = "overpass"                 # geoapify_details / overpass
    member_of: list = field(default_factory=list)   # relation ids a way belongs to (Overpass recurse)
    way_ids: list = field(default_factory=list)     # merged ways

    @property
    def line_class(self) -> Optional[str]:
        return tag_class(self.tags)


@dataclass
class ShapeVerdict:
    accepted: Optional[LineCandidate]
    parts: Parts = field(default_factory=list)       # clipped to the country
    length_km: float = 0.0
    sim: float = 0.0
    anchor_km: Optional[float] = None
    reasons: list = field(default_factory=list)


def _anchor_distance(parts: Parts, anchors: Sequence[tuple]) -> float:
    return min((distance_km(parts, lat, lng) for lat, lng in anchors), default=float("inf"))


def accept(name: str, cands: Sequence[LineCandidate], anchors: Sequence[tuple], iso2: str) -> ShapeVerdict:
    """The best candidate passing every rule (module docstring); reasons per rejected candidate as
    "<index>:<rule>". `anchors` = [(lat, lng)]: the destination point and the stops of the tour-days using it."""
    qclass = geomatch.line_class(name)
    radius = anchor_radius_km(name, qclass)
    min_km = min_length_km(name)
    reasons: list = []
    passed: list = []
    for i, c in enumerate(cands):
        if vertex_count(c.parts) < 2:
            reasons.append(f"{i}:geometry")
            continue
        sim = names_match(name, c.names)
        if sim < NAME_SIM_MIN:
            reasons.append(f"{i}:name")
            continue
        if not class_compatible(qclass, c.line_class):
            reasons.append(f"{i}:kind")
            continue
        clipped = clip_to_country(c.parts, iso2)
        if not clipped:
            reasons.append(f"{i}:country")
            continue
        d = _anchor_distance(clipped, anchors)
        if d > radius:
            reasons.append(f"{i}:distance")
            continue
        length = length_km(clipped)
        if length < min_km:
            reasons.append(f"{i}:length")
            continue
        passed.append((i, c, clipped, length, sim, d))
    chosen = choose(passed)
    for i, c, *_ in passed:
        if chosen is None or c is not chosen[1]:
            reasons.append(f"{i}:not_preferred")
    if chosen is None:
        return ShapeVerdict(None, reasons=reasons)
    _, c, clipped, length, sim, d = chosen
    return ShapeVerdict(c, clipped, round(length, 1), round(sim, 3), round(d, 2), reasons)


def choose(passed: list):
    """Among the accepted candidates: a relation beats merged ways beats a single way (a single way of a river is one
    OSM piece; a relation is the river), then the longer one inside the country. A single way that is a member of an
    accepted relation is never chosen over it."""
    if not passed:
        return None
    rel_ids = {c.osm_id for _, c, *_ in passed if c.osm_type == "relation"}

    def rank(row):
        _, c, _, length, _, d = row
        if c.osm_type == "relation":
            r = 0
        elif len(c.way_ids) > 1:
            r = 1
        else:
            r = 2
        member_penalty = 1 if (c.osm_type != "relation" and set(c.member_of) & rel_ids) else 0
        return (member_penalty, r, -length, d)
    best = min(passed, key=rank)
    return best


# ── merging connected same-name ways (Overpass) ───────────────────────────────────────────────────────────────────

JOIN_KM = 0.05


def merge_ways(ways: Sequence[LineCandidate]) -> list:
    """Ways with the same normalised name joined end to end into chains; each name group becomes one candidate
    (MultiLineString when chains do not connect). Ways keep their order inside a chain."""
    groups: dict = {}
    for w in ways:
        groups.setdefault((_group_key(w.names), w.line_class), []).append(w)
    out = []
    for (_, _), ws in groups.items():
        if len(ws) == 1:
            out.append(ws[0])
            continue
        chains = _chain([p for w in ws for p in w.parts])
        first = max(ws, key=lambda w: length_km(w.parts))
        names = list(dict.fromkeys(n for w in ws for n in w.names if n))
        tags = dict(first.tags)
        member_of = sorted({r for w in ws for r in w.member_of})
        out.append(LineCandidate(chains, names, "way", first.osm_id, tags, first.source, member_of,
                                 [w.osm_id for w in ws if w.osm_id is not None]))
    return out


def _group_key(names: Sequence[str]) -> str:
    """The first name with Latin tokens (name:en / int_name when `name` is in Lao, Thai, Chinese …), else the raw
    name: ways named "ແມ່ນ້ຳຂອງ" / "Mekong" and "ນ້ຳຄານ" / "Nam Khan" must not fall in one group."""
    for n in names:
        toks = [t for t in _TOKENS.findall(_strip(n or "")) if len(t) > 1]
        if toks:
            return " ".join(name_tokens(n))
    return (names[0] if names else "").strip().lower()


def _close(a: Pt, b: Pt) -> bool:
    return km(a[1], a[0], b[1], b[0]) <= JOIN_KM


def _chain(parts: Parts) -> Parts:
    pool = [list(p) for p in parts if len(p) >= 2]
    chains = []
    while pool:
        cur = pool.pop(0)
        grown = True
        while grown:
            grown = False
            for i, p in enumerate(pool):
                if _close(cur[-1], p[0]):
                    cur += p[1:]
                elif _close(cur[-1], p[-1]):
                    cur += list(reversed(p))[1:]
                elif _close(cur[0], p[-1]):
                    cur = p[:-1] + cur
                elif _close(cur[0], p[0]):
                    cur = list(reversed(p))[:-1] + cur
                else:
                    continue
                pool.pop(i)
                grown = True
                break
        chains.append(cur)
    return chains


# ── a tour-day's stretch (geo.tour_segment) ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DaySegment:
    basis: str                       # day_anchors / anchor_window / feature
    segment: Optional[dict]          # GeoJSON LineString ≤ 300 vertices, None for 'feature'
    segment_km: Optional[float]
    from_destination_id: Optional[str]
    to_destination_id: Optional[str]


def day_segment(parts: Parts, prev_stop: Optional[tuple], this_stop: Optional[tuple]) -> DaySegment:
    """`prev_stop` / `this_stop` = (destination_id, lat, lng) of the previous day's stop and this day's (either may be
    None). `parts` = the drawable shape (geom), [] when there is none yet. The ids say which anchors the stretch runs
    between; with 'feature' they are the day's two stops, so a reader without a shape can still draw the provisional
    dashed line between them (D1: never nothing)."""
    pid = prev_stop[0] if prev_stop else None
    tid = this_stop[0] if this_stop else None
    if not parts:
        return DaySegment("feature", None, None, pid, tid)
    pp = project(parts, prev_stop[1], prev_stop[2]) if prev_stop else None
    tp = project(parts, this_stop[1], this_stop[2]) if this_stop else None
    p_ok = pp is not None and pp.km <= STOP_ON_LINE_KM
    t_ok = tp is not None and tp.km <= STOP_ON_LINE_KM
    if p_ok and t_ok and pp.part == tp.part and abs(pp.along - tp.along) >= MIN_STRETCH_KM:
        seg = substring(parts[pp.part], pp.along, tp.along)
        return _segment("day_anchors", seg, pid, tid)
    if p_ok or t_ok:
        # one anchor fits, or both coincide (an out-and-back boat trip from Nong Khiaw): ± WINDOW_KM around it.
        # The day's own stop wins: it is where the day is.
        pr, did = (tp, tid) if t_ok else (pp, pid)
        part = parts[pr.part]
        seg = substring(part, pr.along - WINDOW_KM, pr.along + WINDOW_KM)
        return _segment("anchor_window", seg, did, did)
    return DaySegment("feature", None, None, pid, tid)


def _segment(basis: str, seg: list, from_id, to_id) -> DaySegment:
    seg = _dedupe_consecutive(seg)
    if len(seg) < 2:
        return DaySegment("feature", None, None, from_id, to_id)
    simple = simplify([seg], SEGMENT_VERTICES)
    g = to_geojson(simple[:1])
    if g is None:
        return DaySegment("feature", None, None, from_id, to_id)
    return DaySegment(basis, g, round(part_length_km(seg), 1), from_id, to_id)


# ── approximate route from a day's anchors ────────────────────────────────────────────────────────────────────────

def anchored_line(points: Sequence[tuple], iso2: Optional[str] = None) -> Parts:
    """(lat, lng) points in travel order → a one-part polyline, consecutive points < 0.5 km apart merged; [] when
    fewer than 2 distinct points ≥ MIN_STRETCH_KM apart overall. Points outside the country are dropped when iso2
    is given."""
    pts = []
    for lat, lng in points:
        if iso2 and not in_country(lat, lng, iso2):
            continue
        if pts and km(pts[-1][1], pts[-1][0], lat, lng) < 0.5:
            continue
        pts.append((lng, lat))
    if len(pts) < 2:
        return []
    span = max(km(a[1], a[0], b[1], b[0]) for a in pts for b in pts)
    return [pts] if span >= MIN_STRETCH_KM else []


def order_along(start: tuple, points: Sequence[tuple]) -> list:
    """Points (lat, lng) ordered by distance from `start` (a day's middle places between its two stops)."""
    return sorted(points, key=lambda p: km(start[0], start[1], p[0], p[1]))


# ── duplicates (D2) ───────────────────────────────────────────────────────────────────────────────────────────────

LINE_DUP_NAME_SIM = 0.999


@dataclass(frozen=True)
class LinePlace:
    destination_id: str
    name: str
    country: str
    kind: str = "line"                     # place_shape.kind
    line_class: Optional[str] = None       # water / road / trail (geomatch.line_class or the OSM tags)
    osm_type: Optional[str] = None
    osm_id: Optional[int] = None
    geom_mid: Optional[dict] = None
    osm_names: tuple = ()                  # the OSM feature's names (place_shape.meta.names: name, name:en, name:vi …)


def overlap_share(a: Parts, b: Parts, within_km: float = OVERLAP_KM) -> float:
    """Share (by length) of the SHORTER line lying within `within_km` of the other, sampled every ~0.5 km."""
    if not a or not b:
        return 0.0
    short, other = (a, b) if length_km(a) <= length_km(b) else (b, a)
    total = near = 0.0
    for part in short:
        for p, q in zip(part, part[1:]):
            seg = km(p[1], p[0], q[1], q[0])
            n = max(1, int(seg / 0.5))
            for k in range(n):
                t = (k + 0.5) / n
                x, y = p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t
                total += seg / n
                if distance_km(other, y, x) <= within_km:
                    near += seg / n
    return near / total if total else 0.0


def same_line_place(a: LinePlace, b: LinePlace) -> tuple:
    """(same, why). Same place = same country AND same kind AND name match (both directions) AND (same OSM id OR
    ≥ 70 % overlap). "Mekong River (Thailand)" and "Mekong River" in Laos are different places (different
    countries, D2 keeps them apart — the same OSM relation in two countries is legitimate)."""
    if (a.country or "").strip().lower() != (b.country or "").strip().lower():
        return False, "country"
    if a.kind != b.kind:
        return False, "kind"
    ca = a.line_class or geomatch.line_class(a.name)
    cb = b.line_class or geomatch.line_class(b.name)
    if ca != cb:
        return False, "class"
    same_osm = a.osm_id is not None and a.osm_id == b.osm_id and a.osm_type == b.osm_type
    if names_match(a.name, [b.name]) < LINE_DUP_NAME_SIM:
        # "Red River" / "Song Hong", "Black River" / "Da River": different words, but both are names OF the same OSM
        # feature (its name, name:en, name:vi …) — then the shared OSM id is the evidence
        osm_names = list(a.osm_names) + [n for n in b.osm_names if n not in a.osm_names]
        if same_osm and osm_names and names_match(a.name, osm_names) >= LINE_DUP_NAME_SIM \
                and names_match(b.name, osm_names) >= LINE_DUP_NAME_SIM:
            return True, "osm_id_names"
        return False, "name"
    if same_osm:
        return True, "osm_id"
    pa, pb = parts_from_geojson(a.geom_mid), parts_from_geojson(b.geom_mid)
    if pa and pb and overlap_share(pa, pb) >= OVERLAP_SHARE:
        return True, "overlap"
    return False, "no_geometry_evidence" if not (pa and pb) else "no_overlap"
