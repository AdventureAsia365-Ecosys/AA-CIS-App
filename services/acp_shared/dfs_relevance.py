"""
services.acp_shared.dfs_relevance — AA-448, tour-level DataForSEO search-demand signal.

ADR-2026-038 §0.4 (per AA-445-01's citation): `dfs_relevance` was a SEPARATE axis from the
former atom-level `distinctiveness` signal (removed end-to-end in AA-754 together with the
services/acp_shared/competitor_index.py module). `dfs_relevance` is TOUR-level, from real search
demand (`silver_aa_internal.seo_context.keyword_ideas[].search_volume`), used to filter/prioritize
TOURS at T1 (browse pool — not wired by this task) and T7 (quarter plan) — never attached to an
individual atom (§0.4 point 2).

Confirmed via grep before writing this file: `dfs_relevance` had ZERO hits anywhere in this
repo before this task (AA-445-01/AA-448-00 STEP0 both independently found this) — this is a
new module, not a port of existing code.

Thresholds are explicitly uncalibrated (§0.4 point 6, "chưa hiệu chỉnh bằng data thật" —
AA-439-05 separately found the newest real seo_context row had entirely null search_volume) —
kept as a dataclass of named constants, not hardcoded inline, so a follow-up ticket can tune
them without touching any call site (matches this task's own "config được, không hardcode"
instruction).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Optional
from uuid import UUID

Relevance = Literal["HIGH", "MED", "LOW"]

# 3-bucket HIGH/MED/LOW shape, kept as its own Literal. (It once mirrored the former
# atom-level distinctiveness bucket type; that signal and its module were removed in AA-754, so
# this is now simply the DFS-relevance scale.)


@dataclass(frozen=True)
class DfsRelevanceThresholds:
    """§0.4's own tentative numbers: LOW < 50/mo, MED 50-500/mo, HIGH > 500/mo. `low_max` is the
    boundary between LOW and MED (exclusive on the low side, i.e. volume < low_max -> LOW);
    `high_min` is the boundary between MED and HIGH (inclusive, i.e. volume >= high_min ->
    HIGH)."""
    low_max: int = 50
    high_min: int = 500


_DEFAULT_THRESHOLDS = DfsRelevanceThresholds()


def score_dfs_relevance(
    search_volumes: list[Optional[int]],
    thresholds: DfsRelevanceThresholds = _DEFAULT_THRESHOLDS,
) -> Relevance:
    """Pure. `search_volumes` = every non-None `search_volume` value already extracted from one
    tour's latest `seo_context.keyword_ideas` row (see `fetch_dfs_relevance_by_tour` below for
    the DB-wiring side that builds this list per tour).

    Uses the MAX of the tour's keyword ideas — the tour's single best real-demand keyword
    opportunity — not an average, which a handful of genuinely-zero-volume long-tail ideas
    (routine in a 25-idea/tour list, AA-197's own cap) would silently drag toward LOW even when
    one strong keyword exists.

    Empty input -> MED. This covers BOTH real cases the task's null-handling requirement names:
    (a) no seo_context row exists yet for this tour (T2 DFS was never run against it), and
    (b) a row exists but every one of its keyword_ideas has search_volume=None (a real, live
    case AA-439-05 already found — DataForSEO can return volume=null per keyword). MED is an
    honest "no signal either way" default — not LOW, which would silently punish a tour AA
    simply hasn't run DataForSEO against yet."""
    if not search_volumes:
        return "MED"
    best = max(search_volumes)
    if best < thresholds.low_max:
        return "LOW"
    if best >= thresholds.high_min:
        return "HIGH"
    return "MED"


_SEO_CONTEXT_LATEST_QUERY = """
    SELECT DISTINCT ON (tour_id) tour_id, keyword_ideas
    FROM silver_aa_internal.seo_context
    WHERE tour_id = ANY($1::uuid[])
    ORDER BY tour_id, fetched_at DESC
"""

_SEO_CONTEXT_SINGLE_TOUR_QUERY = """
    SELECT keyword_ideas, people_also_ask, related_keywords
    FROM silver_aa_internal.seo_context
    WHERE tour_id = $1
    ORDER BY fetched_at DESC
    LIMIT 1
"""


async def fetch_dfs_relevance_by_tour(
    tour_ids: list[UUID], pool, thresholds: DfsRelevanceThresholds = _DEFAULT_THRESHOLDS,
) -> dict[UUID, Relevance]:
    """DB-wiring wrapper — ONE bulk query for every tour_id in `tour_ids` (not per-trip N+1;
    same bulk convention `fetch_atoms_by_trip()` already uses for atoms). Reads each tour's
    LATEST seo_context row (`DISTINCT ON (tour_id) ... ORDER BY fetched_at DESC` — a tour can
    have more than one row across repair-round re-fetches; "latest wins" matches
    `fetch_current_version_no()`'s own convention elsewhere in this codebase, quarter.py:427).

    Tours with no seo_context row at all are simply ABSENT from the returned dict (same
    "absent means empty, not zero" convention `atoms_by_trip.get(trip.id, [])` already
    establishes) — callers must `.get(trip.id, score_dfs_relevance([]))` i.e. default missing
    keys to `"MED"` themselves; this function does not silently insert every input tour_id with
    a MED placeholder, since that would hide a genuinely-empty result set from a caller that
    wants to distinguish "no tours passed in" from "every tour scored MED"."""
    if not tour_ids:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SEO_CONTEXT_LATEST_QUERY, tour_ids)
    out: dict[UUID, Relevance] = {}
    for r in rows:
        ideas = r["keyword_ideas"]
        if isinstance(ideas, str):
            ideas = json.loads(ideas) if ideas else []
        volumes = [
            i.get("search_volume") for i in (ideas or [])
            if isinstance(i, dict) and i.get("search_volume") is not None
        ]
        out[r["tour_id"]] = score_dfs_relevance(volumes, thresholds)
    return out


@dataclass(frozen=True)
class SearchDemandSignal:
    """AA-469 Việc 4 — a single-tour bundle of real search-demand data (DFS relevance + the raw
    People-Also-Ask questions + related search terms, migration 069/AA-218), meant for T8's
    angle-generation prompt (services/acp_angle_gate/prompts.py) — a DIFFERENT consumer than
    `fetch_dfs_relevance_by_tour()` above (T7's quarter-plan scoring, which only ever needed the
    3-bucket `Relevance` value, never the raw PAA/keyword text). Kept as its own function/type
    rather than widening `fetch_dfs_relevance_by_tour()`'s bulk-by-list shape, since T8 always
    resolves exactly one tour per request (the atom's own trip_id) — a single-row query matches
    `_fetch_atom_for_tenant()`'s existing single-item convention in
    services/acp_angle_gate/service.py, not a wasted bulk round trip for N=1."""
    relevance: Relevance
    people_also_ask: list[str]
    related_keywords: list[str]


async def fetch_search_demand_signal(
    tour_id: UUID, pool, thresholds: DfsRelevanceThresholds = _DEFAULT_THRESHOLDS,
) -> Optional[SearchDemandSignal]:
    """Returns None when this tour has no seo_context row at all (T2 DFS never run against it) —
    same "absent means no signal, not a zero" convention `fetch_dfs_relevance_by_tour()` already
    uses; callers must treat None as "omit this block from the prompt", not as an empty-but-real
    signal. `people_also_ask`/`related_keywords` are plain JSONB string arrays (migration 069) —
    no per-item shape to unpack, unlike `keyword_ideas`."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SEO_CONTEXT_SINGLE_TOUR_QUERY, tour_id)
    if row is None:
        return None

    def _as_list(value) -> list[str]:
        if isinstance(value, str):
            value = json.loads(value) if value else []
        return [v for v in (value or []) if isinstance(v, str)]

    ideas = row["keyword_ideas"]
    if isinstance(ideas, str):
        ideas = json.loads(ideas) if ideas else []
    volumes = [
        i.get("search_volume") for i in (ideas or [])
        if isinstance(i, dict) and i.get("search_volume") is not None
    ]
    return SearchDemandSignal(
        relevance=score_dfs_relevance(volumes, thresholds),
        people_also_ask=_as_list(row["people_also_ask"]),
        related_keywords=_as_list(row["related_keywords"]),
    )


__all__ = [
    "Relevance", "DfsRelevanceThresholds", "score_dfs_relevance",
    "fetch_dfs_relevance_by_tour", "SearchDemandSignal", "fetch_search_demand_signal",
]
