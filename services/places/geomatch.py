"""AA-712 — Geoapify query normalisation and the acceptance rule (pure functions, no I/O).

Source: the S236 bake-off (AA-Ecosys docs/audits/2026-10-14-S236-design-review/aa712/05-geocoder-bakeoff.md,
"Acceptance rules"). On 55 real names with a Geoapify result, "name similarity ≥ 0.5 AND ≤ 50 km of the
reference point" kept 34 of the 38 correct results and let 3 wrong ones through; `rank.confidence` missed 9
correct POIs (Jokhang, Lake Khuvsgul, Li Phi Falls, Yol score 0) and is therefore recorded, never used as a gate.

The rule: scan the top 3 candidates, accept the first one that passes all of
  (a) name: similarity = 1.0, or ≥ 0.5 when the query has ≥ 2 distinctive tokens;
  (b) type: result_type not country / state / county / postcode, and `city` only when the query itself is a
      settlement (no POI feature word such as temple / lake / pass / cave);
  (c) distance to the model's point ≤ 25 km, or ≤ 50 km for long features (lake / river / valley / park /
      trail / road).

`sim` follows the bake-off script exactly (tests/unit/test_aa712_geoapify.py reproduces its numbers on the
recorded responses): lower-case, diacritics and parentheses removed, generic words (SIM_STOP) dropped; the share
of the remaining query tokens found in the candidate's name (exact, or difflib ratio ≥ 0.85).
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Optional

# Generic words dropped before the name similarity — the bake-off's STOP set, unchanged (changing it changes
# the measured numbers; extend GENERIC_WORDS instead).
SIM_STOP = frozenset({
    "the", "and", "of", "temple", "monastery", "village", "lake", "pass", "la", "falls", "waterfall",
    "waterfalls", "river", "valley", "park", "trail", "historic", "island", "camp", "base", "to", "or",
    "site", "city", "new", "hindu", "folk", "statue", "crater", "mountains", "near", "complexes", "cave",
    "caves", "street", "great", "wall", "villa", "imperial", "historical", "hotel", "local"})
# Words that, alone, never name one place ("ger camp", "local market", "village center", "fishing village"):
# a query with nothing but these (and SIM_STOP) is not geocoded — the model's point for "<name> (<town>)" is
# the day's town point.
GENERIC_WORDS = SIM_STOP | frozenset({
    "market", "markets", "center", "centre", "ger", "fishing", "villages", "reef", "reefs", "living", "glamping",
    "northern", "southern", "eastern", "western", "central", "old", "town", "quarter", "night", "floating",
    "traditional", "homestay", "lodge", "guesthouse", "resort", "beach", "harbour", "harbor", "port", "farm",
    "school", "house", "home", "family", "nomad", "nomadic", "tent", "tents", "area", "region", "countryside",
    "rural", "nearby", "surrounding", "various", "small", "big", "local", "a", "an", "in", "at", "on", "with"})
# Feature words: a query with one of these names a POI, so a `city` result is only its town (an admin fallback).
FEATURE_WORDS = frozenset({
    "temple", "temples", "monastery", "gompa", "dzong", "pagoda", "stupa", "shrine", "church", "mosque",
    "cathedral", "lake", "tso", "pass", "falls", "waterfall", "waterfalls", "river", "valley", "park", "trail",
    "cave", "caves", "statue", "crater", "volcano", "mountain", "mountains", "peak", "glacier", "gorge", "canyon",
    "wall", "fort", "fortress", "palace", "museum", "garden", "gardens", "bridge", "dam", "reservoir", "beach",
    "bay", "reef", "reefs", "estuary", "hot", "spring", "springs", "market", "street", "road", "highway", "hotel",
    "lodge", "resort", "complexes", "ruins", "tomb", "mausoleum", "gate", "tower", "dairy", "farm",
    "mount", "mt", "hill", "hills", "island", "islands", "rock", "rocks", "viewpoint"})
# Long features: the stored point and the model's point may be tens of km apart and both right.
LONG_FEATURE_WORDS = frozenset({"lake", "river", "valley", "park", "trail", "road", "highway", "reservoir",
                                "tso", "national"})
# "Hard" place kinds (bake-off sample rule, s236/bake_sample.py): when no geocoder confirms them the model's
# point is not trusted as a pin — listed for review on /admin/places.
_HARD_RE = re.compile(r"(monaster|gompa|dzong|\bpass\b|\bla$|\bla\b|lake|\btso\b|village|camp|temple|valley|"
                      r"glacier|cave|waterfall|falls|peak|base)", re.I)
REJECT_TYPES = frozenset({"country", "state", "county", "postcode"})

# AA-847 — places that are not a pin. A LINEAR feature (a trail, river, road, loop) has no point a map can mark: a
# pin or a circle at its centroid misleads (the 1,000 km Ho Chi Minh Trail drawn as one pin, S237). Owner decision
# S246: a linear feature is stored geo_precision 'line' and DRAWN as a line (geo.place_shape, migration 255) — never a
# pin, never a circle, never hidden. An AREA feature (national park, delta, plateau, coast, gorge, mountain range) is
# drawn as an area. A name that also names a point on the feature ("River Kwai Bridge", "Kuang Si Falls", "Ho Chi Minh
# Trail Museum", "Peradeniya Railway Station") stays a point.
# S246 review: + stream / creek / waterway / expressway / path / circuit ("Cheonggyecheon Stream", "Yamuna Expressway",
# "Annapurna Circuit", "Druk Path"); `range` moved to the area kind (a range is not a line a traveller follows); not
# `kora` ("Chorten Kora" is a temple) and not `cruise` (an activity, see linked_feature).
LINEAR_FEATURE_WORDS = frozenset({"trail", "river", "road", "highway", "loop", "railway", "railroad", "canal",
                                  "route", "stream", "creek", "waterway", "expressway", "path", "circuit"})
AREA_FEATURE_WORDS = frozenset({"delta", "plateau", "coast", "coastline", "gorge", "archipelago", "highlands",
                                "range"})
AREA_FEATURE_PHRASES = ("national park", "nature reserve", "protected area", "wildlife sanctuary", "marine park",
                        "biosphere reserve")
POINT_ON_FEATURE_WORDS = frozenset({
    "bridge", "falls", "waterfall", "waterfalls", "cave", "caves", "temple", "temples", "pagoda", "stupa", "museum",
    "market", "station", "pier", "jetty", "port", "harbour", "harbor", "fort", "gate", "tower", "village", "town",
    "city", "viewpoint", "resort", "lodge", "hotel", "camp", "homestay", "guesthouse", "headquarters", "hq",
    "visitor", "centre", "center", "monument", "memorial", "cemetery", "tunnel", "tunnels", "dam", "beach",
    "island", "spring", "springs", "school", "restaurant", "cafe", "rapids"})
# Geoapify result_type of a road; with a street word in the name it is linear too ("Sisavangvong Road" already is).
LINEAR_RESULT_TYPES = frozenset({"street"})
STREET_WORDS = frozenset({"street", "avenue", "boulevard", "lane"})
LINEAR = "linear"
AREA = "area"
# How a line is drawn (TripPlanner style, AA-847 6d): water (blue), trail (ink dashed), road (ink solid). The
# place_shape job prefers the OSM tags of the accepted feature (waterway / highway / railway / route=hiking).
LINE_CLASS_WORDS = {
    "water": frozenset({"river", "stream", "creek", "canal", "waterway"}),
    "road": frozenset({"road", "highway", "expressway", "railway", "railroad", "street", "avenue", "boulevard"}),
    "trail": frozenset({"trail", "path", "circuit", "loop", "route", "lane"}),
}

# AA-847 (dedupe) — the KIND of place an English word names, the one class map behind the duplicate-place decision
# (services/places/dedupe.py) and the validity report. Words of one class are synonyms ("falls" = "waterfall",
# "pagoda" = "temple"); a kind only ever keeps two names apart or sends the pair to the model, so a missing synonym
# costs a missed merge, never a wrong one. Every word of FEATURE_WORDS / LINEAR_FEATURE_WORDS / AREA_FEATURE_WORDS /
# POINT_ON_FEATURE_WORDS that names a kind has a class here (tests/unit/test_aa847_dedupe.py checks it, so the
# lexicons cannot drift); the local words (wat, tat, tham, ho, cho …) are dedupe's own table.
_KIND_CLASSES = {
    "waterfall": ("falls", "fall", "waterfall", "waterfalls", "cascade", "cascades"),
    "cave": ("cave", "caves", "grotto"),
    "mountain": ("mount", "mt", "mountain", "mountains", "hill", "hills", "peak", "volcano", "crater"),
    "glacier": ("glacier",),
    "rock": ("rock", "rocks"),
    "lake": ("lake", "pond", "lagoon", "reservoir", "tso"),
    "river": ("river", "stream", "creek", "waterway"),
    "island": ("island", "islands", "isle"),
    "village": ("village",),
    "valley": ("valley",),
    "pass": ("pass",),
    "beach": ("beach",),
    "bay": ("bay",),
    "reef": ("reef", "reefs"),
    "estuary": ("estuary",),
    "spring": ("spring", "springs"),
    "park": ("park",),
    "gorge": ("gorge", "canyon"),
    "delta": ("delta",),
    "plateau": ("plateau", "highlands"),
    "coast": ("coast", "coastline"),
    "archipelago": ("archipelago",),
    "market": ("market", "markets", "bazaar"),
    "bridge": ("bridge",),
    "museum": ("museum",),
    "palace": ("palace",),
    "garden": ("garden", "gardens"),
    "square": ("square",),
    "tunnel": ("tunnel", "tunnels"),
    "dam": ("dam",),
    "tower": ("tower",),
    "ruins": ("ruins",),
    "rapids": ("rapids",),
    "port": ("pier", "jetty", "port", "harbour", "harbor"),
    "station": ("station",),
    "temple": ("temple", "temples", "pagoda", "monastery", "gompa", "shrine", "sanctuary"),
    "church": ("church", "mosque", "cathedral"),
    "stupa": ("stupa", "chedi", "zedi"),
    "fort": ("wall", "gate", "fort", "fortress", "citadel", "dzong"),
    "monument": ("statue", "monument", "memorial", "mausoleum", "tomb", "cemetery"),
    "viewpoint": ("viewpoint", "lookout"),
    "lodging": ("hotel", "lodge", "resort", "homestay", "guesthouse", "camp"),
    "farm": ("farm", "dairy"),
    "facility": ("center", "centre", "headquarters", "hq", "visitor", "school", "restaurant", "cafe"),
    "town": ("town",),
    # linear kinds (their geometry class is LINEAR; the class is the subtype a merge must share)
    "road": ("road", "highway", "street", "avenue", "boulevard", "lane", "expressway", "route", "loop"),
    "trail": ("trail", "path", "trek", "circuit"),
    "rail": ("railway", "railroad"),
    "canal": ("canal",),
    # an AREA kind since the lines lane (S246 review): a range is not a line a traveller follows
    "range": ("range",),
}
KIND_OF_WORD: dict[str, str] = {w: k for k, ws in _KIND_CLASSES.items() for w in ws}
# Two-word kinds, read before single words (both words consumed): a night market is not the day market next to it, an
# old town is not the town, a national park is not a park.
PHRASE_KINDS: dict[tuple[str, str], str] = {
    ("night", "market"): "night_market", ("night", "markets"): "night_market", ("night", "bazaar"): "night_market",
    ("floating", "market"): "floating_market",
    ("old", "town"): "old_town", ("old", "quarter"): "old_town", ("ancient", "town"): "old_town",
    ("old", "city"): "old_town",
    ("national", "park"): "national_park", ("marine", "park"): "national_park",
    ("nature", "reserve"): "reserve", ("wildlife", "sanctuary"): "reserve", ("protected", "area"): "reserve",
    ("biosphere", "reserve"): "reserve",
    ("imperial", "city"): "fort", ("forbidden", "city"): "fort", ("city", "palace"): "palace",
    ("hot", "spring"): "spring", ("hot", "springs"): "spring",
}
# Subtype of a linear place by kind class: a merge of two linear places needs the same subtype (road ≠ trail ≠ river).
LINEAR_SUBTYPES = frozenset({"river", "road", "trail", "rail", "canal"})


def feature_kind(name: Optional[str], result_type: Optional[str] = None) -> Optional[str]:
    """'linear' | 'area' | None (a point). Name words first; the geocoder's result_type only confirms a street."""
    words = _words(name or "")
    if words & POINT_ON_FEATURE_WORDS:
        return None
    joined = " ".join(_TOKENS.findall(_strip(_PARENS.sub(" ", (name or "").lower()))))
    if words & LINEAR_FEATURE_WORDS:
        return LINEAR
    if (result_type or "") in LINEAR_RESULT_TYPES and words & STREET_WORDS:
        return LINEAR
    if words & AREA_FEATURE_WORDS or any(p in joined for p in AREA_FEATURE_PHRASES):
        return AREA
    return None


def line_class(name: Optional[str]) -> str:
    """'water' | 'road' | 'trail' from the name's linear word (the last one wins: "Ho Chi Minh Trail Road" is a road);
    'trail' when none (a conceptual route)."""
    out = "trail"
    for w in _TOKENS.findall(_strip(_PARENS.sub(" ", (name or "").lower()))):
        for cls, ws in LINE_CLASS_WORDS.items():
            if w in ws:
                out = cls
    return out


# ── descriptive phrase → the feature it names (S246) ──────────────────────────────────────────────────────────────
# An atom place is often a phrase around a linear feature: "sandbank on the Nam Ou River", "Gesashi River mangrove
# kayaking", "Li River near Yangshuo", "Annapurna Circuit Trek", "Kanchanaburi River Kwai". The component then links
# to THAT feature's destination (one destination per river, drawn once) instead of a destination named after the
# phrase. Deterministic, measured on a labelled set (tests/unit/test_aa847_linked_feature.py: link / keep generic /
# keep point / already the feature; false links counted first, lesson AA-779): a phrase whose kind is a point on the
# feature ("bridge over the Nam Ou River", "Death Railway Museum") or an area along it ("Betwa River valley") is never
# linked, and only Capitalised words count as the feature's own name (so "the main road south" names nothing).
_LINEAR_ALT = "|".join(sorted(LINEAR_FEATURE_WORDS, key=len, reverse=True))
_CAP = r"[A-Z0-9][\w'’.\-]*"
# "<Proper words> River" (the proper words: a run of Capitalised words right before the linear word)
_FEATURE_AFTER = re.compile(rf"((?:{_CAP}\s+){{1,6}})((?i:{_LINEAR_ALT}))\b")
# "River <Proper>" ("River Kwai")
_FEATURE_BEFORE = re.compile(rf"\b(River)\s+({_CAP}(?:\s+{_CAP}){{0,2}})")
# Capitalised words that start a phrase but are not part of a feature's name (activities, verbs, adjectives, days).
_LEAD_NOISE = frozenset({
    "day", "days", "cruise", "cruising", "crossing", "cross", "ride", "riding", "cycle", "cycling", "bike", "biking",
    "drive", "driving", "hike", "hiking", "trek", "trekking", "walk", "walking", "explore", "exploring", "visit",
    "visiting", "kayak", "kayaking", "boat", "boating", "rafting", "raft", "paddle", "paddling", "along", "on", "the",
    "a", "an", "to", "from", "via", "by", "of", "up", "down", "optional", "free", "scenic", "mighty", "famous", "famed",
    "beautiful", "jungle", "mangrove", "sunset", "sunrise", "morning", "evening", "full", "half", "private", "guided",
    "tour", "trip", "journey", "excursion", "and", "or", "with", "in", "at", "near", "entire", "section", "stage",
    "transfer", "return", "back", "continue", "begin", "start", "end", "finish", "take", "enjoy", "discover"})
# words after the feature that make the phrase name an area along it, not the line
_AREA_AFTER = AREA_FEATURE_WORDS | frozenset({"valley", "basin", "plain", "plains", "watershed", "estuary"})


def _feature_words(run: str) -> list[str]:
    """The trailing run of name words of a Capitalised run: noise words and numbers cut it ("Kilimanjaro Trekking Via
    Machame" → ["Machame"], "10km Pingxi Historic" → ["Pingxi", "Historic"]); a possessive is kept ("Patong's")."""
    words = run.split()
    out: list[str] = []
    for w in reversed(words):
        lw = w.lower().strip(".'’")
        if lw in _LEAD_NOISE or lw.endswith("ing") or w[:1].isdigit():
            break
        out.append(w)
    return list(reversed(out))


def linked_feature(name: Optional[str]) -> Optional[str]:
    """The linear feature a descriptive phrase is about ("sandbank on the Nam Ou River" → "Nam Ou River"), or None
    when the phrase is already the feature, names no Capitalised feature, or names a point / area on it."""
    whole = " ".join(_PARENS.sub(" ", _ALTITUDE.sub(" ", name or "")).split(",")[0].split())
    if not whole or feature_kind(whole) != LINEAR:
        return None
    # "Luang Prabang – Chomphet Loop to Kiao Niao Farmstead": the dash-separated part that names the line
    parts = [p.strip() for p in _DASH_SPLIT.split(whole) if p.strip()]
    s = next((p for p in parts if feature_kind(p) == LINEAR), whole)
    feature, start = None, 0
    before = _FEATURE_BEFORE.search(s)
    if before:
        keep = []
        for w in before.group(2).split():
            lw = w.lower()
            if lw in _LEAD_NOISE or lw.endswith("ing") or lw in _AREA_AFTER or lw in POINT_ON_FEATURE_WORDS \
                    or lw in LINEAR_FEATURE_WORDS:
                break
            keep.append(w)
        if keep:
            feature, start = "River " + " ".join(keep), before.start()
    if feature is None:
        for m in _FEATURE_AFTER.finditer(s + " "):
            words = _feature_words(m.group(1))
            if not words:
                continue
            if {w.lower() for w in words} & (_AREA_AFTER | POINT_ON_FEATURE_WORDS):
                return None     # "Mekong Delta river tour": the delta, not a line
            if _words(s[m.end():]) & _AREA_AFTER:
                return None     # "Betwa River valley"
            word = m.group(2)
            feature = " ".join(words + [word[0].upper() + word[1:].lower()])
            start = max(0, s.rfind(words[0], 0, m.end()))
            break
    if feature is None or is_generic_phrase(feature):
        return None
    if _proper_head(s[:start]):
        return None     # "Wat Phou along the Mekong River", "Luang Prabang on the Mekong River": a place by the line
    if " ".join(_TOKENS.findall(_strip(feature.lower()))) == " ".join(_TOKENS.findall(_strip(whole.lower()))):
        return None     # already the feature itself
    return feature


_DASH_SPLIT = re.compile(r"\s+[—–-]\s+|\s*[—–]\s*")
_HEAD_PREP = re.compile(r"\b(?:on|along|by|beside|near|over|across|of|at|in|overlooking)\s+(?:the\s+)?$", re.I)


def _proper_head(head: str) -> bool:
    """The text before "<prep> (the) <feature>" names a place of its own: a Capitalised word that is not noise."""
    m = _HEAD_PREP.search(head)
    if not m:
        return False
    for w in head[:m.start()].split():
        lw = w.lower().strip(".'’:")
        if w[:1].isupper() and lw not in _LEAD_NOISE and not lw.endswith("ing"):
            return True
    return False


ACCEPT_KM = 25.0
# AA-997 v2: the acceptance rules. geocoder_meta.rule of every accepted answer records which one judged it
# ("v1" = the S236 bake-off rule + the S255 route rule; "aa997.2" = v1 + the strict_reason checks). Strict is OFF by
# default (accept(strict=False)); it is switched on per job (place_relocate payload `accept_rule`) or for new names
# (env GEOCODER_ACCEPT_RULE, geocoder.default_accept_rule) after the relocate re-audit shows it refuses no more
# correct pins than it saves. Answers stored before AA-997 v2 carry no `rule`.
# "aa997.3" (S258 P4 tune) = the same checks with route evidence limited to routes that have the place's own day stop
# and the homonym distance for the re-judge's off-route check (StrictParams / STRICT_PARAMS below): aa997.2 refused
# 34 / 251 verified-correct Geoapify pins in the S257 re-audit (P4), mostly on routes that miss the day's own stop.
# "aa997.4" (S261) = aa997.3 + the day-evidence guard (`day_disagree`, day_disagree below): aa997.3 moved model points
# the S254 audit labelled correct to a same-name feature in another town of the same itinerary (142 km, 118 km) or to
# another same-name feature 4-5 km away, because an exact-name full match overrules an on-route model point.
ACCEPT_RULE_V1 = "v1"
ACCEPT_RULE = "aa997.2"
ACCEPT_RULE_V3 = "aa997.3"
ACCEPT_RULE_V4 = "aa997.4"
STRICT_RULES = (ACCEPT_RULE, ACCEPT_RULE_V3, ACCEPT_RULE_V4)
ACCEPT_RULES = (ACCEPT_RULE_V1, ACCEPT_RULE, ACCEPT_RULE_V3, ACCEPT_RULE_V4)
LONG_FEATURE_KM = 50.0
TOP_N = 3

_PARENS = re.compile(r"\([^)]*\)")
_ALTITUDE = re.compile(r"\(?\s*\d[\d.,]*\s*(?:m|km|ft|metres|meters|masl)\b\.?\s*\)?", re.I)
_TO_SPLIT = re.compile(r"\s+to\s+", re.I)
_TOKENS = re.compile(r"[a-z]+")
# Native-script feature suffixes (Chinese / Korean): a POI, so a `city` result is only its town.
_NATIVE_FEATURE = re.compile(r"(山莊|山庄|寺|廟|庙|宮|宫|湖|潭|峽|峡|谷|瀑布|公園|公园|老街|步道|神社|"
                             r"사$|호수|폭포|공원|해변|산$|섬$|궁$)")
_NATIVE = re.compile(r"[぀-ヿ㐀-鿿가-힯ᄀ-ᇿ]")


def _strip(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return s.replace("đ", "d").replace("Đ", "D")


def tokens(query: str) -> list[str]:
    """Distinctive tokens of a query (the bake-off `toks`)."""
    s = _PARENS.sub(" ", _strip((query or "").lower()))
    return [t for t in _TOKENS.findall(s) if t not in SIM_STOP and len(t) > 1]


def name_sim(query: str, candidate: Optional[str]) -> float:
    qt = tokens(query)
    ct = set(_TOKENS.findall(_strip((candidate or "").lower())))
    if not qt:
        return 0.0
    hit = sum(1 for t in qt if t in ct or any(difflib.SequenceMatcher(None, t, c).ratio() >= 0.85 for c in ct))
    return hit / len(qt)


def _words(query: str) -> set[str]:
    return set(_TOKENS.findall(_strip(_PARENS.sub(" ", (query or "").lower()))))


def is_generic_phrase(name: str) -> bool:
    """No proper noun: every word is generic ('ger camp', 'local market', 'village center')."""
    words = [w for w in _TOKENS.findall(_strip(_PARENS.sub(" ", (name or "").lower()))) if len(w) > 1]
    return not words or all(w in GENERIC_WORDS for w in words)


def is_settlement(query: str) -> bool:
    """A town / village query (a `city` result may be the place itself), i.e. no POI feature word."""
    return not (_words(query) & FEATURE_WORDS)


def radius_km(query: str) -> float:
    return LONG_FEATURE_KM if _words(query) & LONG_FEATURE_WORDS else ACCEPT_KM


def is_hard(name: str) -> bool:
    return bool(_HARD_RE.search(name or ""))


def has_native_script(name: Optional[str]) -> bool:
    return bool(name and _NATIVE.search(name))


def native_feature(query: Optional[str]) -> bool:
    return bool(query and _NATIVE_FEATURE.search(query))


def _no_proper_noun(part: str) -> bool:
    """An "X to Y" endpoint made only of generic / feature words ("Road" in "Road to Mandalay"): alone it
    would need just a 1.0 name match, so any "… Road" within 50 km would pass."""
    words = [w for w in _TOKENS.findall(_strip(part.lower())) if len(w) > 1]
    return not words or all(w in GENERIC_WORDS or w in FEATURE_WORDS for w in words)


def normalise(name: str) -> list[str]:
    """Query texts to send, in order (at most 2): altitudes and parentheticals dropped, "X, City" → "X",
    "X to Y" → ["X", "Y"] minus any endpoint with no proper noun. [] for an empty / generic name."""
    s = _ALTITUDE.sub(" ", name or "")
    s = _PARENS.sub(" ", s)
    s = s.split(",")[0]
    parts = [" ".join(p.split()) for p in _TO_SPLIT.split(s)] if _TO_SPLIT.search(s) else [" ".join(s.split())]
    out = [p.strip(" -–—.;:") for p in parts]
    out = [p for p in out if p and not is_generic_phrase(p)]
    if len(parts) > 1:
        out = [p for p in out if not _no_proper_noun(p)]
    return list(dict.fromkeys(out))[:2]


@dataclass(frozen=True)
class Candidate:
    lat: float
    lng: float
    iso2: str
    name: str
    result_type: str
    confidence: Optional[float]
    match_type: Optional[str]
    place_id: Optional[str]
    source: Optional[str]


def candidates(payload: dict) -> list[Candidate]:
    """Geoapify `format=json` payload → candidates in the provider's order."""
    out = []
    for r in (payload or {}).get("results") or []:
        try:
            lat, lng = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        rank = r.get("rank") or {}
        out.append(Candidate(lat, lng, (r.get("country_code") or "").lower(),
                             r.get("name") or r.get("address_line1") or "", r.get("result_type") or "",
                             rank.get("confidence"), rank.get("match_type"), r.get("place_id"),
                             (r.get("datasource") or {}).get("sourcename")))
    return out


@dataclass
class Verdict:
    accepted: Optional[Candidate]
    rank: Optional[int] = None          # 0-based index of the accepted candidate
    sim: Optional[float] = None
    km: Optional[float] = None
    reasons: list[str] = field(default_factory=list)   # why each scanned candidate was rejected


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    from services.places.guards import km
    return km(lat1, lng1, lat2, lng2)


DAY_TRIP = "day_trip"   # reference_km: a second-choice pass (see there)


def reference_km(c_lat: float, c_lng: float, lat: Optional[float], lng: Optional[float], query: str,
                 routes=()) -> tuple[Optional[float], Optional[str]]:
    """(distance, reason) of a candidate against the reference (AA-997). With a tour route (guards.route_km: the
    neighbouring days' stops of the tours using the name) the route decides — the model's point is only a hint and a
    candidate near a wrong hint but far off the route is rejected (`off_route`, the Beijing "City Wall" on a Xi'an
    tour); a right candidate far from a wrong hint passes (Kong Lor 70 km from the model's point). Without a route the
    model's point is the reference (the S236 bake-off rule, ACCEPT_KM / LONG_FEATURE_KM). With neither: `no_reference`
    (nothing to check a geocoder answer against, so nothing is accepted).
    `day_trip` (a second choice, accept / _accept_native take it only when no candidate passes outright): off the
    route's radius but within guards.DAY_TRIP_KM of it AND within the bake-off radius of the model's point — a day trip
    from the base (Ayutthaya ~75 km from Bangkok, Mutianyu ~70 km from Beijing) where the geocoder and the model agree;
    a homonym in another city (~900 km) stays `off_route`."""
    from services.places import guards
    d = guards.route_km(c_lat, c_lng, routes)
    if d is not None:
        if d <= guards.route_radius(query):
            return d, None
        if (d <= guards.DAY_TRIP_KM and lat is not None and lng is not None
                and _km(lat, lng, c_lat, c_lng) <= radius_km(query)):
            return d, DAY_TRIP
        return d, "off_route"
    if lat is None or lng is None:
        return None, "no_reference"
    d = _km(lat, lng, c_lat, c_lng)
    return d, (None if d <= radius_km(query) else "distance")


def accept(query: str, cands: list[Candidate], lat: Optional[float], lng: Optional[float], iso2: str = "",
           top_n: int = TOP_N, *, routes=(), iso_codes: Optional[tuple] = None,
           settlement: Optional[bool] = None, strict: bool = False, compact: bool = False) -> Verdict:
    """The first of the top `top_n` candidates passing (a) name, (b) type, (c) distance (reference_km); country must
    match `iso2` (or one of `iso_codes`, AA-997: China covers hk / mo) when given (the request filters by country
    already — this is the hard guard). `settlement` forces the `city` rule (the town fallback asks for a town).
    AA-997 v2: `strict` (default OFF, rule ACCEPT_RULE; it only ever refuses more) adds strict_reason's checks — a check
    that needs the model's point is skipped when there is none (not judgeable is not refused); `compact` (strategy v2)
    measures the name with name_sim_v2 (transliteration joins, "Mt" / "in" ignored). `query` is the bare name: a
    context query ("X, Town") is searched with the context but judged on X (geocoder.confirm). `strict` is True (=
    ACCEPT_RULE) or a rule name of STRICT_RULES (strict_rule)."""
    rule = strict_rule(strict)
    params = strict_params(rule)
    sim_fn = name_sim_v2 if compact else name_sim
    n_tok = len(tokens_v2(query) if compact else tokens(query))
    forced_town = bool(settlement)          # the town fallback asks for a town: no site rule then
    settlement = is_settlement(query) if settlement is None else settlement
    codes = set(iso_codes or ()) | ({iso2} if iso2 else set())
    reasons = []
    fallback: Optional[Verdict] = None      # the first `day_trip` candidate: taken only when none passes outright
    for i, c in enumerate(cands[:top_n]):
        sim = sim_fn(query, c.name)
        if codes and c.iso2 and c.iso2 not in codes:
            reasons.append(f"{i}:country")
        elif not (sim >= 0.999 or (sim >= 0.5 and n_tok >= 2)):
            reasons.append(f"{i}:name")
        elif c.result_type in REJECT_TYPES or (c.result_type == "city" and not settlement):
            reasons.append(f"{i}:type")
        else:
            dist, why = reference_km(c.lat, c.lng, lat, lng, query, routes)
            if rule and why in (None, DAY_TRIP):
                why = strict_reason(query, c, sim, lat, lng, routes, settlement=settlement,
                                    forced_town=forced_town, day_trip=why == DAY_TRIP, params=params) or why
            if why == DAY_TRIP and fallback is None:
                fallback = Verdict(c, i, round(sim, 3), round(dist, 2))
            if why:
                reasons.append(f"{i}:{why}")
            else:
                return Verdict(c, i, round(sim, 3), round(dist, 2), reasons)
    return with_fallback(fallback, reasons)


# ── AA-997 v2: stricter acceptance (S257 interim audit: relocate `confirmed` only 18/29 right) ─────────────────────
# Site words for the admin-type rule only (FEATURE_WORDS is shared with dedupe / is_settlement and stays as measured):
# a query naming a site (temple, airport, museum, peak …) is never answered by a town / suburb / district. Long and
# area features (lake, valley, park, island, bay …) are left out: an admin area may carry their name and their point.
_SITE_EXTRA = frozenset({
    "airport", "terminal", "station", "museum", "gallery", "monument", "memorial", "mausoleum", "tomb", "cemetery",
    "stadium", "zoo", "aquarium", "theatre", "theater", "observatory", "lighthouse", "pier", "jetty", "square",
    "plaza", "masjid", "mandir", "wat", "lhakhang", "goemba", "chorten", "castle", "haveli", "mahal", "ghat",
    "bazaar", "hut", "gurudwara", "dargah", "kovil", "minaret", "grotto", "grottoes", "pavilion"})
SITE_WORDS = (FEATURE_WORDS | _SITE_EXTRA) - LONG_FEATURE_WORDS - frozenset({
    "island", "islands", "bay", "beach", "reef", "reefs", "estuary", "hill", "hills", "street", "road", "highway",
    "hot", "spring", "springs", "farm", "dairy", "hotel", "lodge", "resort", "rock", "rocks", "mountains"})
ADMIN_TYPES = frozenset({"city", "suburb", "district"})
# Geoapify rank.match_type values that mean "the place was not found, a containing area / street was returned"
# (Geoapify spells the city one "disrict"; both spellings are listed).
FALLBACK_MATCH_TYPES = frozenset({"match_by_city_or_disrict", "match_by_city_or_district", "match_by_country_or_state",
                                  "match_by_postcode"})
_ROAD_WORDS = frozenset({"street", "road", "avenue", "boulevard", "lane", "highway", "expressway", "path", "trail"})
# Words that make a candidate a DIFFERENT kind of POI than the queried place when the query does not carry the same
# group ("Terelj Hotel" for "Terelj valley", "Phi Phi Natural Resort" for "Koh Phi Phi village", "Tingtibi - Panbang
# Highway" for "Tingtibi"): the geocoder matched the name inside another POI's name.
_OTHER_POI_GROUPS = {
    "lodging": frozenset({"hotel", "hostel", "inn", "motel", "resort", "guesthouse", "homestay", "villa", "apartment",
                          "apartments", "condo", "suites", "lodge", "hotels", "resorts", "bnb"}),
    "food": frozenset({"restaurant", "cafe", "coffee", "bar", "pub", "bistro", "bakery", "eatery", "kitchen",
                       "canteen"}),
    "shop": frozenset({"shop", "store", "mart", "mall", "supermarket", "boutique", "souvenir", "gift", "showroom",
                       "pharmacy"}),
    "office": frozenset({"bank", "atm", "office", "branch", "agency", "travel", "tours", "rental", "company",
                         "headquarters", "hq", "clinic", "hospital"}),
    "school": frozenset({"school", "college", "university", "kindergarten", "academy", "institute"}),
    "transport": frozenset({"parking", "garage", "bus", "stop", "toilet", "toilets", "petrol", "gas"}),
    "road": _ROAD_WORDS,
}
# A query word of the same group allows the candidate word ("hotel in Seoul" never gets here: name_filter drops it).
_OTHER_POI_WORD = {w: g for g, ws in _OTHER_POI_GROUPS.items() for w in ws}
STREET_NEAR_HINT_KM = 5.0     # a street for a site is only its access road: taken only right next to the model's point
# ambiguous_route: a tour route counts as MISSED only when the candidate is farther than this from it — the homonym
# distance (Beijing's Bell Tower for a Xi'an tour ~900 km, Mumbai's Princess Street for a Kochi tour ~1,000 km). Nearer
# routes are the same place seen from another itinerary: a day trip from a base town (Ayutthaya ~65 km from a Bangkok
# stay), a flight leg passing ~130 km away (Siem Reap on a Bangkok → Phnom Penh leg). Review fix: counting a route as
# covered only within the 50 km site radius refused such correct candidates when they were near one route.
AMBIGUOUS_ROUTE_KM = 300.0
# the reasons only the strict rule gives (Verdict.reasons "<i>:<reason>"; locate records them in last_error)
STRICT_REASONS = frozenset({"site_type", "match", "kind", "ambiguous_route", "hint_disagree", "day_disagree",
                            "day_disagree_near"})


@dataclass(frozen=True)
class StrictParams:
    """The knobs of a strict rule (AA-997, S258 P4 tune). The defaults are aa997.2 exactly.
      anchored_only   route evidence for the strict checks (ambiguous_route, hint_disagree, the re-judge's off-route
                      check) only from routes whose use day has its OWN located overnight stop (store.route_anchors
                      marks them `anchored`; a plain tuple counts as anchored). A route without it is the stops of the
                      days before / after — an arrival or departure day, a flight, a trek night at an unlocated hut,
                      or the place itself being that night's stop (excluded from its own route) — so a correct pin
                      can be hundreds of km from it;
      per_tour        ambiguous_route counts tours, not (tour, day) uses (a tour is near when any of its days is);
      ambiguous_km    the homonym distance of ambiguous_route (AMBIGUOUS_ROUTE_KM);
      ambiguous_miss_div  misses allowed = routes // this (5 = one per 5; 2 = a minority);
      rejudge_off_route_km  relocate.rejudge refuses a stored answer this far off the route (aa997.2: the day-trip
                      distance, 150 km);
      rejudge_v1_checks  relocate.rejudge re-applies v1's name / type checks (aa997.2). They were passed when the
                      answer was accepted; a re-judge can fail them only because it cannot rebuild the judged text
                      (a native-script query, an older normalisation), so aa997.3 lists them as `unjudged` instead;
      site_admin_named  a site query answered by a city / suburb / district whose own name carries the query's site
                      word ("Horombo Hut" mapped as a locality) is the site, not refused as `site_type`;
      day_guard       (aa997.4) the day-evidence guard: a move away from the model's point that the use day's own
                      evidence (its other located places, its own overnight stop) contradicts is refused as
                      `day_disagree` (a far move) or `day_disagree_near` (a near move) (day_verdict)."""
    anchored_only: bool = False
    per_tour: bool = False
    ambiguous_km: float = AMBIGUOUS_ROUTE_KM
    ambiguous_miss_div: int = 5
    rejudge_off_route_km: float = 150.0          # guards.DAY_TRIP_KM (not imported here: guards imports this module)
    rejudge_v1_checks: bool = True
    site_admin_named: bool = False
    day_guard: bool = False


_V3_PARAMS = StrictParams(anchored_only=True, rejudge_off_route_km=500.0, rejudge_v1_checks=False,
                          site_admin_named=True)
STRICT_PARAMS = {
    ACCEPT_RULE: StrictParams(),
    ACCEPT_RULE_V3: _V3_PARAMS,
    ACCEPT_RULE_V4: replace(_V3_PARAMS, day_guard=True),
}


def strict_rule(strict) -> Optional[str]:
    """The strict rule a `strict` argument names: True → ACCEPT_RULE (aa997.2), a name in STRICT_RULES → that rule,
    anything else (False, None, "v1") → None (rule v1)."""
    if strict is True:
        return ACCEPT_RULE
    return strict if isinstance(strict, str) and strict in STRICT_RULES else None


def strict_params(rule: Optional[str]) -> StrictParams:
    return STRICT_PARAMS.get(rule or ACCEPT_RULE, STRICT_PARAMS[ACCEPT_RULE])


def needs_day_places(rule) -> bool:
    """The rule judges with the use day's other places (aa997.4): the route reader must load them
    (store.route_anchors `day_places`)."""
    r = strict_rule(rule)
    return bool(r) and strict_params(r).day_guard


def evidence_routes(routes, params: Optional[StrictParams] = None) -> tuple:
    """The routes a strict check may use as evidence (StrictParams.anchored_only); all of them otherwise."""
    if params is None or not params.anchored_only:
        return tuple(routes or ())
    return tuple(r for r in routes or () if getattr(r, "anchored", True))


def is_site(query: str) -> bool:
    """A query that names a point-like site (temple, airport, museum, peak, pass …), not a town / area / long feature:
    an admin area (city / suburb / district) or a fallback match is never its answer."""
    words = _words(query)
    return bool(words & SITE_WORDS) and not (words & LONG_FEATURE_WORDS) and feature_kind(query) != AREA


def kind_conflict(query: str, candidate: Optional[str]) -> Optional[str]:
    """The group of an other-POI word the candidate's name carries and the query does not ('lodging' for "Terelj Hotel"
    asked as "Terelj valley"); None when the candidate is the same kind of thing."""
    q_words = _words(query)
    q_groups = {_OTHER_POI_WORD[w] for w in q_words if w in _OTHER_POI_WORD}
    for w in _words(candidate or "") - q_words:
        g = _OTHER_POI_WORD.get(w)
        if g and g not in q_groups:
            return g
    return None


def route_cover(lat: float, lng: float, routes, radius: float, *, per_tour: bool = False) -> tuple[int, int]:
    """(routes the point is within `radius` km of, routes with a stop). One destination serves every tour that uses
    the name (the key has no town on ~99 % of rows), so its point must not be a homonym for any of them. `per_tour`
    counts tours instead (a route's `tour` attribute; a route without one is its own tour)."""
    from services.places import guards
    groups: dict = {}
    for i, r in enumerate(routes or ()):
        d = guards.route_km(lat, lng, [r])
        if d is None:
            continue
        key = (getattr(r, "tour", None) or ("route", i)) if per_tour else ("route", i)
        groups[key] = groups.get(key, False) or d <= radius
    return sum(groups.values()), len(groups)


def _site_named(query: str, candidate: Optional[str]) -> bool:
    """The candidate's own name carries every site word of the query ("Horombo Hut" for "Horombo Hut")."""
    site = _words(query) & SITE_WORDS
    return bool(site) and site <= _words(candidate or "")


def strict_reason(query: str, c: Candidate, sim: float, lat: Optional[float], lng: Optional[float], routes=(), *,
                  settlement: bool = False, forced_town: bool = False, day_trip: bool = False,
                  check_kind: bool = True, unjudged: Optional[list] = None,
                  hint_km: Optional[float] = None, params: Optional[StrictParams] = None) -> Optional[str]:
    """Why a candidate that passed name / type / distance is still refused (AA-997 v2, rule ACCEPT_RULE), else None:
      `site_type`  a site query answered by a city / suburb / district, or by a street away from the model's point;
      `match`  a fallback match (the containing city / state / postcode) for anything but a settlement query;
      `kind`   the candidate is another kind of POI carrying the name (kind_conflict), farther than
               STREET_NEAR_HINT_KM from the model's point (next to it, it is the place's access point);
      `ambiguous_route`  ≥ 2 tour routes use the name and the candidate is farther than AMBIGUOUS_ROUTE_KM from more
               of them than one per 5 (a homonym: one pin cannot serve tours in two distant cities);
      `hint_disagree`  route reference, the model's point is ON the route and the candidate is beyond the bake-off
               radius of it: only an exact-name, full-match POI may overrule an on-route model point;
      `day_disagree` / `day_disagree_near`  (aa997.4, StrictParams.day_guard) the use day's own evidence sides with
               the model's point against the candidate (day_verdict: a far move / a near move, ≤ the bake-off radius)
               — judged on every route the name has (a day's located places are evidence whether or not its night is
               located); with no model's point `day_disagree` is listed in `unjudged`.
    The street / kind checks need the model's point: with none (lat / lng None — a model null, a stored answer
    re-judged without its hint) they use `hint_km` (the candidate's distance from that point when only the distance is
    known: an aa997.1 relocate stamp's moved_km), else they are NOT judged, never refused; their names are appended to
    `unjudged` when given. `params` (default aa997.2) are the rule's knobs (StrictParams): the route checks use only
    evidence_routes."""
    from services.places import guards
    p = params or STRICT_PARAMS[ACCEPT_RULE]
    all_routes = routes
    routes = evidence_routes(routes, p)
    # `settlement`: the query names a town (no feature word) or a town is asked for; `forced_town`: the town fallback
    site = is_site(query) and not forced_town
    rtype, mtype = c.result_type or "", c.match_type or ""
    has_hint = lat is not None and lng is not None
    hint_dist = _km(lat, lng, c.lat, c.lng) if has_hint else hint_km
    road_query = bool(_words(query) & _ROAD_WORDS)
    if site and rtype in ADMIN_TYPES and not (p.site_admin_named and _site_named(query, c.name)):
        return "site_type"
    if site and rtype == "street" and not road_query:
        if hint_dist is None:
            if unjudged is not None:
                unjudged.append("street")
        elif hint_dist > STREET_NEAR_HINT_KM:
            return "site_type"
    if mtype in FALLBACK_MATCH_TYPES and (site or not settlement):
        return "match"
    if site and mtype == "match_by_street" and not road_query:
        return "match"
    if check_kind and kind_conflict(query, c.name):
        if hint_dist is None:
            if unjudged is not None:
                unjudged.append("kind")
        elif hint_dist > STREET_NEAR_HINT_KM:
            return "kind"       # a POI named after the place right next to the model's point is its access point
    if guards.has_route(routes):
        near, total = route_cover(c.lat, c.lng, routes, p.ambiguous_km, per_tour=p.per_tour)
        if total >= 2 and total - near > total // p.ambiguous_miss_div:
            return "ambiguous_route"
        if has_hint and not day_trip:
            hint_on = (guards.route_km(lat, lng, routes) or 0.0) <= guards.route_radius(query)
            far = _km(lat, lng, c.lat, c.lng) > radius_km(query)
            strong = (sim >= 0.999 and rtype not in ADMIN_TYPES and rtype != "street"
                      and mtype in ("", "full_match"))
            if hint_on and far and not strong:
                return "hint_disagree"
    if p.day_guard:
        if has_hint:
            dv = day_verdict(query, c, lat, lng, all_routes)
            if dv is not None:
                return DAY_REASONS[dv]
        elif unjudged is not None and has_day_evidence(all_routes):
            unjudged.append("day_disagree")
    return None


# ── AA-997 S261: the day-evidence guard (rule aa997.4) ──────────────────────────────────────────────────────────────
# The S261 audit of the aa997.3 relocate chain: of the rows the S254 audit labelled CORRECT on the model's point, six
# were moved by a `confirmed` outcome to a different feature with the same name — a temple of the same name in the
# next overnight town of the itinerary (142 km), a same-name temple near the base city of a day trip (118 km), another
# fort / gurudwara of the same name 4-5 km away in the same city. aa997.3 lets an exact-name full match overrule an
# on-route model point (`hint_disagree` "strong"), and a move inside the bake-off radius is never questioned. The
# evidence that decides is the use DAY itself: the other places of that day that a geocoder located (geo.tour_place,
# store.route_anchors `day_places`) and the day's own overnight stop (TourRoute.own). A move the day contradicts is
# refused. A FAR refusal (> the bake-off radius: another town of the itinerary) is strong evidence — the relocate job
# then keeps the model's point (outcome `kept_model`). A NEAR refusal (another same-name feature in the same city) is
# weak evidence: inside a city the day's sites cluster around the model's point whether it is right (the 4-5 km
# regressions) or wrong (a palace out of town, the model's point in the old town) — the reason is `day_disagree_near`
# and relocate leaves the row on its old, hideable flag (the status quo before aa997.3), never `kept_model`.
DAY_MIN_MOVE_KM = 2.0       # a move this short is never judged (same feature, another entrance / OSM node)
DAY_SUPPORT_KM = 3.0        # the model's point is backed by a located place of the day this close to it
DAY_MARGIN_KM = 1.5         # "clearly nearer": at most half the distance and at least this much nearer
DAY_CENTROID_KM = 0.5       # the model's point IS the day's overnight town point (a town fallback, not a site)
DAY_OWN_STOP_KM = 15.0      # a far move away from a model's point this close to the day's own overnight stop
# a near move is judged only with this many located places of the day within DAY_SUPPORT_KM of the model's point (one
# site nearby is no evidence inside a city)
DAY_NEAR_SUPPORT_N = 2
# a model's point this close to another located place of the day is that place's point copied (the usual model slip):
# a near move away from it is not judged
DAY_COPIED_KM = 0.3
DAY_FAR, DAY_NEAR = "far", "near"
DAY_REASONS = {DAY_FAR: "day_disagree", DAY_NEAR: "day_disagree_near"}


def _day_points(routes) -> tuple[list, list]:
    """(the day's other located places, the use days' own overnight stops) of the routes (guards.TourRoute `day` /
    `own`; a plain tuple carries neither)."""
    day = [p for r in routes or () for p in (getattr(r, "day", None) or ()) if p is not None]
    own = [o for r in routes or () for o in [getattr(r, "own", None)] if o is not None]
    return day, own


def has_day_evidence(routes) -> bool:
    day, own = _day_points(routes)
    return bool(day or own)


def _nearest_km(lat: float, lng: float, pts) -> Optional[float]:
    return min((_km(lat, lng, p[0], p[1]) for p in pts), default=None)


def _clearly_nearer(a: float, b: float) -> bool:
    """`a` is clearly nearer than `b`: at most half of it and at least DAY_MARGIN_KM less."""
    return a * 2 <= b and b - a >= DAY_MARGIN_KM


def day_verdict(query: str, c: Candidate, lat: float, lng: float, routes) -> Optional[str]:
    """DAY_FAR / DAY_NEAR when the use day's evidence contradicts moving the model's point (lat, lng) to the candidate
    (rule aa997.4), else None:
      1. the day's other located places: the model's point has one within DAY_SUPPORT_KM and is clearly nearer to
         them than the candidate → refused. When they are clearly nearer the CANDIDATE the move is taken (the model
         put the site in the wrong place; its day's places are around the right one) and rule 2 is not asked.
         A NEAR move (≤ the bake-off radius) is judged by them only when the model's point is a site of its own among
         them: not the day's overnight town point (DAY_CENTROID_KM — the model gave the town, the site is often out
         of town), not another place's point copied (DAY_COPIED_KM), and with DAY_NEAR_SUPPORT_N of them within
         DAY_SUPPORT_KM;
      2. a far move (> the bake-off radius) from a model's point within DAY_OWN_STOP_KM of the use day's own overnight
         stop to a candidate near ANOTHER stop of the route (within the bake-off radius of a stop that is farther than
         that radius from every own stop): two same-name features on one itinerary, and the day names the model's.
    Never judged for a move ≤ DAY_MIN_MOVE_KM, or with no day evidence (the result is aa997.3's)."""
    move = _km(lat, lng, c.lat, c.lng)
    if move <= DAY_MIN_MOVE_KM:
        return None
    day, own = _day_points(routes)
    radius = radius_km(query)
    far = move > radius
    if day:
        dists = [_km(lat, lng, p[0], p[1]) for p in day]
        dm, dc = min(dists), _nearest_km(c.lat, c.lng, day)
        if _clearly_nearer(dc, dm):
            return None
        if dm <= DAY_SUPPORT_KM and _clearly_nearer(dm, dc):
            if far:
                return DAY_FAR
            centroid = any(_km(lat, lng, o[0], o[1]) <= DAY_CENTROID_KM for o in own)
            support = sum(d <= DAY_SUPPORT_KM for d in dists)
            if not centroid and dm > DAY_COPIED_KM and support >= DAY_NEAR_SUPPORT_N:
                return DAY_NEAR
    if far and own and (_nearest_km(lat, lng, own) or 0.0) <= DAY_OWN_STOP_KM:
        stops = [p for r in routes or () for p in r if p is not None]
        stop = min(stops, key=lambda p: _km(c.lat, c.lng, p[0], p[1]), default=None)
        if (stop is not None and _km(c.lat, c.lng, stop[0], stop[1]) <= radius
                and (_nearest_km(stop[0], stop[1], own) or 0.0) > radius):
            return DAY_FAR
    return None


def day_disagree(query: str, c: Candidate, lat: float, lng: float, routes) -> bool:
    """day_verdict refuses the move (far or near)."""
    return day_verdict(query, c, lat, lng, routes) is not None


def strict_refusals(reasons) -> list[str]:
    """The strict-only reasons in a Verdict / tried list ("0:ambiguous_route" → "ambiguous_route"), in order, once."""
    out = []
    for r in reasons or ():
        why = str(r).split(":", 1)[-1]
        if why in STRICT_REASONS and why not in out:
            out.append(why)
    return out


# ── AA-997 v2: name similarity that survives transliteration joins (strategy v2 only) ─────────────────────────────
# "Konglor" = "Kong Lor", "Pingan" = "Ping'an": the bake-off's per-token difflib ≥ 0.85 misses a joined / split
# transliteration. "Mt" / "Mount" and the context prepositions never decide a match ("Mt Palay-Palay" vs "Mount
# Palay-Palay"; "Kumbum Monastery in Qinghai" vs "Kumbum Monastery").
V2_EXTRA_STOP = frozenset({"mt", "mount", "in", "at", "near", "by"})


def tokens_v2(query: str) -> list[str]:
    return [t for t in tokens(query) if t not in V2_EXTRA_STOP]


def name_sim_v2(query: str, candidate: Optional[str]) -> float:
    """name_sim with V2_EXTRA_STOP dropped and compact matching: a query token equal to 2–3 consecutive candidate
    tokens joined, or 2–3 consecutive query tokens joined equal to a candidate token, count as matched."""
    qt = tokens_v2(query)
    if not qt:
        return name_sim(query, candidate)
    cl = _TOKENS.findall(_strip(_PARENS.sub(" ", (candidate or "").lower())))
    ct = set(cl)
    joined_c = {"".join(cl[i:i + n]) for n in (2, 3) for i in range(len(cl) - n + 1)}
    matched = [t in ct or t in joined_c or any(difflib.SequenceMatcher(None, t, c).ratio() >= 0.85 for c in ct)
               for t in qt]
    for n in (2, 3):
        for i in range(len(qt) - n + 1):
            if "".join(qt[i:i + n]) in ct:
                for k in range(i, i + n):
                    matched[k] = True
    return sum(matched) / len(qt)


# ── AA-997 v2: query variants (strategy v2) ─────────────────────────────────────────────────────────────────────────
_CONTEXT_SPLIT = re.compile(r"\s+(?:in|near|at)\s+(?=[A-Z])")
_OR_SPLIT = re.compile(r"\s+or\s+", re.I)


_DIRECTION_WORDS = frozenset({"north", "south", "east", "west", "northern", "southern", "eastern", "western",
                              "upper", "lower", "inner", "outer", "old", "new", "main", "central", "side", "entrance",
                              "gate", "area", "part"})


def _clean_part(text: Optional[str]) -> str:
    """A context / alias part worth searching: altitudes stripped, and at least one proper word (not only generic /
    kind / direction words: "north square", "fort", "4820 m" are not)."""
    t = " ".join(_ALTITUDE.sub(" ", text or "").split()).strip(" ,;:-–—.")
    proper = {w for w in _TOKENS.findall(_strip(t.lower())) if len(w) > 1} - GENERIC_WORDS - FEATURE_WORDS \
        - _SITE_EXTRA - SIM_STOP - _DIRECTION_WORDS
    return t if t and proper else ""


def _first_of_or(text: str) -> str:
    """'Tham Xang or Tham Hoi caves' → 'Tham Xang caves' (the shared kind word kept); other text unchanged."""
    if not _OR_SPLIT.search(text) or _TO_SPLIT.search(text):
        return text
    first = _OR_SPLIT.split(text)[0]
    tail_kind = [w for w in _TOKENS.findall(_strip(text.lower()))[-1:] if w in FEATURE_WORDS]
    return first + (" " + tail_kind[0] if tail_kind and tail_kind[0] not in _words(first) else "")


def query_plan(name: str, *, context: Optional[str] = None, max_queries: int = 3) -> list[tuple[str, str]]:
    """(text to search, bare name to judge) pairs for strategy v2, in order, at most `max_queries`. The judged name
    never includes a context town, so a candidate matching only the town ("Andong Folk Village" for "Hahoe Folk
    Village, Andong") fails.
      1. "X in / near / at <Proper>": the name's own disambiguator goes first ("Gandhi Ashram, Sabarmati"), then the
         bare X. When X alone is generic ("Great Wall at Mutianyu") the full name is searched and judged (as v1), then
         "<Context> X" ("Mutianyu Great Wall").
      2. Otherwise the normalised name(s) first (as v1).
      3. A parenthetical naming a site ("Palace of the Winds (Hawa Mahal)", "the White Temple (Wat Rong Khun)") is an
         alias: searched and judged on its own. Any other parenthetical / ", part" is a context ("Hanok Village
         (Jeonju)" → "Hanok Village, Jeonju").
      4. `context` (relocate: the day's overnight town) when the name gave none — tried last.
    " or " compounds are judged on their first part ("Tham Xang or Tham Hoi caves" → "Tham Xang caves")."""
    raw = " ".join(_ALTITUDE.sub(" ", name or "").split())
    paren = " ".join(m.strip() for m in re.findall(r"\(([^)]*)\)", raw))
    flat = " ".join(_PARENS.sub(" ", raw).split())
    head, _, comma_tail = flat.partition(",")
    head = head.strip()
    plan: list[tuple[str, str]] = []
    ctx = ""
    m = _CONTEXT_SPLIT.search(head)
    if m:
        base, inline = _first_of_or(head[:m.start()].strip()), _clean_part(head[m.end():])
        if inline and normalise(base):
            b = normalise(base)[0]
            plan += [(f"{b}, {inline}", b), (b, b)]
        else:       # a generic X: only the whole name identifies the place
            whole = _first_of_or(head)
            plan += [(q, q) for q in normalise(whole)]
            if inline and plan:
                plan.append((f"{inline} {_CONTEXT_SPLIT.split(head)[0].strip()}", plan[0][1]))
    else:
        plan += [(q, q) for q in normalise(_first_of_or(head))]
    alias = _clean_part(_first_of_or(paren)) if paren else ""
    if alias and (_words(paren) & (FEATURE_WORDS | _SITE_EXTRA)) and not _TO_SPLIT.search(alias):
        a = normalise(alias)
        if a:
            plan.insert(1 if plan else 0, (a[0], a[0]))
        alias_used = True
    else:
        alias_used = False
    if not m:
        ctx = _clean_part(comma_tail) or ("" if alias_used else _clean_part(paren))
    ctx = ctx or ("" if m else " ".join((context or "").split()))
    if plan and ctx and name_sim(ctx, plan[0][1]) < 0.999:
        plan.append((f"{plan[0][1]}, {ctx}", plan[0][1]))
    return list(dict.fromkeys(plan))[:max(1, max_queries)]


def with_fallback(fallback: Optional[Verdict], reasons: list[str]) -> Verdict:
    """The `day_trip` second choice when there is one (its reasons list the scan), else no candidate."""
    if fallback is None:
        return Verdict(None, reasons=reasons)
    return Verdict(fallback.accepted, fallback.rank, fallback.sim, fallback.km, reasons)
