"""AA-997 — which itinerary names are not a place, decided before any paid call (pure functions, no I/O).

The S254 geocode audit (AA-Ecosys docs/audits/2026-10-28-S254-geocode-audit, §7 and fix plan step 4) found that about
half of the 1,621 unresolved names, and ~300 of the resolved ones, are not places: route legs ("Lhasa to Gyantse"),
generic lodging ("hotel in Seoul", "Ganpati Palace (or similar)"), activities and food ("Korean cooking class"),
people ("Tsaatan family"), transport ("Shatabdi Express"), regions and seas ("Arabian Sea", "rural Laos") and
descriptions with no proper name ("small riverside Lao village"). Asking the model and the geocoder about them costs a
call each, three times (the retry loop), and a wrong answer becomes a wrong pin.

`classify(name, locality)` returns a :class:`NameVerdict`: `is_place` False with one `cause` (CAUSES) for a name that
must not be geocoded, else True with `query` — the name with annotations removed ("Yali La Pass (4820 m)" →
"Yali La Pass", "the village of Gogina" → "Gogina").

The rules are deliberately narrow. Measured on the audit's labelled names (tests/unit/test_aa997_name_filter.py,
fixture tests/unit/fixtures/aa997_place_names.json): almost no name verified `correct` is dropped, and the real-place
groups of the unresolved set (obscure, spelling variant, well-known, cross-country) lose only a handful. NOT dropped
here on purpose:
  - compounds ("Mount Kailash and Lake Manasarovar", "Bell and Drum Tower"): many were verified correct; the geocoder
    tries the name and the route guard (guards.route_km) rejects a far-off answer;
  - "kind in/of <Town>" phrases ("the Tibet Museum in Lhasa", "outskirts of Kandy"): they keep their anchor, which the
    geocoder or the town fallback (locate.py) can place;
  - dash pairs ("Pothana–Phedi", "Jiankou–Mutianyu section of the Great Wall"): trail stretches, verified correct;
  - Capitalised kind names ("Great Mosque", "Palace", "the Great Wall"): a geocoder may know them.
A name with a locality (a generic name keyed to a town, "City Palace (Jaipur)") is never a `generic_kind`: the
locality makes it one place.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from services.places import geomatch
from shared import countries as _countries

RULE_VERSION = "aa997.1"

ROUTE_SEGMENT = "route_segment"
LODGING = "lodging"
ACTIVITY = "activity"            # activities, food, shows, people, transport: the audit's "not a place at all"
REGION = "region"                # seas, "rural <Country>", landscapes with no proper name
GENERIC_KIND = "generic_kind"    # a kind with no name ("base camp", "international airport") and no locality
DESCRIPTIVE = "descriptive"      # a description with no proper name ("small riverside Lao village")
ANNOTATION_ONLY = "annotation_only"   # nothing left once the altitude / "(or similar)" is removed ("At about 4040 m")
CAUSES = (ROUTE_SEGMENT, LODGING, ACTIVITY, REGION, GENERIC_KIND, DESCRIPTIVE, ANNOTATION_ONLY)
# geo.place_name.last_error / shared.destinations.review_reason of a name dropped here
NON_PLACE_PREFIX = "non_place"


@dataclass(frozen=True)
class NameVerdict:
    is_place: bool
    cause: Optional[str] = None
    query: str = ""               # the name to geocode (annotations and wrappers removed); "" when not a place

    @property
    def reason(self) -> Optional[str]:
        """`non_place:<cause>`, None for a place."""
        return None if self.is_place else f"{NON_PLACE_PREFIX}:{self.cause}"


# ── cleaning ───────────────────────────────────────────────────────────────────────────────────────────────────────
_OR_SIMILAR = re.compile(r"\(?\s*\bor\s+similar\b\s*\)?", re.I)
_UNIT = r"\d[\d.,]*\s*(?:m|km|ft|metres|meters|masl)\b\.?"
_ALTITUDE = re.compile(r"\(\s*(?:at\s+)?(?:about\s+|approx\.?\s+|c\.\s*)?" + _UNIT + r"\s*\)"
                       r"|\b(?:at\s+)?(?:about\s+|approx\.?\s+)?" + _UNIT, re.I)
# "the (small) village of X" → X (X must hold a proper word)
_WRAPPER = re.compile(r"^(?:the\s+)?(?:(?:small|tiny|remote|little|picturesque|charming|historic|ancient|old|"
                      r"beautiful|sleepy|quiet)\s+)*(?:village|town|hamlet|city|settlement)\s+of\s+(.+)$", re.I)


def clean(name: Optional[str]) -> str:
    """The name without "(or similar)", altitudes and "the village of" wrappers; whitespace collapsed."""
    s = _OR_SIMILAR.sub(" ", name or "")
    s = _ALTITUDE.sub(" ", s)
    s = " ".join(s.split()).strip(" ,;:-–—")
    m = _WRAPPER.match(s)
    if m and _proper_words(m.group(1)):
        s = m.group(1).strip()
    return s


# ── word classes ───────────────────────────────────────────────────────────────────────────────────────────────────
# Capitalised words that do not name a place on their own: peoples, faiths, languages, nationalities.
_ADJECTIVE_PROPER = frozenset({
    "lao", "laotian", "thai", "khmer", "cambodian", "vietnamese", "chinese", "tibetan", "nepali", "nepalese",
    "indian", "hindu", "buddhist", "muslim", "jain", "sikh", "christian", "catholic", "taoist", "daoist", "confucian",
    "shinto", "japanese", "korean", "mongolian", "bhutanese", "lankan", "sinhalese", "tamil", "burmese",
    "filipino", "taiwanese", "malay", "indonesian", "kazakh", "kyrgyz", "uzbek", "uyghur", "russian", "european",
    "french", "british", "colonial", "hmong", "khmu", "khamu", "akha", "lahu", "karen", "yao", "tai", "lu",
    "naxi", "yi", "miao", "bai", "zhuang", "sherpa", "gurung", "tharu", "newari", "rajasthani", "mughal",
    "unesco", "asian", "himalayan", "k", "pop", "my", "our", "your",
})
_STOP = frozenset({"the", "a", "an", "of", "in", "on", "at", "by", "to", "and", "or", "with", "for", "from", "near",
                   "along", "around", "above", "below", "beside", "into", "over", "its", "their", "his", "her"})
_TOKEN = re.compile(r"[A-Za-zÀ-ɏḀ-ỿ][\w'’.\-]*")


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _low(w: str) -> str:
    return _fold(w).lower().strip(".'’-")


def _proper_words(s: str) -> list[str]:
    """Capitalised words that may be (part of) a proper name: not a stop word, not a people / faith adjective, not a
    generic or feature word ("Temple", "Village" alone name nothing)."""
    return [w for w in _TOKEN.findall(s or "") if w[:1].isupper() and _low(w)
            and _low(w) not in _STOP | _ADJECTIVE_PROPER | geomatch.GENERIC_WORDS | geomatch.FEATURE_WORDS]


def _capitalised(q: str) -> list[str]:
    """Capitalised words other than stop words and people / faith adjectives: a Capitalised kind word counts here
    ("Great Mosque", "Palace", "the Dzong" are names a geocoder may know; "small riverside Lao village" has none)."""
    return [w for w in _TOKEN.findall(q) if w[:1].isupper() and _low(w) not in _STOP | _ADJECTIVE_PROPER]


# ── the rules ──────────────────────────────────────────────────────────────────────────────────────────────────────
# "X to Y" legs. Kept as a place: a named stretch of a wall / trail / river ("Jinshanling to Simatai West Great Wall").
_TO = re.compile(r"\S\s+to\s+\S", re.I)
_LEG_WORDS = re.compile(r"\b(?:via|towards?|en route|on the way|heading)\b|->|→", re.I)
_STRETCH_WORDS = frozenset({"wall", "trail", "trek", "road", "river", "canal", "railway", "path", "circuit", "track"})

# generic lodging: the kind, optionally "in / at / near <Town>"; a named hotel stays a place
_LODGING = re.compile(
    r"^(?:the\s+|a\s+|an\s+|our\s+|your\s+)?(?:(?:pre-?booked|group|local|selected|chosen|standard|comfortable|simple|"
    r"basic|boutique|heritage|luxury|traditional|eco|family-?run|riverside|beachside|mountain|tented|central|nearby|"
    r"overnight|deluxe|superior|first-class|budget|charming|cosy|cozy|small|lovely|best)\s+)*"
    r"(?:hotels?|guest\s?houses?|lodges?|resorts?|home-?stays?|hostels?|inns?|ryokans?|eco-?lodges?|accommodations?|"
    r"hotel\s+lobby|hotel\s+room|bungalows?|cabins?|chalets?|farm\s?stays?|camps?ites?|tented\s+camps?)"
    r"(?:\s+(?:in|at|near|of|on|by|inside|outside|within)\b.*)?$", re.I)

# activities / food / shows / people / transport, counted only in lower case or as the last word: a Capitalised one
# inside a name is part of it ("Gao Family Courtyard"); a name holding a place-kind word stays ("Cooking Village
# Museum")
_ACTIVITY = re.compile(
    r"\b(?:class(?:es)?|cooking|cuisine|hot\s?pot|dinner|lunch|breakfast|meals?|banquet|tasting|show|performances?|"
    r"concert|karaoke|noraebang|bar\s+scene|balloon\s+ride|elephant\s+ride|boat\s+ride|rickshaw\s+ride|"
    r"jeep\s+safari|ride|transfers?|flight|express|sleeper\s+train|overnight\s+train|bus|minivan|families|family|"
    r"monks|nuns|nomads|people|peoples|tribes?|villagers|locals|guides?|porters|festival|ceremony|demonstration|"
    r"workshop|lesson|massage|spa\s+treatment|briefing|check-?in|check-?out|orientation)\b", re.I)
_ACTIVITY_KEEP = frozenset({"museum", "temple", "temples", "monastery", "market", "village", "villages", "palace",
                            "fort", "park", "station", "street", "square", "garden", "gardens", "pagoda", "shrine",
                            "church", "hall", "courtyard", "compound", "mansion", "residence", "house", "home"})

# regions and water bodies (never one point). A region word next to a proper name keeps the name ("countryside
# around Yangshuo", "Jeju Island coastline", "Eastern Karakoram" were verified correct as areas); "rural <Country>" is
# dropped.
_WATER = re.compile(r"\b(?:sea|ocean|strait|gulf|himalayas)\b", re.I)
_REGION_WORDS = re.compile(r"\b(?:countryside|hinterland|plains|landscapes?|scenery|farmland|paddies|rice\s+fields|"
                           r"plantations|hill\s+country|highlands|lowlands|ranges)\b", re.I)
# "rural Laos", "remote Bhutan" (not "Central Hong Kong" / "Northern Thailand": a district, or harmless to search)
_DIRECTION = re.compile(r"^(?:the\s+)?(?:very\s+)?(?:rural|remote)\s+(.+)$", re.I)

# a kind with no name
_KIND_ONLY = re.compile(
    r"^(?:the\s+|a\s+|an\s+)?(?:international\s+airport|airport|domestic\s+airport|train\s+station|railway\s+station|"
    r"bus\s+station|bus\s+stop|ferry\s+terminal|pier|jetty|harbou?r|trail\s?head|road\s?head|meeting\s+point|"
    r"starting\s+point|office|departure\s+office|base\s?camp|basecamp|high\s+camp|camp\s+\d+|campsite|"
    r"summit|viewpoint|view\s+point|hot\s+springs?|waterfalls?|rice\s+terraces|tea\s+estate|tea\s+factory|"
    r"local\s+market|night\s+market|old\s+town|old\s+city|old\s+quarter|city\s+centre|city\s+center|town\s+centre|"
    r"town\s+center|downtown|riverbank|river\s?side|lakeside|beach)$", re.I)


def _is_activity(q: str) -> bool:
    for m in _ACTIVITY.finditer(q):
        if m.group(0)[:1].islower() or not q[m.end():].strip(" .,)"):
            return True
    return False


def _is_water(q: str) -> bool:
    """A sea / ocean / strait / gulf / the Himalayas named as such: the word in lower case ("the beach by the sea"),
    last ("Arabian Sea") or before "of" ("Sea of Japan"). A Capitalised one inside a name is part of a place
    ("Ocean Park" in Hong Kong, "Tanah Lot Sea Temple")."""
    for m in _WATER.finditer(q):
        rest = q[m.end():].strip(" .,)")
        if m.group(0)[:1].islower() or not rest or rest.lower().startswith("of "):
            return True
    return False


def _is_region(q: str) -> bool:
    if _is_water(q):
        return True
    m = _DIRECTION.match(q)
    if m and _countries.resolve(m.group(1)) is not None:
        return True                       # "rural Laos"
    if _REGION_WORDS.search(q):
        return not [w for w in _proper_words(q) if _countries.resolve(w) is None]   # "rural Thai landscapes"
    return False


def classify(name: Optional[str], locality: Optional[str] = None) -> NameVerdict:
    """Place or not, before any paid call. `locality` is the town a generic name is keyed to (geo.place_name)."""
    raw = " ".join((name or "").split())
    q = clean(raw)
    if not q:
        return NameVerdict(False, ANNOTATION_ONLY)
    words = {_low(w) for w in _TOKEN.findall(q)}
    if _OR_SIMILAR.search(raw):
        return NameVerdict(False, LODGING)        # "Ganpati Palace (or similar)": a hotel class, not one place
    if (_TO.search(q) and not words & _STRETCH_WORDS) or _LEG_WORDS.search(q):
        return NameVerdict(False, ROUTE_SEGMENT)
    if _LODGING.match(q):
        return NameVerdict(False, LODGING)
    if _is_activity(q) and not words & _ACTIVITY_KEEP:
        return NameVerdict(False, ACTIVITY)
    if _is_region(q):
        return NameVerdict(False, REGION)
    if locality or geomatch.has_native_script(q):
        return NameVerdict(True, None, q)        # a generic name keyed to its town; a native-script name
    capitalised = _capitalised(q)
    if _KIND_ONLY.match(q) or (geomatch.is_generic_phrase(q) and not capitalised):
        return NameVerdict(False, GENERIC_KIND)
    if not capitalised:
        return NameVerdict(False, DESCRIPTIVE)
    return NameVerdict(True, None, q)
