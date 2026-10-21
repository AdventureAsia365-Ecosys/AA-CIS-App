"""AA-847 — content-validity report of the live catalog's places: deterministic, no model call.

A pre-flip gate for TripPlanner's PLACES_SOURCE=cis (ADR 0007 decision 10) and a standing check after every
tour_graph_build (its counts land in geo.graph_build.stats['validity'], so /admin/places shows a trend). Every item
carries a REASON code from a fixed taxonomy (below) so the counts are comparable run to run; the same-place rule (V2)
is pinned by a labelled keep / merge pair set in tests/unit/test_aa847_validity.py, with false merges measured
(lesson S245 / AA-779: a wrong merge is the costly error).

Checks (per live tour; the v_tour liveness rule on the base tables, read as cis_app with the AA master tenant named
explicitly — cis_app's RLS policies are USING (true)):

  V1 duration — the tour's day count disagrees with its duration:
     ranged_heading          a "Day 5-6 —" heading: the canonical parser (itinerary_utils / catalog._tour_day_text)
                             folds that text into the previous day, while days.py covers both days
     missing_day_numbers     canonical "Day N —" numbers are not 1..max
     duration_text_vs_days   fewer itinerary days than duration_days (the v_tour rule: the duration text first,
                             else the highest canonical day)
     extra_days              more itinerary days than duration_days
  V2 same place repeated within one tour:
     same_destination_names  one destination under ≥ 2 component spellings (name_norm)
     near_duplicate          two destinations of the same country with the same distinctive name (tokens after
                             generic words, or the same letters run together: "Kuangsi" = "Kuang Si"), ≤ 2 km apart,
                             ≤ 5 km when either point is the model's (llm) or an area
  V3 not a pin — a linear or area feature stored as a point (geomatch.feature_kind, name + Geoapify result_type):
     linear_feature          trail / river / road / loop … NOT stored as a line (geo_precision point / area): drawn
                             as a pin or a circle today. Owner decision S246: lines are drawn as lines, so the fix
                             stores it 'line' and queues its shape
     area_feature            national park / delta / plateau / coast / gorge / range … (drawn as an area)
     linear_without_shape    a line destination whose geo.place_shape is not drawable yet (pending / not_found /
                             failed, or no row): TripPlanner can only draw the provisional "approximate route"
  V4 the component's own tour-day text does not name it (AA-819, the narration day bug):
     no_day_text             the component's day has no canonical day block
     day_text_not_naming     the day block does not name the component
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from services.places import days as days_mod
from services.places import geomatch

MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"

V1, V2, V3, V4 = "duration", "same_place", "not_a_pin", "day_text"
CHECKS = (V1, V2, V3, V4)
REASONS = {
    V1: ("ranged_heading", "missing_day_numbers", "duration_text_vs_days", "extra_days"),
    V2: ("same_destination_names", "near_duplicate"),
    V3: ("linear_feature", "area_feature", "linear_without_shape"),
    V4: ("no_day_text", "day_text_not_naming"),
}

# The pre-flip gate (Done-when 3: no duplicate pin, no trail pin on the map): only the findings a map viewer sees and
# the validity fix clears. near_duplicate (two pins of one place) clears with the duplicate fix; linear_feature with
# the line fix (stored 'line', shape queued); area_feature with the area fix (stored as an area); linear_without_shape
# when the place_shape job (or the admin's GeoJSON) draws the line — an anchored "approximate" shape counts as drawn
# (open question to Nghiệp, proposed yes; approximate shapes are listed separately on /admin/places Lines).
# Informational, never blocking: same_destination_names (one destination, several spellings — one pin; it is what a
# near_duplicate turns into once the in-tour dedupe or the fix points the spellings at the keeper, e.g. Kuang Si),
# V1 duration and V4 day text (content defects with their own fixes, not a map defect).
GATE_REASONS = ("near_duplicate", "linear_feature", "area_feature", "linear_without_shape")
# geo.place_shape statuses TripPlanner draws as a line (approximate: dashed, labelled "approximate route")
DRAWN_SHAPE_STATUSES = ("ok", "manual", "approximate")

NEAR_KM = 2.0
NEAR_KM_LOOSE = 5.0
# Words that name a kind of place in the languages of the catalog, not one place (Lao wat / tat / that / phou / nam /
# ban, Vietnamese chua / den / ho / nui / song …): two different temples 1 km apart share "wat", not a name.
# The local words are split from the English ones: a local kind word is often run together with the name in one
# spelling and not in the other ("Phousi" = "Phou Si"), so the run-together comparison keeps the local words.
LOCAL_KIND_WORDS = frozenset({
    "wat", "vat", "ban", "tat", "that", "phou", "phu", "nam", "huay", "xe", "chua", "den", "thap", "cau", "nui", "ho",
    "song", "doi", "khao", "ko", "koh", "phra", "tham", "pha", "muang", "mueang"})
ENGLISH_KIND_WORDS = frozenset({
    "mt", "mount", "hill", "hills", "old", "town", "national", "central", "great", "grand", "big", "small", "upper",
    "lower", "north", "south", "east", "west", "night", "market", "museum", "palace", "royal", "garden", "gardens",
    "square", "bridge"})
SAME_PLACE_GENERIC = LOCAL_KIND_WORDS | ENGLISH_KIND_WORDS

# Kind of place a name says it is (English and local words; geomatch.tokens drops some of these — falls, cave, lake,
# river, temple … — so the kinds are read from the raw words). Two names that say different kinds are two places even
# with the same distinctive name: "Long Bien Bridge" / "Long Bien Market", "Hoan Kiem Lake" / "Hoan Kiem Night
# Market", "Ben Thanh Market" / "Ben Thanh Night Market", "Mount Phousi" / "Phousi Market" (review S246). Synonyms
# share a kind ("Tat Kuang Si" = "Kuang Si Falls", "Mount Phousi" = "Phou Si Hill"). A kind only ever keeps two names
# apart, so a missing synonym costs a missed merge, never a wrong one. Local words that also start proper names (ho:
# Ho Chi Minh, ban: Ban Gioc, phu: Phu Quoc, khao: Khao Sok) are left out: they would only cause missed merges.
_KIND_OF_WORD = {
    **dict.fromkeys(("falls", "fall", "waterfall", "waterfalls", "cascade", "cascades", "tat", "thac"), "waterfall"),
    **dict.fromkeys(("cave", "caves", "grotto", "tham"), "cave"),
    **dict.fromkeys(("mount", "mt", "mountain", "mountains", "hill", "hills", "peak", "phou", "nui", "doi"),
                    "mountain"),
    **dict.fromkeys(("lake", "pond", "lagoon", "reservoir"), "lake"),
    **dict.fromkeys(("river", "stream", "nam", "song", "xe", "huay"), "river"),
    **dict.fromkeys(("island", "islands", "isle", "ko", "koh"), "island"),
    **dict.fromkeys(("village",), "village"),
    **dict.fromkeys(("valley",), "valley"),
    **dict.fromkeys(("pass",), "pass"),
    **dict.fromkeys(("beach",), "beach"),
    **dict.fromkeys(("park",), "park"),
    **dict.fromkeys(("town", "city", "muang", "mueang"), "town"),
    **dict.fromkeys(("market", "markets", "cho"), "market"),
    **dict.fromkeys(("bridge", "cau"), "bridge"),
    **dict.fromkeys(("museum",), "museum"),
    **dict.fromkeys(("palace",), "palace"),
    **dict.fromkeys(("garden", "gardens"), "garden"),
    **dict.fromkeys(("square",), "square"),
    **dict.fromkeys(("temple", "temples", "wat", "vat", "chua", "den", "pagoda", "monastery", "shrine"), "temple"),
    **dict.fromkeys(("stupa", "that", "thap"), "stupa"),
    **dict.fromkeys(("street", "road", "trail"), "street"),
    **dict.fromkeys(("wall", "gate", "fort", "citadel"), "fort"),
    **dict.fromkeys(("statue", "monument", "memorial", "mausoleum", "tomb"), "monument"),
}
# Kinds whose bare name usually IS the feature ("Hoan Kiem" = Hoan Kiem Lake, "Kuang Si" = the falls): one side
# naming such a kind and the other none can be one place. Every other kind is a built place inside a town or
# district of the same name ("Luang Prabang Night Market" is not "Luang Prabang", "Long Bien Bridge" is not the Long
# Bien district): a bare name never merges with it.
BARE_NAME_KINDS = frozenset({"waterfall", "cave", "mountain", "lake", "river", "island", "village", "valley", "pass",
                             "beach", "park", "town"})

_DURATION_RE = re.compile(r"(\d+)\s*-?\s*days?")
_CANONICAL_RE = re.compile(r"(?:^|\n)Day\s+(\d+)\s+—")


# ── V1 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def duration_days(duration_raw: Optional[str], itinerary: Optional[str]) -> Optional[int]:
    """catalog.v_tour.duration_days: the number in the duration text ('14 Days'), else the highest canonical day."""
    m = _DURATION_RE.search((duration_raw or "").lower())
    if m:
        return int(m.group(1))
    days = canonical_days(itinerary)
    return max(days) if days else None


def canonical_days(itinerary: Optional[str]) -> list[int]:
    """Canonical day numbers ("Day N —"), the parse_canonical_itinerary_days / _tour_day_text rule."""
    return sorted({int(m.group(1)) for m in _CANONICAL_RE.finditer(itinerary or "")})


def duration_findings(duration_raw: Optional[str], itinerary: Optional[str]) -> list[tuple[str, str]]:
    """[(reason, detail)] of V1 for one tour."""
    out = []
    dd = duration_days(duration_raw, itinerary)
    canon = canonical_days(itinerary)
    blocks = days_mod.parse_days(itinerary or "")
    ranged = [f"{b.day_from}-{b.day_to}" for b in blocks if b.day_to > b.day_from]
    covered = sorted({d for b in blocks for d in range(b.day_from, b.day_to + 1)})
    if ranged:
        out.append(("ranged_heading", f"headings {', '.join(ranged)}"))
    if canon and canon != list(range(1, max(canon) + 1)):
        missing = sorted(set(range(1, max(canon) + 1)) - set(canon))
        out.append(("missing_day_numbers", f"missing {', '.join(map(str, missing[:10]))}" if missing
                    else "repeated day numbers"))
    n_days = max(covered) if covered else (max(canon) if canon else None)
    if dd is not None and n_days is not None:
        if n_days < dd:
            out.append(("duration_text_vs_days", f"duration {dd} days, itinerary {n_days} days"))
        elif n_days > dd:
            out.append(("extra_days", f"duration {dd} days, itinerary {n_days} days"))
    return out


# ── V2 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _strip(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return s.replace("đ", "d").replace("Đ", "D")


def distinctive_tokens(name: Optional[str]) -> list[str]:
    """geomatch tokens (feature words such as falls / cave / lake dropped) minus SAME_PLACE_GENERIC, in order."""
    return [t for t in geomatch.tokens(name or "") if t not in SAME_PLACE_GENERIC]


def place_kinds(name: Optional[str]) -> frozenset[str]:
    """The kinds of place a name says it is (see _KIND_OF_WORD). "night market", "old town" / "old quarter" /
    "ancient town" and "national park" are kinds of their own: a night market is not the day market next to it, an
    old town is not the town."""
    words = re.findall(r"[a-z0-9]+", _strip((name or "").lower()))
    out = {_KIND_OF_WORD[w] for w in words if w in _KIND_OF_WORD}
    pairs = set(zip(words, words[1:]))
    if ("night", "market") in pairs or ("night", "markets") in pairs:
        out.discard("market")
        out.add("night_market")
    if pairs & {("old", "town"), ("old", "quarter"), ("ancient", "town"), ("old", "city")}:
        out.discard("town")
        out.add("old_town")
    if ("national", "park") in pairs:
        out.discard("park")
        out.add("national_park")
    return frozenset(out)


def same_kind(a: Optional[str], b: Optional[str]) -> bool:
    """The two names can be one place by kind: both name the same kinds, or one names none and the other only kinds
    whose bare name is the feature (BARE_NAME_KINDS)."""
    ka, kb = place_kinds(a), place_kinds(b)
    if ka and kb:
        return ka == kb
    return (ka or kb) <= BARE_NAME_KINDS


def same_name(a: Optional[str], b: Optional[str]) -> bool:
    """The same distinctive name of the same kind: equal token sets, or the same letters run together ("Kuangsi" /
    "Kuang Si"; "Mount Phousi" / "Phou Si Hill", compared with the local kind words kept), and no different kinds
    ("Long Bien Bridge" is not "Long Bien Market"). A name with no distinctive token never matches."""
    ta, tb = distinctive_tokens(a), distinctive_tokens(b)
    if not ta or not tb or not same_kind(a, b):
        return False
    if set(ta) == set(tb) or "".join(ta) == "".join(tb):
        return True
    ra, rb = (["".join(t for t in geomatch.tokens(x or "") if t not in ENGLISH_KIND_WORDS)] for x in (a, b))
    return ra[0] == rb[0] and len(ra[0]) >= 4


def km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


@dataclass(frozen=True)
class Place:
    destination_id: str
    name: str
    country: Optional[str]
    lat: float
    lng: float
    located_by: Optional[str] = None
    precision: Optional[str] = None
    review_reason: Optional[str] = None
    result_type: Optional[str] = None
    components: int = 0           # live components on it (keeper choice)
    shape_status: Optional[str] = None   # geo.place_shape.status of a line destination (None: no row)


def is_same_place(a: Place, b: Place) -> tuple[bool, Optional[float]]:
    """(same, km): same country, same distinctive name, within NEAR_KM (NEAR_KM_LOOSE when either is llm / area).
    AA-847: never for a line destination — a point distance means nothing for a river ("Mekong" and "Mekong River"
    can be geocoded 100 km apart and be one place); lines are matched on their shapes (services/places/line_dedupe)."""
    if a.destination_id == b.destination_id:
        return True, 0.0
    if "line" in (a.precision, b.precision):
        return False, None
    if (a.country or "").strip().lower() != (b.country or "").strip().lower():
        return False, None
    if not same_name(a.name, b.name):
        return False, None
    d = km(a.lat, a.lng, b.lat, b.lng)
    loose = any(p.located_by == "llm" or p.precision == "area" for p in (a, b))
    return d <= (NEAR_KM_LOOSE if loose else NEAR_KM), round(d, 2)


def keeper_of(places: Iterable[Place]) -> Place:
    """One keeper per cluster: a geocoder-confirmed point first, then the most live components, then the id."""
    return sorted(places, key=lambda p: (p.located_by != "geoapify", -p.components, p.destination_id))[0]


def clusters(places: list[Place]) -> list[list[Place]]:
    """Groups of ≥ 2 distinct destinations that are the same place (pairwise rule, single link inside one tour or
    one country; the group is cut where a member is not the same place as the keeper — no chaining across)."""
    by_id = {p.destination_id: p for p in places}
    parent = {i: i for i in by_id}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ids = sorted(by_id)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if is_same_place(by_id[a], by_id[b])[0]:
                parent[find(a)] = find(b)
    groups: dict[str, list[Place]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(by_id[i])
    out = []
    for g in groups.values():
        if len(g) < 2:
            continue
        k = keeper_of(g)
        members = [k] + [p for p in g if p is not k and is_same_place(k, p)[0]]   # group-level rule (lesson S239)
        if len(members) >= 2:
            out.append(members)
    return out


def v3_reason(p: Place) -> Optional[str]:
    """linear_feature / linear_without_shape / area_feature / None for one destination (see the module docstring)."""
    if p.precision == "line":
        return None if p.shape_status in DRAWN_SHAPE_STATUSES else "linear_without_shape"
    kind = geomatch.feature_kind(p.name, p.result_type)
    if kind == geomatch.LINEAR:
        return "linear_feature"
    if kind == geomatch.AREA and p.precision != "area":
        return "area_feature"
    return None


# ── V4 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _norm_text(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _strip(text or "").lower()).strip()


_NAMING_GENERIC = frozenset({"the", "of", "and", "in", "at", "to", "a"})


def day_text_names(day_text: Optional[str], name: Optional[str]) -> bool:
    """The day text names the component: the whole normalised name, or every distinctive word of it."""
    t, n = _norm_text(day_text), _norm_text(name)
    if not n or n in t:
        return True
    words = [w for w in n.split() if len(w) >= 3 and w not in _NAMING_GENERIC and w not in geomatch.SIM_STOP]
    tw = set(t.split())
    return bool(words) and all(w in tw for w in words)


def day_blocks(itinerary: Optional[str]) -> dict[int, str]:
    """day -> 'title\\nbody' of the canonical blocks (catalog._tour_day_text: a repeated day keeps the last block)."""
    out = {}
    for part in re.split(r"\n(?=Day\s+\d+\s+—)", itinerary or ""):
        m = re.match(r"\s*Day\s+(\d+)\s+—", part)
        if m:
            out[int(m.group(1))] = part
    return out


# ── report ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Item:
    check: str
    reason: str
    country: Optional[str]
    source_id: str
    tour_id: str
    title: Optional[str]
    day: Optional[int] = None
    names: list[str] = field(default_factory=list)
    destination_ids: list[str] = field(default_factory=list)
    detail: Optional[str] = None
    keeper_id: Optional[str] = None
    km: Optional[float] = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def evaluate(tours: list[dict], components: list[dict]) -> list[Item]:
    """tours: {source_id, tour_id, title, country, duration_raw, itinerary}; components: {source_id, day_index, name,
    name_norm, destination_id, destination_name, country, lat, lng, located_by, precision, review_reason,
    result_type, shape_status}. Returns every finding, deterministic order."""
    by_source: dict[str, list[dict]] = {}
    for c in components:
        by_source.setdefault(c["source_id"], []).append(c)
    comp_count: dict[str, int] = {}
    for c in components:
        if c.get("destination_id"):
            comp_count[str(c["destination_id"])] = comp_count.get(str(c["destination_id"]), 0) + 1
    items: list[Item] = []
    seen_pin: set[tuple] = set()
    for t in sorted(tours, key=lambda x: (str(x.get("country") or ""), str(x.get("title") or ""), x["source_id"])):
        base = dict(country=t.get("country"), source_id=t["source_id"], tour_id=str(t["tour_id"]), title=t.get("title"))
        for reason, detail in duration_findings(t.get("duration_raw"), t.get("itinerary")):
            items.append(Item(V1, reason, detail=detail, **base))
        comps = by_source.get(t["source_id"], [])
        # V2a: one destination under several spellings in this tour
        spellings: dict[str, dict[str, str]] = {}
        for c in comps:
            if c.get("destination_id"):
                spellings.setdefault(str(c["destination_id"]), {})[c.get("name_norm") or c["name"]] = c["name"]
        for dest, names in sorted(spellings.items()):
            if len(names) >= 2:
                items.append(Item(V2, "same_destination_names", names=sorted(names.values()), destination_ids=[dest],
                                  **base))
        # V2b: distinct destinations of this tour that are one place
        places = {}
        for c in comps:
            if c.get("destination_id") and c.get("lat") is not None and c.get("lng") is not None:
                d = str(c["destination_id"])
                places.setdefault(d, Place(d, c.get("destination_name") or c["name"], c.get("country"),
                                           float(c["lat"]), float(c["lng"]), c.get("located_by"), c.get("precision"),
                                           c.get("review_reason"), c.get("result_type"), comp_count.get(d, 0),
                                           c.get("shape_status")))
        for group in clusters(list(places.values())):
            k = group[0]
            dist = max(is_same_place(k, p)[1] or 0.0 for p in group[1:])
            items.append(Item(V2, "near_duplicate", names=[p.name for p in group],
                              destination_ids=[p.destination_id for p in group], keeper_id=k.destination_id,
                              km=dist, **base))
        # V3: per destination, once per tour. A linear feature is flagged while it is not stored as a line (a pin or
        # a circle at a trail's centroid); a line while its shape is not drawable; an area feature while it is stored
        # as a point.
        for d, p in sorted(places.items()):
            if (t["source_id"], d) in seen_pin:
                continue
            reason = v3_reason(p)
            if reason is None:
                continue
            seen_pin.add((t["source_id"], d))
            detail = (f"shape {p.shape_status or 'missing'}" if reason == "linear_without_shape"
                      else f"precision {p.precision}, result_type {p.result_type}")
            items.append(Item(V3, reason, names=[p.name], destination_ids=[d], detail=detail, **base))
        # V4: the component's own day text names it
        blocks = day_blocks(t.get("itinerary"))
        for c in sorted(comps, key=lambda x: (x["day_index"], x["name"])):
            text = blocks.get(int(c["day_index"]))
            if text is None:
                items.append(Item(V4, "no_day_text", day=int(c["day_index"]), names=[c["name"]],
                                  destination_ids=[str(c["destination_id"])] if c.get("destination_id") else [],
                                  **base))
            elif not day_text_names(text, c["name"]):
                items.append(Item(V4, "day_text_not_naming", day=int(c["day_index"]), names=[c["name"]],
                                  destination_ids=[str(c["destination_id"])] if c.get("destination_id") else [],
                                  **base))
    return items


def summarize(tours: list[dict], items: list[Item]) -> dict:
    """Counts per check and reason, catalog-wide and per country (tours with ≥ 1 finding, findings)."""
    def empty():
        return {"tours": 0, "tours_flagged": 0,
                "checks": {ch: {"items": 0, "tours": 0, "reasons": {r: 0 for r in REASONS[ch]}} for ch in CHECKS},
                "gate": {"reasons": list(GATE_REASONS), "items": 0, "tours": 0, "pass": True}}
    total, per = empty(), {}
    for t in tours:
        c = str(t.get("country") or "-")
        per.setdefault(c, empty())["tours"] += 1
        total["tours"] += 1
    flagged: dict[str, set] = {}
    by_check: dict[tuple, set] = {}
    for it in items:
        c = str(it.country or "-")
        for bucket in (total, per.setdefault(c, empty())):
            bucket["checks"][it.check]["items"] += 1
            bucket["checks"][it.check]["reasons"][it.reason] += 1
        flagged.setdefault(c, set()).add(it.source_id)
        by_check.setdefault((c, it.check), set()).add(it.source_id)
    for (c, ch), s in by_check.items():
        per[c]["checks"][ch]["tours"] = len(s)
    for ch in CHECKS:
        total["checks"][ch]["tours"] = len(set().union(*[s for (c, x), s in by_check.items() if x == ch] or [set()]))
    for c, s in flagged.items():
        per[c]["tours_flagged"] = len(s)
    total["tours_flagged"] = len(set().union(*flagged.values())) if flagged else 0
    blocking = [it for it in items if it.reason in GATE_REASONS]
    for bucket, sel in [(total, blocking)] + [(per[c], [it for it in blocking if str(it.country or "-") == c])
                                             for c in per]:
        bucket["gate"].update(items=len(sel), tours=len({it.source_id for it in sel}), **{"pass": not sel})
    return {"catalog": total, "by_country": dict(sorted(per.items()))}


def stats_counts(summary: dict) -> dict:
    """The compact counts stored in geo.graph_build.stats['validity'] (trend on /admin/places)."""
    cat = summary["catalog"]
    return {"tours": cat["tours"], "tours_flagged": cat["tours_flagged"], "gate.items": cat["gate"]["items"],
            **{f"{ch}.{r}": n for ch in CHECKS for r, n in cat["checks"][ch]["reasons"].items()}}


# ── DB (cis_app; AA master tenant named explicitly) ────────────────────────────────────────────────────────────────

LIVE_TOURS_SQL = """
    SELECT COALESCE(rt.source_group_id, rt.tour_id)::text AS source_id, pt.tour_id::text AS tour_id,
           pt.aa_name AS title, rt.country, rt.duration AS duration_raw, pt.aa_itineraries AS itinerary
      FROM gold_aa_internal.published_tours pt
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
     WHERE pt.tenant_id = $1::uuid
       AND pt.master_status = 'active' AND pt.deleted_at IS NULL
       AND rt.source_status = 'active' AND rt.deleted_at IS NULL
       AND ($2::text IS NULL OR rt.country = $2)
"""
COMPONENTS_SQL = """
    SELECT gp.source_id, gp.day_index, gp.name, gp.name_norm, gp.destination_id::text AS destination_id,
           d.name AS destination_name, d.country, d.lat, d.lng, d.located_by, d.geo_precision AS precision,
           d.review_reason, d.geocoder_meta ->> 'result_type' AS result_type, ps.status AS shape_status
      FROM geo.tour_place gp
      JOIN shared.destinations d ON d.id = gp.destination_id
                                AND d.geo_status <> 'suspect' AND d.duplicate_of IS NULL
      LEFT JOIN geo.place_shape ps ON ps.destination_id = d.id
     WHERE gp.source_id = ANY($1::text[])
"""


async def load(conn, country: Optional[str] = None) -> tuple[list[dict], list[dict]]:
    """Live tours (optionally one country) and their resolved components on usable destinations. Runs in its own
    transaction with app.tenant_id = AA master (store._catalog_fetch), and names the tenant in the SQL too."""
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER_TENANT_ID)
        tours = [dict(r) for r in await conn.fetch(LIVE_TOURS_SQL, MASTER_TENANT_ID, country)]
        sources = sorted({t["source_id"] for t in tours})
        comps = [dict(r) for r in await conn.fetch(COMPONENTS_SQL, sources)] if sources else []
    return tours, comps


async def report(conn, country: Optional[str] = None) -> tuple[dict, list[Item]]:
    tours, comps = await load(conn, country)
    items = evaluate(tours, comps)
    return summarize(tours, items), items


def page(items: list[Item], *, check: Optional[str] = None, reason: Optional[str] = None, page_no: int = 1,
         page_size: int = 50) -> dict:
    sel = [i for i in items if (check is None or i.check == check) and (reason is None or i.reason == reason)]
    page_no = max(1, page_no)
    start = (page_no - 1) * page_size
    return {"total": len(sel), "page": page_no, "page_size": page_size,
            "items": [i.as_dict() for i in sel[start:start + page_size]]}
