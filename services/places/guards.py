"""AA-712 — quality guards for coordinates (design §4).

Hard (a point failing these is never stored):
  - the country is an ISO 3166-1 country (AA-990: shared.countries through the in-code mirror shared/countries, any
    country — no hand-made "AA country" list), and the point lies inside THAT country's bounding box. The country
    checked is the PLACE's own country (the model's answer, confirmed by the geocoder), never the tour's: a tour sold
    as India may climb Kilimanjaro in Tanzania, a Thailand tour may sleep in Siem Reap (cross-border, normal);
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
