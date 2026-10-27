"""AA-847 — content-validity report of the live catalog's places: deterministic, no model call.

A pre-flip gate for TripPlanner's PLACES_SOURCE=cis (ADR 0007 decision 10) and a standing check after every
tour_graph_build (its counts land in geo.graph_build.stats['validity'], so /admin/places shows a trend). Every item
carries a REASON code from a fixed taxonomy (below) so the counts are comparable run to run; the same-place rule (V2)
is services/places/dedupe.decide (the one decision the place_dedupe job, the in-tour dedupe and this report share),
pinned by the labelled pair sets in tests/unit/test_aa847_dedupe.py and tests/unit/test_aa847_validity.py, with false
merges measured (lesson S245 / AA-779: a wrong merge is the costly error).

Checks (per live tour; the v_tour liveness rule on the base tables, read as cis_app with the AA master tenant named
explicitly — cis_app's RLS policies are USING (true)):

  V1 duration — the tour's day count disagrees with its duration:
     ranged_heading          a "Day 5-6 —" heading covering several days with one block (informational). Since
                             AA-990 (S253) every reader applies one rule (day_parse.day_range): the canonical parser
                             (itinerary_utils, catalog._tour_day_text from CIS 289), days.py and this report give
                             each day 5..6 the block's text; before, the canonical parser folded it into day 4
     missing_day_numbers     canonical "Day N —" days (a valid range counted as every day it covers) are not 1..max
     duration_text_vs_days   fewer itinerary days than duration_days (the v_tour rule: the duration text first,
                             else the highest canonical day)
     extra_days              more itinerary days than duration_days
  V2 same place repeated within one tour:
     same_destination_names  one destination under ≥ 2 component spellings (name_norm)
     near_duplicate          two destinations the same-place rules merge (services/places/dedupe.py, auto_merge) that
                             are not merged yet — the place_dedupe job clears them (AA-847 Problem 2)
     possible_duplicate      two destinations the rules leave to the two-model check (dedupe llm band) and that are
                             not merged: informational, nobody acts on it (owner decision S246: no manual merges)
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

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from services.content_generation import day_parse
from services.places import days as days_mod
from services.places import dedupe, geomatch

MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"

V1, V2, V3, V4 = "duration", "same_place", "not_a_pin", "day_text"
CHECKS = (V1, V2, V3, V4)
REASONS = {
    V1: ("ranged_heading", "missing_day_numbers", "duration_text_vs_days", "extra_days"),
    V2: ("same_destination_names", "near_duplicate", "possible_duplicate"),
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

_DURATION_RE = re.compile(r"(\d+)\s*-?\s*days?")


# ── V1 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def duration_days(duration_raw: Optional[str], itinerary: Optional[str]) -> Optional[int]:
    """catalog.v_tour.duration_days: the number in the duration text ('14 Days'), else the highest canonical day."""
    m = _DURATION_RE.search((duration_raw or "").lower())
    if m:
        return int(m.group(1))
    days = canonical_days(itinerary)
    return max(days) if days else None


def canonical_days(itinerary: Optional[str]) -> list[int]:
    """Canonical day numbers ("Day N —", a "Day A-B —" block counting as A..B), the parse_canonical_itinerary_days /
    catalog._tour_day_text / v_tour rule (day_parse.canonical_blocks)."""
    return sorted({n for b in day_parse.canonical_blocks(itinerary or "") for n in b.days})


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


def km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    return dedupe.km(lat1, lng1, lat2, lng2)


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
    place_id: Optional[str] = None       # geocoder_place_id
    geocoder_name: Optional[str] = None  # geocoder_meta ->> 'name'
    geo_status: Optional[str] = None


def to_row(p: Place) -> dedupe.Row:
    """The dedupe row of a report place (no shape geometry here: two lines are never auto-merged by the report)."""
    return dedupe.Row(p.destination_id, p.name, p.country or "", float(p.lat), float(p.lng),
                      located_by=p.located_by, geo_status=p.geo_status, precision=p.precision,
                      result_type=p.result_type, geocoder_name=p.geocoder_name, place_id=p.place_id,
                      components=p.components)


def pair_decision(a: Place, b: Place) -> dedupe.Decision:
    """AA-847 Problem 2: the one same-place decision (services/places/dedupe.decide), rules band."""
    return dedupe.decide(to_row(a), to_row(b))


def is_same_place(a: Place, b: Place) -> tuple[bool, Optional[float]]:
    """(same, km): the rules alone say one place (dedupe auto_merge). The pairs the rules send to the two-model check
    are possible_duplicate, never near_duplicate."""
    if a.destination_id == b.destination_id:
        return True, 0.0
    dec = pair_decision(a, b)
    return dec.decision == dedupe.AUTO, dec.km


def keeper_of(places: Iterable[Place]) -> Place:
    """The cluster keeper by the dedupe order (manual, line, existing keeper, Geoapify POI, settlement, card,
    components, age, id)."""
    places = list(places)
    by_id = {p.destination_id: p for p in places}
    return by_id[dedupe.keeper_of([to_row(p) for p in places]).id]


def clusters(places: list[Place]) -> list[list[Place]]:
    """Groups of ≥ 2 distinct destinations the rules merge ([keeper, member, …]): dedupe.plan_country, rules band —
    the same keeper, no chaining and the group-level clash rule (lesson S239) as the place_dedupe job."""
    by_id = {p.destination_id: p for p in places}
    plan = dedupe.plan_country([to_row(p) for p in places], rules_only=True)
    groups: dict[str, list[Place]] = {}
    for m in plan.merges:
        groups.setdefault(m["keeper_id"], [by_id[m["keeper_id"]]]).append(by_id[m["merged_id"]])
    return [groups[k] for k in sorted(groups)]


def possible_pairs(places: list[Place]) -> list[tuple[Place, Place, dedupe.Decision]]:
    """Pairs the rules leave to the two-model check (dedupe llm band): informational possible_duplicate."""
    out = []
    ps = sorted(places, key=lambda p: p.destination_id)
    for i, a in enumerate(ps):
        for b in ps[i + 1:]:
            dec = pair_decision(a, b)
            if dec.decision == dedupe.LLM:
                out.append((a, b, dec))
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
    """day -> 'title\\nbody' of the canonical blocks (catalog._tour_day_text: a repeated day keeps the last block; a
    "Day A-B —" block is the text of each day A..B)."""
    out = {}
    for b in day_parse.canonical_blocks(itinerary or ""):
        for n in b.days:
            out[n] = f"{b.title}\n{b.body}"
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
                                           c.get("shape_status"), c.get("place_id"), c.get("geocoder_name"),
                                           c.get("geo_status")))
        grouped: set = set()
        for group in clusters(list(places.values())):
            k = group[0]
            dist = max(is_same_place(k, p)[1] or 0.0 for p in group[1:])
            items.append(Item(V2, "near_duplicate", names=[p.name for p in group],
                              destination_ids=[p.destination_id for p in group], keeper_id=k.destination_id,
                              km=dist, **base))
            grouped.update(p.destination_id for p in group)
        for a, b, dec in possible_pairs(list(places.values())):
            if a.destination_id in grouped and b.destination_id in grouped:
                continue
            items.append(Item(V2, "possible_duplicate", names=[a.name, b.name],
                              destination_ids=[a.destination_id, b.destination_id], km=dec.km, detail=dec.reason,
                              **base))
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
           d.review_reason, d.geocoder_meta ->> 'result_type' AS result_type, ps.status AS shape_status,
           d.geocoder_place_id AS place_id, d.geocoder_meta ->> 'name' AS geocoder_name, d.geo_status
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
