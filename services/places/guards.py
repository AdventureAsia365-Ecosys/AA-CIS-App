"""AA-712 — quality guards for coordinates (design §4).

Hard (a point failing these is never stored):
  - the country is one AA sells (or a neighbour it crosses into) — ISO list from TripPlanner
    geocode.py, and the point lies inside that country's bounding box;
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

# Country name (lowercase) -> ISO 3166-1 alpha-2: the ONE list of countries AA sells (every live tour's country).
# TripPlanner geocode.COUNTRY_ISO had 14 countries; AA-712 added Indonesia / Malaysia / Kyrgyzstan / Uzbekistan
# (migration 150), AA-990 Pakistan / Philippines (migration 197). It equals the chk_raw_tours_country constraint:
# tests/unit/test_aa990_country_lists.py parses the latest migration that defines it. Tibet is China.
# Every other CIS country list imports from here (aa_content.validate, catalog_http.geo, places.geocoder / shape).
COUNTRY_ISO2 = {
    "bhutan": "bt", "cambodia": "kh", "china": "cn", "india": "in", "indonesia": "id", "japan": "jp",
    "kyrgyzstan": "kg", "laos": "la", "malaysia": "my", "mongolia": "mn", "myanmar": "mm", "nepal": "np",
    "pakistan": "pk", "philippines": "ph", "south korea": "kr", "sri lanka": "lk", "taiwan": "tw",
    "thailand": "th", "tibet": "cn", "uzbekistan": "uz", "vietnam": "vn",
}
ISO2_COUNTRY = {"bt": "Bhutan", "kh": "Cambodia", "cn": "China", "in": "India", "id": "Indonesia", "jp": "Japan",
                "kg": "Kyrgyzstan", "la": "Laos", "my": "Malaysia", "mn": "Mongolia", "mm": "Myanmar",
                "np": "Nepal", "pk": "Pakistan", "ph": "Philippines", "kr": "South Korea", "lk": "Sri Lanka",
                "tw": "Taiwan", "th": "Thailand", "uz": "Uzbekistan", "vn": "Vietnam"}
ISO2_ISO3 = {"bt": "BTN", "kh": "KHM", "cn": "CHN", "in": "IND", "id": "IDN", "jp": "JPN", "kg": "KGZ",
             "la": "LAO", "my": "MYS", "mn": "MNG", "mm": "MMR", "np": "NPL", "pk": "PAK", "ph": "PHL",
             "kr": "KOR", "lk": "LKA", "tw": "TWN", "th": "THA", "uz": "UZB", "vn": "VNM"}
ISO3_ISO2 = {v: k for k, v in ISO2_ISO3.items()}
AA_ISO2 = sorted(ISO2_COUNTRY)
# Known dirty raw_tours.country values (TripPlanner country_normalize.py) and common variants.
# Each one must also be a shared.countries alias (CIS 279): tests/integration/test_aa851_migration_279.py.
_ALIASES = {"sri-landka": "sri lanka", "okinawa": "japan", "viet nam": "vietnam", "korea": "south korea",
            "republic of korea": "south korea", "lao pdr": "laos", "burma": "myanmar",
            "kyrgyz republic": "kyrgyzstan", "republic of the philippines": "philippines",
            "islamic republic of pakistan": "pakistan"}

# (min_lat, max_lat, min_lng, max_lng), generous by ~0.3°. Kinmen/Matsu are Taiwan; Andaman is India.
BBOX = {
    "bt": (26.4, 28.6, 88.5, 92.4), "kh": (9.6, 15.0, 102.0, 108.0), "cn": (17.8, 53.9, 73.2, 135.4),
    "in": (6.2, 37.4, 67.8, 97.7), "jp": (23.8, 45.9, 122.6, 146.2), "la": (13.6, 22.8, 99.8, 107.9),
    "mn": (41.3, 52.4, 87.4, 120.2), "mm": (9.3, 28.9, 91.9, 101.5), "np": (26.1, 30.8, 79.8, 88.5),
    "kr": (32.8, 38.9, 124.3, 132.1), "lk": (5.6, 10.1, 79.3, 82.2), "tw": (21.6, 26.6, 118.0, 122.4),
    "th": (5.3, 20.8, 97.1, 105.9), "vn": (8.2, 23.7, 101.9, 109.8),
    "id": (-11.3, 6.4, 94.7, 141.4), "my": (0.6, 7.7, 99.3, 119.6), "kg": (38.9, 43.6, 69.0, 80.6),
    "uz": (36.9, 45.9, 55.7, 73.5),
    # Pakistan 23.6-37.1 N / 60.9-77.8 E (Gilgit-Baltistan to Karakoram), Philippines 4.6-21.1 N / 116.9-126.6 E
    # (Tawi-Tawi to Batanes, Palawan to Mindanao's east coast) — AA-990.
    "pk": (23.3, 37.4, 60.6, 78.1), "ph": (4.3, 21.4, 116.6, 126.9),
}

OUTLIER_KM = 600.0
# Geocoders whose accepted result replaces the model's point (services/places/geocoder.CONFIRMED_BY).
CONFIRMED_BY = frozenset({"geoapify"})


def canonical_country(name: Optional[str]) -> Optional[str]:
    """'thailand' / 'SRI-LANDKA' / 'Tibet' -> 'Thailand' / 'Sri Lanka' / 'China'; None if not AA."""
    key = (name or "").strip().lower()
    key = _ALIASES.get(key, key)
    iso = COUNTRY_ISO2.get(key)
    return ISO2_COUNTRY.get(iso) if iso else None


def iso2(name: Optional[str]) -> str:
    c = canonical_country(name)
    return COUNTRY_ISO2.get(c.lower(), "") if c else ""


def country_spellings(code: Optional[str]) -> list[str]:
    """Every lower-case country spelling :func:`iso2` maps to one ISO code, aliases included
    ('cn' -> ['china', 'tibet'], 'lk' -> ['sri lanka', 'sri-landka']); [] when the code is not AA.
    Used to filter raw_tours.country by ISO code (catalog.http.v1, AA-976)."""
    key = (code or "").strip().lower()
    if not key:
        return []
    names = {name for name, iso in COUNTRY_ISO2.items() if iso == key}
    names |= {alias for alias in _ALIASES if iso2(alias) == key}
    return sorted(names)


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
    """None when the point may be stored, else the reason it may not (hard guard)."""
    if lat is None or lng is None:
        return "no_point"
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return "out_of_range"
    if canonical_country(country) is None:
        return "not_aa_country"
    if not in_country(lat, lng, country):
        return "outside_country_bbox"
    return None


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
