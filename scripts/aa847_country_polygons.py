"""AA-847 — build services/places/geodata/country_polygons.json from Natural Earth admin-0 1:10m.

Natural Earth is public domain (https://www.naturalearthdata.com/about/terms-of-use/). The output holds only the
catalog's countries (services/places/guards.ISO2_COUNTRY), simplified (Douglas-Peucker, DEFAULT_TOL_DEG ≈ 1 km)
and rounded to 4 decimals (≈ 11 m), so the file stays small and pure-Python point-in-polygon stays fast.
It is what services/places/shape.py clips a river / road / trail with ("≈ n km in Laos"); guards.BBOX is a
rectangle and cannot do it (Thailand's box holds the Luang Prabang – Vientiane Mekong).

Run offline, once, when the country list changes (no network from the script itself):
    curl -sSfL -o /tmp/ne/ne10.geojson \
      https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_admin_0_countries.geojson
    python3 -I scripts/aa847_country_polygons.py /tmp/ne/ne10.geojson services/places/geodata/country_polygons.json
"""
from __future__ import annotations

import json
import sys

ISO3_ISO2 = {"BTN": "bt", "KHM": "kh", "CHN": "cn", "IND": "in", "IDN": "id", "JPN": "jp", "KGZ": "kg", "LAO": "la",
             "MYS": "my", "MNG": "mn", "MMR": "mm", "NPL": "np", "KOR": "kr", "LKA": "lk", "TWN": "tw", "THA": "th",
             "UZB": "uz", "VNM": "vn"}
DEFAULT_TOL_DEG = 0.01
MIN_RING_VERTICES = 4          # a ring simplified below this (a rock) is dropped
SOURCE = "Natural Earth 1:10m admin-0 countries v5.1.2 (public domain)"


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


def build(src: dict, tol: float = DEFAULT_TOL_DEG) -> dict:
    countries: dict = {}
    for f in src["features"]:
        p = f.get("properties") or {}
        iso2 = ISO3_ISO2.get(p.get("ADM0_A3")) or ISO3_ISO2.get(p.get("ISO_A3"))
        if not iso2:
            continue
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        out = []
        for poly in polys:
            rings = [simplify_ring(r, tol) for r in poly]
            if len(rings[0]) < MIN_RING_VERTICES:
                continue
            out.append([r for r in rings if len(r) >= MIN_RING_VERTICES])
        countries.setdefault(iso2, []).extend(out)
    return {"source": SOURCE, "tolerance_deg": tol, "countries": dict(sorted(countries.items()))}


def main(argv: list) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        src = json.load(fh)
    out = build(src)
    missing = sorted(set(ISO3_ISO2.values()) - set(out["countries"]))
    if missing:
        print(f"missing countries: {missing}", file=sys.stderr)
        return 1
    with open(argv[2], "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
        fh.write("\n")
    n = sum(len(r) for polys in out["countries"].values() for poly in polys for r in poly)
    print(f"{len(out['countries'])} countries, {n} vertices -> {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
