"""AA-847 / AA-990 — country polygons (services/places/geodata/country_polygons.json) from Natural Earth 1:10m.

Natural Earth is public domain (https://www.naturalearthdata.com/about/terms-of-use/). The output holds EVERY ISO
3166-1 country Natural Earth draws (AA-990: shared.countries, no hand-made AA list), simplified (Douglas-Peucker,
DEFAULT_TOL_DEG ≈ 1 km) and rounded to 4 decimals (≈ 11 m), so pure-Python point-in-polygon stays fast. Each
country's rings are stored as ONE JSON string, so services/places/shape.py parses only the countries a run touches.
It is what shape.py clips a river / road / trail with ("≈ n km in Laos"); a bounding box is a rectangle and cannot
do it (Thailand's box holds the Luang Prabang – Vientiane Mekong).

This module is the polygon half of scripts/aa990_country_data.py, which reads the ISO codes from the migrations and
writes both data files. Run that script (offline, no network from the script itself):
    curl -sSfL -o /tmp/ne/ne10.geojson \
      https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_admin_0_countries.geojson
    python3 -I scripts/aa990_country_data.py /tmp/ne/ne10.geojson
"""
from __future__ import annotations

import json

DEFAULT_TOL_DEG = 0.01
MIN_RING_VERTICES = 4          # a ring simplified below this (a rock) is dropped
SOURCE = "Natural Earth 1:10m admin-0 countries v5.1.2 (public domain)"
# Natural Earth features with no ISO code that lie inside ISO countries (ADMIN -> ISO alpha-2 codes, lower case).
# Siachen Glacier is between Ladakh (India) and Baltistan (Pakistan), both AA trekking areas: it goes to both.
# The rest of the code-less features (Bir Tawil, Spratly / Scarborough / Bajo Nuevo / Serranilla, Kosovo, which
# ISO has no code for) are left out.
FOLD = {
    "Siachen Glacier": ("in", "pk"),
    "Somaliland": ("so",),
    "Northern Cyprus": ("cy",),
    "Cyprus No Mans Area": ("cy",),
    "Dhekelia Sovereign Base Area": ("cy",),
    "Akrotiri Sovereign Base Area": ("cy",),
    "US Naval Base Guantanamo Bay": ("cu",),
    "Southern Patagonian Ice Field": ("ar", "cl"),
}


def _dp(points: list, tol: float) -> list:
    """Douglas-Peucker on a list of [x, y] (iterative, keeps the first and last point)."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            d = (abs(dy * px - dx * py + bx * ay - by * ax) / norm) if norm else ((px - ax) ** 2 + (py - ay) ** 2) ** .5
            if d > best:
                best, idx = d, i
        if idx > 0 and best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def simplify_ring(ring: list, tol: float) -> list:
    out = _dp([[round(x, 4), round(y, 4)] for x, y in ring], tol)
    if out[0] != out[-1]:
        out.append(out[0])
    return out


def feature_codes(props: dict, codes: set) -> tuple:
    """The ISO alpha-2 codes (lower case) a Natural Earth feature belongs to: ISO_A2_EH, then ISO_A2, then WB_A2 when
    it is an ISO code (`codes`, upper case); FOLD for the code-less features inside ISO countries; () otherwise."""
    for key in ("ISO_A2_EH", "ISO_A2", "WB_A2"):
        v = (props.get(key) or "").upper()
        if v in codes:
            return (v.lower(),)
    return FOLD.get(props.get("ADMIN") or "", ())


def _polygons(geometry: dict, tol: float) -> list:
    polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    out = []
    for poly in polys:
        rings = [simplify_ring(r, tol) for r in poly]
        if len(rings[0]) < MIN_RING_VERTICES:
            continue
        out.append([r for r in rings if len(r) >= MIN_RING_VERTICES])
    return out


def build(src: dict, codes: set, tol: float = DEFAULT_TOL_DEG) -> dict:
    """{iso2: [polygon [ring [[lng, lat], …]]]} for every feature whose code is in `codes` (ISO alpha-2, upper). A
    country that `tol` would simplify away entirely (the Holy See, Monaco: smaller than the tolerance) keeps its
    rings unsimplified (4-decimal rounding only)."""
    countries: dict = {}
    for f in src["features"]:
        targets = feature_codes(f.get("properties") or {}, codes)
        if not targets:
            continue
        out = _polygons(f["geometry"], tol)
        for iso2 in targets:
            countries.setdefault(iso2, []).extend(out)
    empty = {code for code, polys in countries.items() if not polys}
    if empty:
        for f in src["features"]:
            targets = [c for c in feature_codes(f.get("properties") or {}, codes) if c in empty]
            out = _polygons(f["geometry"], 0.0) if targets else []
            for iso2 in targets:
                countries[iso2].extend(out)
    return dict(sorted((k, v) for k, v in countries.items() if v))


def polygons_file(countries: dict, tol: float = DEFAULT_TOL_DEG) -> dict:
    """The JSON document shape.py reads: each country's polygons as one compact JSON string (lazy parse)."""
    return {"source": SOURCE, "tolerance_deg": tol,
            "countries": {iso2: json.dumps(polys, separators=(",", ":")) for iso2, polys in countries.items()}}


def bbox(polys: list) -> tuple:
    """(min_lat, max_lat, min_lng, max_lng) of every vertex of every ring."""
    xs = [x for poly in polys for ring in poly for x, _ in ring]
    ys = [y for poly in polys for ring in poly for _, y in ring]
    return min(ys), max(ys), min(xs), max(xs)
