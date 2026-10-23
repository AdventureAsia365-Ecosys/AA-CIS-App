"""
services.acp_planning.runway — N4 Runway Map (1x/year + invalidation).

Ported from aamc/planning.py's runway_map()/D1 (aa-marketing-v2 research
build). Content peaks in the BOOKING window, not the travel season. Pure
Python, $0 LLM — see compute_runway_map() for the fully testable core.

AA-516 (03/09/2026): the async `fetch_trips()`/`runway_map()` DB-wiring wrappers this docstring
used to describe were deleted — Slate (T7, AA-511) fully replaced the admin-triggered N4-N6
quarter-plan flow they backed; nothing live calls them anymore (services/acp_produce/
slot_runner.py's own docstring already documented deliberately never calling this layer). T7/T8
now read trips through `tenant_pool.py` (AA-448) and call `compute_runway_map()` directly.
`compute_runway_map()` itself is untouched — still the live, tested, pure core.

Fixes applied during the port (see docs/implementation-notes/AA-301.md):
  B9  — family detection scanned trip.summary (aamc.Trip), which does not
        exist for unpublished tours in this schema (v_trip_registry only
        exposes pt.aa_summary, NULL pre-publish). Scans itinerary_source
        (rt.src_itineraries) instead — always populated from S0 ingest.
  B9  — destination-level lead-time offset used trips[0] as a "representative"
        trip. Now computed per-trip and aggregated (min lo, max hi) across
        every trip at that destination.
  B11 — MOFU band was placed entirely above the market's own hi, but a
        destination's achievable "distance to nearest season" is capped by
        its season-spacing geometry and can be <= hi for common cases (2
        seasons ~6 months apart, long_haul hi=6) — MOFU became unreachable.
        Redesigned band: BOFU keeps the inner half of [lo, hi] (closest to
        travel), MOFU takes the outer half of [lo, hi] plus one band-width
        beyond hi, TOFU extends further. See _stage_for_dist().
"""
from __future__ import annotations

import re
from typing import Optional
from uuid import UUID

from .constants import LONG_HAUL_MARKETS, RUNWAY_OFFSETS_MONTHS
from .models import RunwayCell, RunwayMap, Trip, compute_trips_hash

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

_FAMILY_RE = re.compile(r"famil", re.IGNORECASE)
_BOFU_INNER_FRACTION = 0.5  # B11 — see module docstring

_DAY_RE = re.compile(r"(\d+)\s*days?\b", re.IGNORECASE)
_HALF_DAY_RE = re.compile(r"half\s*day", re.IGNORECASE)
_DAY_TRIP_RE = re.compile(r"day\s*trip|same\s*day", re.IGNORECASE)
_OVERNIGHT_RE = re.compile(r"overnight", re.IGNORECASE)
_HOUR_RE = re.compile(r"\d+(\.\d+)?\s*hours?", re.IGNORECASE)
_MIN_RE = re.compile(r"\d+\s*min", re.IGNORECASE)


def parse_duration_days(duration_raw: Optional[str]) -> Optional[int]:
    """Parser for raw_tours.duration free text. Built from a live survey of
    749 non-null v_trip_registry.duration_raw rows (aa_internal tenant,
    2026-07-22) — not guessed from 1-2 examples. Observed shapes: '# days',
    '# DAYS', '# day(s) # night(s)' (with/without newline), 'HALF DAY',
    '# hours', 'DAY TRIP'/'same day', 'OVERNIGHT TRIP', '# MIN.', and several
    1-off variants (ranges like '4-5 hours', 'Total time: X hours/days').
    Returns None when unparseable — never fabricates a number."""
    if not duration_raw:
        return None
    s = duration_raw.strip()
    if not s:
        return None
    m = _DAY_RE.search(s)
    if m:
        return int(m.group(1))
    if _HALF_DAY_RE.search(s):
        return 0
    if _DAY_TRIP_RE.search(s):
        return 0
    if _OVERNIGHT_RE.search(s):
        return 1
    if _HOUR_RE.search(s) or _MIN_RE.search(s):
        return 0
    return None


def parse_period(period: Optional[str]) -> list[int]:
    """'Mar-May,Sep-Nov' -> [3,4,5,9,10,11]. 'May-Oct' -> [5..10]."""
    months: list[int] = []
    for span in (period or "").split(","):
        m = re.match(r"\s*([A-Za-z]{3})[a-z]*\s*-\s*([A-Za-z]{3})", span.strip())
        if not m:
            single = re.match(r"\s*([A-Za-z]{3})", span.strip())
            if single and single.group(1).lower() in MONTHS:
                months.append(MONTHS[single.group(1).lower()])
            continue
        a, b = MONTHS.get(m.group(1).lower()), MONTHS.get(m.group(2).lower())
        if not a or not b:
            continue
        cur = a
        while True:
            months.append(cur)
            if cur == b:
                break
            cur = cur % 12 + 1
    return sorted(set(months))


def _family_detected(itinerary_source: Optional[str]) -> bool:
    """B9 fix — scans itinerary_source (rt.src_itineraries, always populated
    from S0 ingest), not summary (pt.aa_summary — NULL pre-publish)."""
    if not itinerary_source:
        return False
    return bool(_FAMILY_RE.search(itinerary_source))


def _offset_for(market: str, trip: Trip) -> tuple[float, float]:
    fam = _family_detected(trip.itinerary_source) or (parse_duration_days(trip.duration_raw) or 0) >= 14
    if market.upper() in LONG_HAUL_MARKETS:
        return RUNWAY_OFFSETS_MONTHS["family_extended" if fam else "long_haul"]
    return RUNWAY_OFFSETS_MONTHS["short_haul"]


def _offsets_for_destination(market: str, trips: list[Trip]) -> tuple[float, float]:
    """B9 fix — offset computed per-trip and aggregated (min lo, max hi)
    across every trip at the destination, not a single 'representative'
    trips[0] (which silently mis-classified whole destinations, e.g. one
    family-worded Mongolia trip's 6-12mo offset applied to all its trips)."""
    los, his = [], []
    for t in trips:
        lo, hi = _offset_for(market, t)
        los.append(lo)
        his.append(hi)
    return min(los), max(his)


def _stage_for_dist(dist: float, lo: float, hi: float) -> str:
    """B11 fix — see module docstring for the root-cause analysis. BOFU keeps
    the inner half of [lo, hi] (closest to actual travel), MOFU takes the
    outer half of [lo, hi] plus one more band-width beyond hi, TOFU extends
    two band-widths further, else OFF. dist < lo keeps the original
    'in/near season, capture for next cycle' TOFU fallback."""
    band_width = max(hi - lo, 0.5)
    bofu_hi = lo + band_width * _BOFU_INNER_FRACTION
    mofu_hi = bofu_hi + band_width
    tofu_hi = mofu_hi + band_width * 2
    if dist < lo:
        return "TOFU"
    if dist <= bofu_hi:
        return "BOFU"
    if dist <= mofu_hi:
        return "MOFU"
    if dist <= tofu_hi:
        return "TOFU"
    return "OFF"


def dead_trip_url_alarms(trips: list[Trip]) -> list[str]:
    """Trip ids with a CONFIRMED dead trip_url (url_alive is False). Trips
    with url_alive=None (acp_deliver.tenant_tour_pages has no row for them —
    the table is empty today, migration 078) are NOT alarmed: absence of
    data is not evidence of a dead link. Safe no-op while the table is
    empty, matches issue requirement exactly."""
    return [str(t.id) for t in trips if t.url_alive is False]


def compute_runway_map(tenant_id: UUID, year: int, trips: list[Trip], markets: list[str]) -> RunwayMap:
    """Pure computation — no DB, no LLM, 100% unit-testable."""
    unknowns: list[str] = []
    dests: dict[str, list[Trip]] = {}
    for t in trips:
        if not t.served:   # AA-986 L3 (ADR 0015 Q9): the served state, not lifecycle_stage
            unknowns.append(f"Trip '{t.name}' is no longer served by AA ({t.served_state}) — excluded from runway map.")
            continue
        if not t.period:
            unknowns.append(
                f"Trip '{t.name}' has no PERIOD — excluded from runway map (seasonal timing content off).")
            continue
        dests.setdefault(t.destination or t.name, []).append(t)

    cells: list[RunwayCell] = []
    for dest, dtrips in dests.items():
        travel_months = sorted({m for t in dtrips for m in parse_period(t.period)})
        if not travel_months:
            continue
        starts = [m for m in travel_months if ((m - 2) % 12 + 1) not in travel_months] or travel_months
        for market in markets:
            lo, hi = _offsets_for_destination(market, dtrips)
            for month in range(1, 13):
                dist = min(((s - month) % 12) for s in starts)
                stage = _stage_for_dist(dist, lo, hi)
                cells.append(RunwayCell(destination=dest, market=market, month=month, stage=stage))

    return RunwayMap(
        tenant_id=tenant_id, year=year, cells=cells,
        trips_hash=compute_trips_hash(trips), unknowns=unknowns,
    )


def _row_to_trip(row) -> Trip:
    """AA-516: kept — `fetch_trips()` (the only caller inside THIS module) was deleted as dead
    N4-N6 code, but `tenant_pool.py::fetch_tenant_trips()` (AA-448, T7's live, real trip-fetch
    path) imports this exact helper directly (`from .runway import _row_to_trip`) to build Trip
    objects from its own, different query's rows. A near-miss found via a full-repo import
    grep before finalizing this cleanup — do not delete without re-checking tenant_pool.py."""
    return Trip(
        id=row["id"], name=row["name"], destination=row["destination"],
        period=row["period"], duration_raw=row["duration_raw"],
        itinerary_source=row["itinerary_source"],
        lifecycle_stage=row["lifecycle_stage"] or "active",
        trip_url=row["trip_url"], url_alive=row["url_alive"],
        served_state=row["served_state"] if "served_state" in row.keys() else None,   # AA-986 L3
    )


def recompute_trigger_note(previous_hash: Optional[str], current_trips: list[Trip]) -> Optional[str]:
    """Returns a human-readable invalidation note when N4 (and therefore
    N5/N6 downstream) is stale, else None. Marks only — never triggers a job
    itself (AA-301: 'đánh dấu invalidate, không tự trigger job')."""
    from .models import needs_recompute
    if needs_recompute(previous_hash, current_trips):
        return (f"N4/N5/N6 stale for {len(current_trips)} trips — a trip was added, "
                f"removed, or its PERIOD/lifecycle_stage changed since the last runway_map().")
    return None


__all__ = [
    "parse_duration_days", "parse_period", "dead_trip_url_alarms",
    "compute_runway_map", "recompute_trigger_note",
]
