"""
Country resolution for S0 (the Excel upload): raw Excel value -> the canonical shared.countries name.

AA-990 (Nghiệp, S252: "nó thuộc file excel nào, tour thô của quốc gia nào thì ghi đúng vậy"): a tour's country is
exactly what its source says, any ISO 3166-1 country (shared/countries, the mirror of shared.countries; raw_tours
.country is a foreign key to shared.countries.name since CIS 288). Never re-labelled from the itinerary: a tour sold
as India that climbs Kilimanjaro stays India; its places get their own country from geocoding.

Order:
  1. the country column resolves to one country (name, ISO name, alias, alpha-2 / alpha-3: 'INDIA', 'Viet Nam',
     'SRI-LANDKA', 'Okinawa', 'Tibet' -> China as shared.countries maps it) -> that country;
  2. the column is EMPTY -> the one country the file name names ('INDIA.xlsx' -> India; longest spelling first;
     a file name naming two countries names none);
  3. the column is filled but names NO country at all ('Himalayas', 'Ladakh', 'Asia') -> the file name's country
     ('Himalayas' in NEPAL.xlsx -> Nepal): the file the row came from is then the source's statement;
  4. the column names one or more countries but not a single one: a compound ('India & Nepal') whose parts include
     the file name's country -> that country; otherwise None (the caller keeps the raw value in
     country_raw_unresolved, AA-571). The file name never overrides an explicit, different column value
     ('TANZANIA' in INDIA.xlsx stays Tanzania; 'India & Nepal' in THAILAND.xlsx is None).
The verbatim Excel row stays in raw_tours.raw_data.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

from shared import countries as _countries

# SEO country words (services/content_generation/seo_meta_utils.py history, services/seo_intelligence/seed_builder.py
# foreign-keyword filter): canonical name -> extra spellings. A word list for those filters only — it restricts
# nothing: S0 resolves every ISO country through shared/countries.
COUNTRY_MASTER = {
    # AA-571 Việc 2B: OKINAWA (a Japanese prefecture, not a country) — a shared.countries alias too.
    "Japan":         ["JAPAN", "JAPANESE", "OKINAWA"],
    "Sri Lanka":     ["SRI LANKA", "SRILANKA", "LKA", "SRI-LANDKA", "SRI LANDKA"],
    "South Korea":   ["SOUTH KOREA", "KOREA", "KOR", "REPUBLIC OF KOREA"],
    "Vietnam":       ["VIETNAM", "VIET NAM", "VNM"],
    "Thailand":      ["THAILAND", "THAI"],
    "Bhutan":        ["BHUTAN", "BTN"],
    "Nepal":         ["NEPAL", "NPL"],
    "India":         ["INDIA", "IND"],
    "Cambodia":      ["CAMBODIA", "KHMER", "KHM"],
    "Laos":          ["LAOS", "LAO", "LAO PDR"],
    "Myanmar":       ["MYANMAR", "BURMA", "MMR"],
    "Indonesia":     ["INDONESIA", "IDN", "BALI"],
    "Malaysia":      ["MALAYSIA", "MYS"],
    "Singapore":     ["SINGAPORE", "SGP"],
    "Philippines":   ["PHILIPPINES", "PHIL", "PHL"],
    "Mongolia":      ["MONGOLIA", "MNG"],
    "China":         ["CHINA", "CHN", "PRC"],
    "Tibet":         ["TIBET"],
    "Taiwan":        ["TAIWAN", "TWN"],
    "Maldives":      ["MALDIVES", "MDV"],
    "Pakistan":      ["PAKISTAN", "PAK"],
}

# Upper-case three-letter codes S0 has always read in a FILE NAME ('IND_tours.xlsx'). Other codes are not: as words
# in a file name they are noise ('Laos AND Vietnam.xlsx' is not Andorra, 'Tours IN Asia' not India).
_FILE_CODES = frozenset({"LKA", "KOR", "VNM", "BTN", "NPL", "IND", "KHM", "LAO", "MMR", "IDN", "MYS", "SGP", "PHL",
                         "MNG", "CHN", "PRC", "TWN", "MDV", "PAK"})


@lru_cache(maxsize=1)
def _file_spellings() -> list[tuple[str, str]]:
    """(UPPER spelling, canonical name), longest first, for the file-name match."""
    out = {}
    for c in _countries.all_countries():
        for s in c.spellings():
            s = " ".join(s.upper().replace("_", " ").replace("-", " ").split())
            if len(s) >= 4 or s in _FILE_CODES:
                out.setdefault(s, c.name)
        if c.iso3 in _FILE_CODES:
            out.setdefault(c.iso3, c.name)
    for code in _FILE_CODES:
        c = _countries.resolve(code)
        if c:
            out.setdefault(code, c.name)
    return sorted(out.items(), key=lambda kv: len(kv[0]), reverse=True)


def country_from_filename(filename: Optional[str]) -> Optional[str]:
    """'INDIA.xlsx' / 'raw-inbox/SRI-LANDKA.xlsx' / 'south_korea_2026.xlsx' -> India / Sri Lanka / South Korea. A
    file name that names two countries ('Laos and Vietnam.xlsx') says no single country: None."""
    if not filename:
        return None
    fname = filename.split("/")[-1].rsplit(".", 1)[0]
    text = f' {" ".join(fname.upper().replace("_", " ").replace("-", " ").split())} '
    found: list[str] = []
    for spelling, name in _file_spellings():            # longest first; a match is blanked out ('SOUTH KOREA')
        pattern = r"(?<![A-Z0-9])" + re.escape(spelling) + r"(?![A-Z0-9])"
        if re.search(pattern, text):
            text = re.sub(pattern, " ", text)
            if name not in found:
                found.append(name)
    return found[0] if len(found) == 1 else None


def resolve_country(raw_value: Optional[str], filename: Optional[str] = None) -> Optional[str]:
    """The tour's country as its source states it (module docstring): the column first, the file name when the column
    is empty or names no country; None when the column names countries that do not include the file's country."""
    raw = raw_value.strip() if isinstance(raw_value, str) else ""
    if not raw:
        return country_from_filename(filename)
    whole = _countries.resolve(raw) or _countries.resolve(raw.replace("_", " "))
    if whole:
        return whole.name
    parts = [c.name for c in _countries.resolve_parts(raw)]
    if not parts:                                        # 'Himalayas', 'Ladakh', 'Asia': the column names no country,
        return country_from_filename(filename)           # so the file the row came from is the source's statement
    file_country = country_from_filename(filename)
    if file_country and file_country in parts:
        return file_country
    return None
