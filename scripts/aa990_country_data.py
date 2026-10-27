"""AA-990 — build the in-code mirror of shared.countries and the country polygons, for EVERY ISO 3166-1 country.

Nghiệp (S252): a tour's country is what its source says, any ISO country; a place's country is where it really is, so
a place may lie in another country than its tour (cross-border). No hand-made "AA country" list restricts anything.

Writes, from files only (no network, no DB; run with `python3 -I`):
  1. shared/countries/countries.json — one entry per shared.countries row as the migrations write it (CIS 279's
     VALUES + 288's alias additions), plus derived fields:
       bbox  [min_lat, max_lat, min_lng, max_lng]: the Natural Earth polygon's box grown by BBOX_PAD_DEG, or for the
             countries whose box was tuned by hand before AA-990 (BBOX_TUNED), that box; null when NE has no polygon;
       zones [[IANA zone, lat, lng], …]: the country's zones with their reference point, from tzdata zone.tab, in
             its order (first = the most populous zone).
  2. services/places/geodata/country_polygons.json — Natural Earth admin-0 1:10m v5.1.2 polygons of every ISO country
     (scripts/aa847_country_polygons.py), the 11 overseas / dependent codes admin-0 does not draw taken from
     admin_0_map_units.

    NE=https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson
    mkdir -p /tmp/ne && cd /tmp/ne
    curl -sSfLO $NE/ne_10m_admin_0_countries.geojson && curl -sSfLO $NE/ne_10m_admin_0_map_units.geojson
    python3 -I scripts/aa990_country_data.py /tmp/ne/ne_10m_admin_0_countries.geojson \
        /tmp/ne/ne_10m_admin_0_map_units.geojson [/usr/share/zoneinfo/zone.tab]

Without the two geojson paths (`--rows-only`) it rewrites countries.json keeping the bbox / zones already there (an
alias-only change). tests/unit/test_aa990_country_mirror.py checks the JSON against the migrations.
"""
from __future__ import annotations

import gc
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "api" / "migrations"
ISO_MIGRATION = "279_countries_iso3166.sql"
ALIAS_MIGRATIONS = ("288_raw_tours_country_iso.sql",)
COUNTRIES_JSON = REPO / "shared" / "countries" / "countries.json"
POLYGONS_JSON = REPO / "services" / "places" / "geodata" / "country_polygons.json"
ZONE_TAB = "/usr/share/zoneinfo/zone.tab"
BBOX_PAD_DEG = 0.3
# (min_lat, max_lat, min_lng, max_lng): the boxes tuned by hand before AA-990 (services/places/guards.py, AA-712 /
# AA-990 F3). Kinmen / Matsu are Taiwan, Andaman is India; Pakistan's east edge stops short of Leh / Nubra (the
# Natural Earth box of Pakistan, with Siachen, would hold them). Kept: tuned data, not an allow-list.
BBOX_TUNED = {
    "bt": (26.4, 28.6, 88.5, 92.4), "kh": (9.6, 15.0, 102.0, 108.0), "cn": (17.8, 53.9, 73.2, 135.4),
    "in": (6.2, 37.4, 67.8, 97.7), "jp": (23.8, 45.9, 122.6, 146.2), "la": (13.6, 22.8, 99.8, 107.9),
    "mn": (41.3, 52.4, 87.4, 120.2), "mm": (9.3, 28.9, 91.9, 101.5), "np": (26.1, 30.8, 79.8, 88.5),
    "kr": (32.8, 38.9, 124.3, 132.1), "lk": (5.6, 10.1, 79.3, 82.2), "tw": (21.6, 26.6, 118.0, 122.4),
    "th": (5.3, 20.8, 97.1, 105.9), "vn": (8.2, 23.7, 101.9, 109.8),
    "id": (-11.3, 6.4, 94.7, 141.4), "my": (0.6, 7.7, 99.3, 119.6), "kg": (38.9, 43.6, 69.0, 80.6),
    "uz": (36.9, 45.9, 55.7, 73.5), "pk": (23.3, 37.4, 60.6, 77.4), "ph": (4.3, 21.4, 116.6, 126.9),
}

_ROW = re.compile(r"\(\s*'([A-Z]{2})',\s*'([A-Z]{3})',\s*'(\d{3})',\s*'((?:[^']|'')*)',\s*'((?:[^']|'')*)',\s*"
                  r"(?:ARRAY\[(.*?)\]::text\[\]|'\{\}'::text\[\])\s*\)", re.S)
_ALIAS_ROW = re.compile(r"\(\s*'([A-Z]{2})',\s*ARRAY\[(.*?)\]::text\[\]\s*\)", re.S)
_STR = re.compile(r"'((?:[^']|'')*)'")
# 288 (and any later alias migration) marks its alias VALUES list with these two comment lines.
ALIAS_BEGIN, ALIAS_END = "-- aa990:aliases:begin", "-- aa990:aliases:end"


def _unq(s: str) -> str:
    return s.replace("''", "'")


def iso_rows(migrations: Path = MIGRATIONS) -> list[dict]:
    """The rows CIS 279 seeds into shared.countries, then the aliases each later alias migration appends."""
    sql = (migrations / ISO_MIGRATION).read_text(encoding="utf-8")
    start = sql.index("INSERT INTO shared.countries (iso2")
    block = sql[start:sql.index("ON CONFLICT (iso2) DO NOTHING;", start)]
    rows = [{"iso2": a, "iso3": b, "numeric": n, "name": _unq(name), "iso_name": _unq(iname),
             "aliases": [_unq(x) for x in _STR.findall(al or "")]}
            for a, b, n, name, iname, al in _ROW.findall(block)]
    by = {r["iso2"]: r for r in rows}
    for extra in alias_additions(migrations):
        r = by[extra[0]]
        r["aliases"] = sorted(set(r["aliases"]) | set(extra[1]))
    return rows


def alias_additions(migrations: Path = MIGRATIONS) -> list[tuple[str, list[str]]]:
    """(iso2, [aliases]) that the alias migrations add (288's marked VALUES list)."""
    out = []
    for name in ALIAS_MIGRATIONS:
        path = migrations / name
        if not path.exists():
            continue
        sql = path.read_text(encoding="utf-8")
        block = sql[sql.index(ALIAS_BEGIN):sql.index(ALIAS_END)]
        out += [(iso2, [_unq(x) for x in _STR.findall(arr)]) for iso2, arr in _ALIAS_ROW.findall(block)]
    return out


def _coord(s: str, deg_digits: int) -> float:
    sign = -1.0 if s[0] == "-" else 1.0
    s = s[1:]
    d, m, sec = int(s[:deg_digits]), int(s[deg_digits:deg_digits + 2]), int(s[deg_digits + 2:] or 0)
    return round(sign * (d + m / 60 + sec / 3600), 4)


def zones(zone_tab: str) -> dict:
    """{ISO2: [[zone, lat, lng], …]} from tzdata zone.tab (one country code per line), in zone.tab's order (its first
    zone of a country is the most populous one, e.g. America/New_York): the default when no point is known."""
    out: dict = {}
    for line in Path(zone_tab).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        code, coords, zone = line.split("\t")[:3]
        m = re.match(r"([+-]\d+)([+-]\d+)$", coords)
        lat_s, lng_s = m.group(1), m.group(2)
        lat = _coord(lat_s, 2)
        lng = _coord(lng_s, 3)
        out.setdefault(code, []).append([zone, lat, lng])
    return out


def _polygon_module():
    spec = importlib.util.spec_from_file_location("aa847_country_polygons",
                                                  Path(__file__).with_name("aa847_country_polygons.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def polygons(countries_geojson: str, map_units_geojson: str, codes: set) -> dict:
    poly = _polygon_module()
    with open(countries_geojson, encoding="utf-8") as fh:
        src = json.load(fh)
    out = poly.build(src, codes)
    del src
    gc.collect()
    missing = {c for c in codes if c.lower() not in out}
    if missing:
        with open(map_units_geojson, encoding="utf-8") as fh:
            src = json.load(fh)
        src["features"] = [f for f in src["features"]
                           if (f.get("properties") or {}).get("ISO_A2_EH", "").upper() in missing]
        out.update(poly.build(src, missing))
        del src
        gc.collect()
    return dict(sorted(out.items()))


def _padded(box: tuple) -> list:
    lo_lat, hi_lat, lo_lng, hi_lng = box
    return [round(max(-90.0, lo_lat - BBOX_PAD_DEG), 1), round(min(90.0, hi_lat + BBOX_PAD_DEG), 1),
            round(max(-180.0, lo_lng - BBOX_PAD_DEG), 1), round(min(180.0, hi_lng + BBOX_PAD_DEG), 1)]


def countries_doc(rows: list[dict], boxes: dict, zone_map: dict) -> dict:
    out = []
    for r in sorted(rows, key=lambda r: r["iso2"]):
        code = r["iso2"].lower()
        box = BBOX_TUNED.get(code)
        out.append({**r, "aliases": sorted(r["aliases"]),
                    "bbox": list(box) if box else boxes.get(code),
                    "zones": zone_map.get(r["iso2"], [])})
    return {"source": "shared.countries (CIS 279 + 288, ISO 3166-1, Debian iso-codes 4.16.0); bbox: Natural Earth "
                      "1:10m v5.1.2 (public domain); zones: tzdata zone.tab. Generated by "
                      "scripts/aa990_country_data.py, never edited by hand.",
            "countries": out}


def write_json(path: Path, doc: dict, compact: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if compact:
            json.dump(doc, fh, separators=(",", ":"), ensure_ascii=False)
        else:
            fh.write("{\n" + f'"source": {json.dumps(doc["source"], ensure_ascii=False)},\n"countries": [\n')
            fh.write(",\n".join(json.dumps(c, ensure_ascii=False, separators=(", ", ": "))
                                for c in doc["countries"]))
            fh.write("\n]}")
        fh.write("\n")


def main(argv: list) -> int:
    rows = iso_rows()
    if len(argv) >= 2 and argv[1] == "--rows-only":
        old = {c["iso2"]: c for c in json.loads(COUNTRIES_JSON.read_text(encoding="utf-8"))["countries"]}
        boxes = {k.lower(): v.get("bbox") for k, v in old.items()}
        zone_map = {k: v.get("zones") or [] for k, v in old.items()}
        write_json(COUNTRIES_JSON, countries_doc(rows, boxes, zone_map), compact=False)
        print(f"{len(rows)} countries -> {COUNTRIES_JSON} (bbox / zones kept)")
        return 0
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    codes = {r["iso2"] for r in rows}
    polys = polygons(argv[1], argv[2], codes)
    poly = _polygon_module()
    boxes = {code: _padded(poly.bbox(p)) for code, p in polys.items() if p}
    write_json(POLYGONS_JSON, poly.polygons_file(polys), compact=True)
    zone_map = zones(argv[3] if len(argv) == 4 else ZONE_TAB)
    write_json(COUNTRIES_JSON, countries_doc(rows, boxes, zone_map), compact=False)
    no_poly = sorted(c for c in codes if c.lower() not in polys)
    print(f"{len(rows)} countries ({len(polys)} with a polygon; none for {no_poly}) -> {COUNTRIES_JSON}, "
          f"{POLYGONS_JSON} ({POLYGONS_JSON.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
