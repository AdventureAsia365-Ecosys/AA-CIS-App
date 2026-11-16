"""services/acp_contract/research_plan.py — AA-1122 item 4: the order in which segment research buys places.

Before AA-1122 the plan sorted stale places by name and a capped run took the cheapest prefix (select_within_usd), so a
capped run starved the places late in the alphabet and every new place. The plan is now in priority order, used by
manual and automatic runs alike (select_within_usd keeps the longest prefix that fits the caps):

  1. **new** — never researched (no acp_contract.segment_research_log row): most active platform tours first, then the
     longest wait since the place's first atom;
  2. **stale** — a requested tier is stale (segment_research.stale_tiers, the one definition of stale): the oldest
     research first, then the most active tours;
  3. **ahead** (scope auto_refresh only) — fresh, but stale under the windows shortened by
     ``cis.seo.research_refresh_ahead_days``: the oldest first, at most ``cis.seo.research_refresh_max_share_pct`` of
     the places in scope per run, so a block bought in one week does not expire in one week. The run then buys through
     a research cache with the same narrowed windows (:func:`run_windows`), so a refresh always buys before it stamps
     a new log row (critic of the S282 design: a fresh store answer would otherwise stamp the place fresh for free);
  4. **requalify** — fresh places whose stored map the keyword rules would change (needs_requalify): last, at most
     ``cis.seo.research_requalify_max_places`` per run; the rest wait (counts["requalify_waiting"]) because a rebuilt
     map buys its new keywords in every requested market (critic C6).

Scope ``auto_new`` keeps bucket 1 only; ``auto_refresh`` takes 1-4; an administrator's run (scope None) takes 1, 2, 4.
$0: reads only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from services.seo_intelligence.research_cache import Windows

# The run scopes. None = an administrator's run (POST /admin/segment-research/run); these two are set by the nightly
# tick (research_auto.py) and nothing else.
SCOPES = ("auto_new", "auto_refresh")
BUCKETS = ("new", "stale", "ahead", "requalify")
ORDER = {"auto_new": ("new",), "auto_refresh": BUCKETS, None: ("new", "stale", "requalify")}

_LOG_AGE_SQL = """
    SELECT canonical_place, min(researched_at) AS oldest, max(researched_at) AS newest
      FROM acp_contract.segment_research_log
     WHERE canonical_place = ANY($1::text[]) GROUP BY canonical_place
"""


@dataclass
class PlaceInfo:
    """What the order reads about a place: its active platform tours and when its first atom was written."""
    tours: int = 0
    first_seen: object = None


@dataclass
class Selection:
    """The planned places in priority order, with the counts the preview, the panel and the result show."""
    places: list
    counts: dict


def place_info(rows: list[dict]) -> dict[str, PlaceInfo]:
    """place -> PlaceInfo from segment_research.scoped_rows (distinct tours across its Segments, the oldest atom)."""
    tours: dict[str, set] = {}
    first: dict[str, object] = {}
    for r in rows:
        place = r["canonical_place"]
        tours.setdefault(place, set()).update(t for t in (r.get("tour_ids") or []) if t)
        seen = r.get("first_seen")
        if seen is not None and (first.get(place) is None or seen < first[place]):
            first[place] = seen
    return {p: PlaceInfo(len(t), first.get(p)) for p, t in tours.items()}


async def log_ages(pool, names: list[str]) -> dict[str, tuple]:
    """place -> (oldest, newest) researched_at of its research rows; absent = never researched."""
    if not names:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_LOG_AGE_SQL, list(names))
    return {r["canonical_place"]: (r.get("oldest"), r.get("newest")) for r in rows}


def narrowed(windows: Windows, ahead_days: int) -> Windows:
    """The windows of a refresh-ahead read: each freshness window shortened by `ahead_days`, never under one day."""
    ahead = timedelta(days=max(int(ahead_days), 0))
    one = timedelta(days=1)
    return Windows(max(windows.volume - ahead, one), max(windows.serp - ahead, one), windows.recover)


def order_key(bucket: str, name: str, info: dict, ages: dict):
    """The sort key inside a bucket (module docstring). Pure."""
    i = info.get(name) or PlaceInfo()
    far = "9999"
    if bucket == "new":
        return (-i.tours, str(i.first_seen or far), name)
    if bucket in ("stale", "ahead"):
        return (str((ages.get(name) or (None, None))[0] or far), -i.tours, name)
    return (-i.tours, name)


def _bucket_of(name: str, fresh: dict, ages: dict, market_codes: list, tiers: tuple):
    """(bucket, stale markets, SERP stale) of one place under the normal windows; bucket None = fresh."""
    from services.acp_contract.segment_research import stale_tiers

    markets, serp = stale_tiers(fresh.get(name, set()), market_codes, tiers)
    if name not in ages:
        return "new", (list(market_codes) if "volume" in tiers else []), "serp" in tiers
    return ("stale" if markets or serp else None), markets, serp


async def _ahead(pool, by_place: dict, names: list[str], market_codes: list, tiers: tuple, windows: Windows) -> list:
    """The fresh places stale under the narrowed `windows`, as _Place rows of bucket ahead."""
    from services.acp_contract.segment_research import _fresh_tiers, stale_tiers
    from services.acp_contract.segment_research_batch import _Place

    early = await _fresh_tiers(pool, names, windows)
    out = []
    for name in names:
        markets, serp = stale_tiers(early.get(name, set()), market_codes, tiers)
        if markets or serp:
            out.append(_Place(name, by_place[name], markets, serp=serp))
    return out


def _attach_maps(order: list, maps: dict, market_codes: list, tiers: tuple) -> None:
    """A place reuses its stored map when the rules allow (reusable_keywords); else it is stale in every market."""
    from services.acp_contract.segment_research_batch import reusable_keywords

    for p in order:
        kws = reusable_keywords(p.name, maps.get(p.name, []))
        if kws is not None:
            p.keywords, p.has_map = kws, True
        elif "volume" in tiers:
            p.markets = list(market_codes)


async def plan_selection(pool, by_place: dict[str, list[str]], market_codes: list[str], tiers: tuple[str, ...],
                         windows=None, *, info: dict | None = None, scope: str | None = None,
                         ahead_windows: Windows | None = None, ahead_share_pct: float = 0.0,
                         requalify_max: int | None = None) -> Selection:
    """The places to research in priority order (module docstring) with their stale markets, SERP need and map."""
    from services.acp_contract.segment_research import _fresh_tiers
    from services.acp_contract.segment_research_batch import _Place, load_maps, needs_requalify

    info = info or {}
    names = sorted(by_place)
    fresh = await _fresh_tiers(pool, names, windows)
    ages = await log_ages(pool, names)
    buckets: dict[str, list] = {b: [] for b in BUCKETS}
    fresh_names: list[str] = []
    for name in names:
        bucket, markets, serp = _bucket_of(name, fresh, ages, market_codes, tiers)
        if bucket is None:
            fresh_names.append(name)
        else:
            buckets[bucket].append(_Place(name, by_place[name], markets, serp=serp))
    if scope == "auto_refresh" and ahead_windows is not None and ahead_share_pct > 0 and fresh_names:
        buckets["ahead"] = await _ahead(pool, by_place, fresh_names, market_codes, tiers, ahead_windows)
    maps = await load_maps(pool, [p.name for b in ("new", "stale", "ahead") for p in buckets[b]] + fresh_names)
    taken = {p.name for p in buckets["ahead"]}
    buckets["requalify"] = [_Place(n, by_place[n], [], serp="serp" in tiers, requalify=True) for n in fresh_names
                            if n not in taken and needs_requalify(n, maps.get(n, []))]
    counts = _sort_and_cap(buckets, info, ages, len(names), ahead_share_pct, requalify_max)
    order = [p for b in ORDER[scope] for p in buckets[b]]
    _attach_maps(order, maps, market_codes, tiers)
    return Selection(order, counts)


def _sort_and_cap(buckets: dict, info: dict, ages: dict, n_scope: int, ahead_share_pct: float,
                  requalify_max: int | None) -> dict:
    """Sort each bucket, label its places, cap the ahead share and the requalify count; return the counts."""
    for b, items in buckets.items():
        items.sort(key=lambda p, b=b: order_key(b, p.name, info, ages))
        for p in items:
            p.bucket = b
    counts = {k: len(v) for k, v in buckets.items()}
    buckets["ahead"] = buckets["ahead"][:math.floor(n_scope * max(min(ahead_share_pct, 100.0), 0.0) / 100.0)]
    limit = len(buckets["requalify"]) if requalify_max is None else max(int(requalify_max), 0)
    counts["requalify_waiting"] = max(len(buckets["requalify"]) - limit, 0)
    buckets["requalify"] = buckets["requalify"][:limit]
    counts["ahead_selected"] = len(buckets["ahead"])
    return counts


async def select_for_run(pool, by_place: dict, market_codes: list, tiers: tuple, windows: Windows, *,
                         info: dict, scope: str | None) -> tuple[Selection, Windows]:
    """(the run's selection, the windows its research cache uses), with the settings in force (research_settings)."""
    from services.seo_intelligence import research_settings as rs
    from shared import settings

    ahead_days = int(settings.get(rs.REFRESH_AHEAD_DAYS)) if scope == "auto_refresh" else 0
    ahead = narrowed(windows, min(ahead_days, windows.volume.days - 1)) if ahead_days > 0 else None
    selection = await plan_selection(
        pool, by_place, market_codes, tiers, windows, info=info, scope=scope, ahead_windows=ahead,
        ahead_share_pct=float(settings.get(rs.REFRESH_MAX_SHARE_PCT)) if ahead else 0.0,
        requalify_max=int(settings.get(rs.REQUALIFY_MAX_PLACES)))
    return selection, (ahead if ahead is not None and selection.counts.get("ahead_selected") else windows)


def bucket_counts(places: list) -> dict[str, int]:
    """bucket -> how many of `places` (the selected prefix)."""
    out: dict[str, int] = {}
    for p in places:
        out[p.bucket or "other"] = out.get(p.bucket or "other", 0) + 1
    return out


__all__ = ["SCOPES", "PlaceInfo", "Selection", "place_info", "log_ages", "narrowed", "order_key", "plan_selection",
           "select_for_run", "bucket_counts"]
