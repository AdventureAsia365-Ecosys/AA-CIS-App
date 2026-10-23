"""Country codes and IANA time zones for catalog.http.v1 (AA-976, CRM R33: per-day time zone from the tour's places).

The catalog views carry country NAMES as CIS normalises them (``raw_tours.country``, e.g. 'Vietnam', 'Sri Lanka';
contract catalog.tour.v1 says ISO codes are not part of it). catalog.http.v1 needs ISO 3166-1 alpha-2 codes and an
IANA time zone per place, so both are derived here, never stored:

- the country code comes from ``services/places/guards.iso2`` (the AA country list, the same one the
  ``chk_raw_tours_country`` constraint enforces), plus the few neighbour countries a multi-country tour may cross
  into (:data:`_EXTRA_ISO2`);
- the time zone is the country's zone; the three AA countries with more than one zone split by longitude
  (Indonesia WIB / WITA / WIT, Malaysia peninsula / Borneo, Mongolia west / rest), Indonesia also by latitude
  where a province line is not a meridian (North Sulawesi east of 125 deg E, e.g. Bitung and the Lembeh Strait,
  is WITA like the rest of Sulawesi). That is exact enough for a day plan: each split is a whole-hour step
  between provinces, and the test table pins the dive and ferry stops nearest each line.

No network and no tz database lookup at request time; ``tests/unit/test_aa976_catalog_http.py`` checks that every
zone named here exists in the system tz database.
"""

from __future__ import annotations

from typing import Optional

from services.places.guards import country_spellings, iso2

# Neighbours a multi-country AA tour may stop in that are not AA sales countries (not in guards.COUNTRY_ISO2).
_EXTRA_ISO2 = {
    "philippines": "PH", "singapore": "SG", "hong kong": "HK", "macau": "MO", "bangladesh": "BD",
    "pakistan": "PK", "maldives": "MV", "brunei": "BN", "timor-leste": "TL", "east timor": "TL",
    "kazakhstan": "KZ", "tajikistan": "TJ", "turkmenistan": "TM",
}

# One zone per country, except the splits in :func:`time_zone`.
_ZONE = {
    "BT": "Asia/Thimphu", "KH": "Asia/Phnom_Penh", "CN": "Asia/Shanghai", "IN": "Asia/Kolkata",
    "ID": "Asia/Jakarta", "JP": "Asia/Tokyo", "KG": "Asia/Bishkek", "LA": "Asia/Vientiane",
    "MY": "Asia/Kuala_Lumpur", "MN": "Asia/Ulaanbaatar", "MM": "Asia/Yangon", "NP": "Asia/Kathmandu",
    "KR": "Asia/Seoul", "LK": "Asia/Colombo", "TW": "Asia/Taipei", "TH": "Asia/Bangkok",
    "UZ": "Asia/Tashkent", "VN": "Asia/Ho_Chi_Minh",
    "PH": "Asia/Manila", "SG": "Asia/Singapore", "HK": "Asia/Hong_Kong", "MO": "Asia/Macau",
    "BD": "Asia/Dhaka", "PK": "Asia/Karachi", "MV": "Indian/Maldives", "BN": "Asia/Brunei",
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
    """'Vietnam' / 'Tibet' / 'philippines' -> 'VN' / 'CN' / 'PH'; None when unknown."""
    code = iso2(name)
    if code:
        return code.upper()
    return _EXTRA_ISO2.get((name or "").strip().lower())


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
    return _ZONE.get(code)


def spellings(code: str) -> list[str]:
    """Every lower-case country name that maps to one ISO code ('CN' -> ['china', 'tibet']), for filters."""
    code = (code or "").upper()
    names = set(country_spellings(code))
    names |= {name for name, iso in _EXTRA_ISO2.items() if iso == code}
    return sorted(names)


def all_zones() -> set[str]:
    """Every zone this module can answer (tests check each one exists)."""
    return set(_ZONE.values()) | {"Asia/Jayapura", "Asia/Makassar", "Asia/Kuching", "Asia/Hovd", FALLBACK_ZONE}
