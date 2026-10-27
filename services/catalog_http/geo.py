"""Country codes and IANA time zones for catalog.http.v1 (AA-976, CRM R33: per-day time zone from the tour's places).

The catalog views carry country NAMES as CIS normalises them (``raw_tours.country``, e.g. 'Vietnam', 'Sri Lanka';
contract catalog.tour.v1 says ISO codes are not part of it). catalog.http.v1 needs ISO 3166-1 alpha-2 codes and an
IANA time zone per place, so both are derived here, never stored:

- the country code comes from ``services/places/guards.iso2`` (every ISO 3166-1 country: ``shared/countries``, the
  mirror of shared.countries, AA-990), so a place in Tanzania on a tour sold as India gets TZ;
- the time zone is the country's zone (:data:`_ZONE` for the countries AA tours visit most, tuned by hand); the three
  countries with more than one zone that AA tours cross most split by longitude
  (Indonesia WIB / WITA / WIT, Malaysia peninsula / Borneo, Mongolia west / rest), Indonesia also by latitude
  where a province line is not a meridian (North Sulawesi east of 125 deg E, e.g. Bitung and the Lembeh Strait,
  is WITA like the rest of Sulawesi). That is exact enough for a day plan: each split is a whole-hour step
  between provinces, and the test table pins the dive and ferry stops nearest each line. Any other country: its
  one zone, or for a country with several (Russia, the United States, Brazil…) the zone whose tzdata reference point
  is nearest the place (its first zone in zone.tab when no point is known).

No network and no tz database lookup at request time; ``tests/unit/test_aa976_catalog_http.py`` checks that every
zone named here exists in the system tz database.
"""

from __future__ import annotations

import math
from typing import Optional

from shared import countries as _countries
from services.places.guards import country_spellings, iso2

# One zone per country, except the splits in :func:`time_zone`: the countries AA tours visit most (tuned by hand; China
# is Asia/Shanghai everywhere, not tzdata's Asia/Urumqi). Every other country: its zones in shared/countries.
_ZONE = {
    "BT": "Asia/Thimphu", "KH": "Asia/Phnom_Penh", "CN": "Asia/Shanghai", "IN": "Asia/Kolkata",
    "ID": "Asia/Jakarta", "JP": "Asia/Tokyo", "KG": "Asia/Bishkek", "LA": "Asia/Vientiane",
    "MY": "Asia/Kuala_Lumpur", "MN": "Asia/Ulaanbaatar", "MM": "Asia/Yangon", "NP": "Asia/Kathmandu",
    "KR": "Asia/Seoul", "LK": "Asia/Colombo", "TW": "Asia/Taipei", "TH": "Asia/Bangkok",
    "UZ": "Asia/Tashkent", "VN": "Asia/Ho_Chi_Minh", "PK": "Asia/Karachi", "PH": "Asia/Manila",
    "SG": "Asia/Singapore", "HK": "Asia/Hong_Kong", "MO": "Asia/Macau",
    "BD": "Asia/Dhaka", "MV": "Indian/Maldives", "BN": "Asia/Brunei",
    "TL": "Asia/Dili", "KZ": "Asia/Almaty", "TJ": "Asia/Dushanbe", "TM": "Asia/Ashgabat",
}

# The answer when neither the place nor the tour has a known country (never expected for a live AA tour).
FALLBACK_ZONE = "UTC"

# Longitude splits (degrees east).
_ID_WITA_FROM = 114.42    # Bali (Gilimanuk 114.43), Lombok, Sulawesi, Nusa Tenggara, East / South Kalimantan:
#                           UTC+8; Java's east tip (Banyuwangi / Ketapang 114.37-114.40) stays UTC+7
_ID_WIT_FROM = 125.0      # Maluku, Papua: UTC+9 (Sula, Buru, Wetar lie between 125 and 127 deg E)
_ID_WIT_NORTH_FROM = 127.0  # north of _ID_NORTH_SULAWESI_LAT: Sangihe, Talaud, Bitung / Lembeh stay WITA;
#                             Ternate / Tidore / Halmahera (127.3+) are WIT
_ID_NORTH_SULAWESI_LAT = 1.0  # North Sulawesi east of 125 deg E lies at 1.1 deg N and above
_MY_BORNEO_FROM = 109.0   # Sabah, Sarawak
_MN_HOVD_BEFORE = 96.5    # Bayan-Olgii, Khovd, Uvs: UTC+7


def country_code(name: Optional[str]) -> Optional[str]:
    """'Vietnam' / 'Tibet' / 'philippines' / 'Tanzania' -> 'VN' / 'CN' / 'PH' / 'TZ'; None when unknown."""
    code = iso2(name)
    return code.upper() if code else None


def _indonesia_wit(lng: float, lat: Optional[float]) -> bool:
    if lat is not None and lat >= _ID_NORTH_SULAWESI_LAT:
        return lng >= _ID_WIT_NORTH_FROM
    return lng >= _ID_WIT_FROM


def time_zone(code: Optional[str], lng: Optional[float] = None, lat: Optional[float] = None) -> Optional[str]:
    """IANA zone for a country code, split by longitude (and for Indonesia latitude) where the country has
    several; None when unknown. Without ``lat`` the Indonesian WITA / WIT line is the 125 deg E meridian."""
    if not code:
        return None
    code = code.upper()
    if lng is not None:
        if code == "ID":
            if _indonesia_wit(lng, lat):
                return "Asia/Jayapura"
            if lng >= _ID_WITA_FROM:
                return "Asia/Makassar"
        elif code == "MY" and lng >= _MY_BORNEO_FROM:
            return "Asia/Kuching"
        elif code == "MN" and lng < _MN_HOVD_BEFORE:
            return "Asia/Hovd"
    if code in _ZONE:
        return _ZONE[code]
    return _iso_zone(code, lng, lat)


def _iso_zone(code: str, lng: Optional[float], lat: Optional[float]) -> Optional[str]:
    c = _countries.by_iso2(code)
    if c is None or not c.zones:
        return None
    if len(c.zones) == 1 or lng is None or lat is None:
        return c.zones[0][0]
    scale = math.cos(math.radians(lat))

    def dist(z: tuple) -> float:          # degrees, the longitude gap taken across the antimeridian when shorter
        dlng = (z[2] - lng + 180.0) % 360.0 - 180.0
        return (z[1] - lat) ** 2 + (dlng * scale) ** 2

    return min(c.zones, key=dist)[0]


def spellings(code: str) -> list[str]:
    """Every lower-case country name that maps to one ISO code ('CN' -> ['china', 'tibet']), for filters."""
    code = (code or "").upper()
    return country_spellings(code)


def all_zones() -> set[str]:
    """Every zone this module can answer (tests check each one exists)."""
    return (set(_ZONE.values()) | {"Asia/Jayapura", "Asia/Makassar", "Asia/Kuching", "Asia/Hovd", FALLBACK_ZONE}
            | {z[0] for c in _countries.all_countries() for z in c.zones})
