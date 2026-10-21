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
from dataclasses import dataclass, field
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


def accept(query: str, cands: list[Candidate], lat: float, lng: float, iso2: str = "",
           top_n: int = TOP_N) -> Verdict:
    """The first of the top `top_n` candidates passing (a) name, (b) type, (c) distance; country must match
    `iso2` when given (the request filters by country already — this is the hard guard)."""
    n_tok = len(tokens(query))
    settlement = is_settlement(query)
    radius = radius_km(query)
    reasons = []
    for i, c in enumerate(cands[:top_n]):
        sim = name_sim(query, c.name)
        dist = _km(lat, lng, c.lat, c.lng)
        if iso2 and c.iso2 and c.iso2 != iso2:
            reasons.append(f"{i}:country")
        elif not (sim >= 0.999 or (sim >= 0.5 and n_tok >= 2)):
            reasons.append(f"{i}:name")
        elif c.result_type in REJECT_TYPES or (c.result_type == "city" and not settlement):
            reasons.append(f"{i}:type")
        elif dist > radius:
            reasons.append(f"{i}:distance")
        else:
            return Verdict(c, i, round(sim, 3), round(dist, 2), reasons)
    return Verdict(None, reasons=reasons)
