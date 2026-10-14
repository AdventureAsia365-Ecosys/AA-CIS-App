"""AA-712 — place-name keys shared by Python and SQL.

`name_norm()` is the identity key of a place name inside a country: shared.destinations
(name_norm, country, locality_norm) is UNIQUE since migration 226, and geo.place_name caches
resolution by the same key. Migration 226 backfills existing rows with the SQL twin
`geo.name_norm(text)`; the two MUST give the same string, otherwise a job would not find a row the
migration keyed (tests/integration/test_aa712_migration_226.py compares them on real Postgres).
That is why the accent table below is an explicit list (the same 504 characters the migration
passes to `translate()`), not unicodedata at call time.

Generic names ("Base Camp", "Night Market", "Ban Na" style kind-only names, see
place_cards.is_generic_name) name a different place in every town, so their key carries a locality
(the place card's locality, else the overnight town of that day); without one they are never
resolved (design §4 "Homonyms").
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Optional


def _accent_table() -> tuple[str, str]:
    """(from, to) for translate(): every Latin letter with diacritics in U+00C0–U+024F and
    U+1E00–U+1EFF whose NFKD base is one ASCII letter, plus letters NFKD does not decompose."""
    src, dst = [], []
    for lo, hi in ((0xC0, 0x24F), (0x1E00, 0x1EFF)):
        for cp in range(lo, hi + 1):
            ch = chr(cp)
            base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            if len(base) == 1 and base.isascii() and base.isalpha() and ch != base:
                src.append(ch)
                dst.append(base.lower())
    for k, v in {"đ": "d", "Đ": "d", "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "ı": "i", "ħ": "h",
                 "Ħ": "h", "ŧ": "t", "Ŧ": "t", "ƀ": "b", "ɨ": "i", "ʉ": "u"}.items():
        if k not in src:
            src.append(k)
            dst.append(v)
    return "".join(src), "".join(dst)


ACCENTS_FROM, ACCENTS_TO = _accent_table()
_TRANS = str.maketrans(ACCENTS_FROM, ACCENTS_TO)
# Multi-letter folds translate() cannot do; applied before it, in both Python and SQL.
_MULTI = (("ß", "ss"), ("æ", "ae"), ("Æ", "ae"), ("œ", "oe"), ("Œ", "oe"), ("’", "'"))
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_LEADING_ARTICLES = re.compile(r"^((the|a|an) )+")


def name_norm(name: Optional[str]) -> str:
    """'Hội An' == 'Hoi-An' == 'hoi an'; 'The Old Quarter' == 'old quarter'. '' for no name."""
    s = name or ""
    for a, b in _MULTI:
        s = s.replace(a, b)
    s = s.translate(_TRANS).lower().replace("'", "")
    s = " ".join(_NON_ALNUM.sub(" ", s).split())
    stripped = _LEADING_ARTICLES.sub("", s)
    return stripped if stripped else s


def country_norm(country: Optional[str]) -> str:
    return name_norm(country)


def is_generic(name: Optional[str]) -> bool:
    """Kind-only names ('Base Camp', 'Night Market', 'City Palace'): the place-card rule."""
    from services.acp_shared.place_cards import is_generic_name
    return is_generic_name(name)


def locality_key(name: Optional[str], locality: Optional[str]) -> Optional[str]:
    """'' for a distinctive name; name_norm(locality) for a generic one; None when a generic name
    has no locality (the name stays unresolved rather than borrowing another town's point)."""
    if not is_generic(name):
        return ""
    loc = name_norm(locality)
    return loc or None


# Fixed namespace for component ids (uuid5). Never change it: TripPlanner drafts pin component ids
# (trip_events.payload.component_id, no FK), so a new namespace would drop every pin.
COMPONENT_NS = uuid.UUID("6f1d6a52-0f7c-5a12-9b1e-712a71200000")


def component_id(source_id: str, day_index: int, name: str, country: str, activity: str) -> str:
    """Stable id of a (source, day, place, activity) component. Not keyed by destination_id: a
    name that is pending today and resolved tomorrow must keep its id (design §2.3)."""
    key = "|".join([str(source_id), str(int(day_index)), name_norm(name), country_norm(country),
                    (activity or "").strip().lower()])
    return str(uuid.uuid5(COMPONENT_NS, key))
