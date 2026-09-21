"""services/acp_contract/atom_ranking.py — AA-515, the rank-sum stage.

Ported (adapted where AA-CIS genuinely has no equivalent — disclosed below) from Ms. Thư's
aa-social-media `src/aa_social/stages/score.py`. Full evidence: `docs/claude_audit/AA-515-
step0-ranking-investigation.md`, `AA-515-step0b-demand-research-loop.md`,
`AA-515-step0c-multimarket-schema.md`.

**The unit ranked is the Segment**, per ADR 0014 (rank-sum, no weights, no tuning constant) —
4 axes, each a competition rank (1, 2, 2, 4 — ties share a rank), summed, lowest total wins:
demand (one specific market per run — see AA-545 below), recurrence (distinct tours a Segment
spans, now platform-wide, not single-tenant — AA-545 made Segment itself platform-wide, so this
axis measures a real-world moment's recurrence across the WHOLE catalog, restoring what AA-509's
original per-tenant Segment scoping had suppressed), questions (People Also Ask landing on the
Segment), said (how much the itineraries describe the moment).

**AA-545 — platform-wide, one full rank-sum pass per finite market, no more per-tenant "best
market kept" merge.** `run_atom_ranking(market, pool)` computes ranks over the WHOLE platform
Segment pool for exactly ONE market per call (looped once per market in
`services/seo_intelligence/seed_builder.py::DFS_LOCATION_MAP`, triggered at A3 alongside Segment
— see `services/export/handler.py`). Previously (AA-515) one call took a TENANT's whole
`target_market` list and picked, per Segment, whichever single market gave the lowest total
(`rank_segments(candidates, markets)`'s old best-of-N loop) — that merge is GONE from this module
entirely; a tenant with several target markets now reads/merges across the matching
`(market, tour_id, segment_id)` rows at READ TIME instead (`services/acp_shared/slate.py`), the
same principle Route's own read-time `AVG(total_rank)` already applies (AA-545 Q3).

**`_demand()` reads the `search_demand` cache by NAME (word-overlap), never embedding-match**
— a deliberate choice this build keeps, not re-litigates (STEP0b Q1: the reference repo
measured the embedding matcher under-reaching from 14% to 45% of Segments carrying any demand
at all when switched to name-matching).

**Two adaptations, disclosed, not silent:**
1. **`questions` also uses word-overlap claim-by-name**, not embedding-match. Ms. Thư's own
   `_questions()` lands PAA questions on a Segment via `atom_matches` (built by
   `match_queries_to_atoms()`, an embedding-based landing step this codebase has no equivalent
   infrastructure for — no `atom_matches` table, no `Embedder`). Rather than build a parallel
   embedding-matching subsystem for one axis, this port extends the SAME claim-by-name test
   `_demand()` already uses (and the build prompt already mandated for demand) to the PAA
   questions carried alongside each bought keyword in `search_demand.people_also_ask` — a
   Segment claims a keyword's PAA the same way it claims that keyword's volume. Consistent with
   the demand decision, not a second, different mechanism.
2. **`said` is `SUM(LENGTH(COALESCE(tour_atoms.evidence, tour_atoms.text)))`** — AA-610 fixed
   what this docstring used to flag as a disclosed limitation. `evidence` (migration 160) is
   the verbatim source-text span an atom was extracted from (services/acp_shared/
   atom_extraction.py SYSTEM_PROMPT), not `text`'s terse `f"{place} — {action}"` join —
   `said` now varies by how much an itinerary elaborates on a moment, the signal it was always
   meant to carry. `text` remains the fallback for any atom read before migration 160 (NULL
   `evidence` until its day is next re-atomized), so this axis never silently drops to 0 for
   pre-AA-610 data — just keeps the weaker name-length signal it always had until then.
3. **`_about_something_else()`/`elsewhere`-refusal (score.py's off-topic-PAA suspect-claim
   check) is NOT ported** — a secondary refinement layered on top of `_demand()`, not the
   rank-sum itself, and depends on a `trips`/country-word table shape this codebase doesn't
   share. Deferred, disclosed, not silently dropped — `_demand()`/`compute_questions()` below
   read every measured row, unfiltered by that refinement.

Transit/unnamed-place exclusion (ADR 0019/0020, `ranking_reference.py`) runs before ranking —
excluded Segments still get a row (per tour they touch) in `atom_ranking`, `excluded_reason`
set, every rank column NULL — "an exclusion is arguable rather than a silent absence" (score.py's
own docstring), not hidden entirely.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from services.acp_contract.ranking_reference import (
    PLACE_KINDS,
    claimable_words,
    is_transit,
    keyword_words,
    names_somewhere,
)


@dataclass(frozen=True)
class Candidate:
    """One Segment and the four things it is ranked on."""

    segment_id: str
    place: str
    action: str
    tour_ids: tuple[str, ...]
    recurrence: int
    questions: int
    said: int
    demand: dict[str, int]


@dataclass(frozen=True)
class RankedSegment:
    segment_id: str
    tour_ids: tuple[str, ...]
    demand_rank: int
    recurrence_rank: int
    questions_rank: int
    said_rank: int
    total_rank: int
    demand_market: str | None
    demand_volume: int | None
    recurrence: int
    questions: int
    said: int


@dataclass(frozen=True)
class ExcludedSegment:
    segment_id: str
    tour_ids: tuple[str, ...]
    reason: str  # 'transit' | 'unnamed_place'


def _competition_ranks(
    candidates: Sequence[Candidate], of: Callable[[Candidate], int],
) -> dict[str, int]:
    """Best first: 1, 2, 2, 4. Equal values take equal ranks — a straight sum would otherwise
    encode the order rows happened to arrive in."""
    ordered = sorted(candidates, key=lambda c: -of(c))
    ranks: dict[str, int] = {}
    previous = None
    place = 0
    for position, candidate in enumerate(ordered, start=1):
        value = of(candidate)
        if value != previous:
            place = position
            previous = value
        ranks[candidate.segment_id] = place
    return ranks


def _demand_ranks(candidates: Sequence[Candidate], market: str) -> dict[str, int]:
    """Rank on demand for one market, with a Segment nothing was measured for taking the
    MEDIAN of the measured — "known unsearched is worse than unknown, but absence of evidence
    is not evidence of absence" (score.py's own reasoning, ported verbatim in spirit)."""
    measured = [c for c in candidates if market in c.demand]
    if not measured:
        return {c.segment_id: 1 for c in candidates}
    ranks = _competition_ranks(measured, lambda c: c.demand[market])
    middle = sorted(ranks.values())[len(ranks) // 2]
    return {c.segment_id: ranks.get(c.segment_id, middle) for c in candidates}


def rank_segments(candidates: list[Candidate], market: str) -> list[RankedSegment]:
    """Rank-sum every candidate for ONE market. No weights, no tuning constant — a straight sum
    of 4 competition ranks, lowest wins (ADR 0014).

    AA-545 — no more best-of-N-market loop: `run_atom_ranking()` now calls this once per finite
    platform market (looped by its caller), so there is exactly one `market` to score against,
    not a tenant's list to pick the best of. The old best-of-N merge (Ms. Thư's own `rank()`
    never had this either — it was AA-515's own addition for a per-tenant multi-market Segment
    row) now happens at READ TIME instead, across the resulting per-market rows
    (`services/acp_shared/slate.py`).
    """
    if not candidates:
        return []

    recurrence_rank = _competition_ranks(candidates, lambda c: c.recurrence)
    questions_rank = _competition_ranks(candidates, lambda c: c.questions)
    said_rank = _competition_ranks(candidates, lambda c: c.said)
    demand_rank = _demand_ranks(candidates, market)

    ranked = []
    for candidate in candidates:
        total = (
            demand_rank[candidate.segment_id] + recurrence_rank[candidate.segment_id]
            + questions_rank[candidate.segment_id] + said_rank[candidate.segment_id]
        )
        ranked.append(RankedSegment(
            segment_id=candidate.segment_id, tour_ids=candidate.tour_ids,
            demand_rank=demand_rank[candidate.segment_id],
            recurrence_rank=recurrence_rank[candidate.segment_id],
            questions_rank=questions_rank[candidate.segment_id],
            said_rank=said_rank[candidate.segment_id], total_rank=total,
            demand_market=market, demand_volume=candidate.demand.get(market),
            recurrence=candidate.recurrence, questions=candidate.questions, said=candidate.said,
        ))
    return sorted(ranked, key=lambda r: (r.total_rank, r.segment_id))


def compute_demand(
    place: str, action: str, demand_rows: list[tuple[str, str, int]],
) -> dict[str, int]:
    """Port of score.py's `_demand()` — the strongest keyword a Segment owns, per market, by
    NAME (word-overlap), never embedding-match. `demand_rows` = every (keyword, market, volume)
    row in `search_demand` with a non-null volume, loaded once per ranking run (not re-queried
    per Segment, unlike the reference repo's own per-call SQLite query — Postgres round-trips
    are not free the way a local SQLite file's are; same output, cheaper)."""
    claimable = claimable_words(place, action)
    best: dict[str, tuple[int, int]] = {}
    for keyword, market, volume in demand_rows:
        shared = keyword_words(keyword) & claimable
        if not shared - PLACE_KINDS:
            continue
        fit = len(shared)
        held = best.get(market)
        if held is None or (fit, volume) > held:
            best[market] = (fit, volume)
    return {market: volume for market, (_fit, volume) in best.items()}


def compute_questions(
    place: str, action: str, paa_rows: list[tuple[str, str, list[str]]],
) -> int:
    """How many distinct People Also Ask questions this Segment claims, by the SAME
    claim-by-name test `compute_demand()` uses (docstring item 1 — no embedding-match
    infrastructure exists in this codebase to port `_questions()`'s real mechanism)."""
    claimable = claimable_words(place, action)
    seen: set[str] = set()
    for keyword, _market, questions in paa_rows:
        shared = keyword_words(keyword) & claimable
        if not shared - PLACE_KINDS:
            continue
        seen.update(questions)
    return len(seen)


def classify_exclusion(place: str, action: str) -> str | None:
    """'transit' | 'unnamed_place' | None — the 2 exclusion classes ranking is never applied to
    (ADR 0019/0020), checked in this order because a transit action ("arrive at the trailhead")
    is excluded for what it DOES regardless of whether its place also fails to name somewhere."""
    if is_transit(action):
        return "transit"
    if not names_somewhere(place):
        return "unnamed_place"
    return None


# ── DB-facing wrapper (impure) ──────────────────────────────────────────────────────────────

async def run_atom_ranking(market: str, pool) -> dict:
    """Rebuild atom_ranking WHOLE for one market, platform-wide (DELETE+INSERT) — matches the
    AA-510 STEP0 finding that Ms. Thư's own `routes`/`atom_scores` are "derived, never
    accumulated"; no downstream table has an FK into this one yet expecting stability across
    re-runs (AA-545 build confirmed this still holds: neither Route/Hub nor Slate FK a specific
    `atom_ranking` row).

    AA-545 — recomputes over the WHOLE PLATFORM Segment set (every tenant, every tour), for
    exactly this one `market`, not one tenant's Segment set across several markets. Triggered at
    A3, once per `services/seo_intelligence/seed_builder.py::DFS_LOCATION_MAP` market, right
    after `run_segment_matching()` (`services/export/handler.py`) — not per-tenant-rewrite
    anymore.
    """
    async with pool.acquire() as conn:
        segment_rows = await conn.fetch("""
            SELECT asg.segment_id, asg.canonical_place, asg.canonical_action,
                   array_agg(DISTINCT ta.tour_id) AS tour_ids,
                   COALESCE(SUM(LENGTH(COALESCE(ta.evidence, ta.text, ''))), 0) AS said
            FROM acp_contract.atom_segment asg
            JOIN acp_contract.atom_segment_member asm ON asm.segment_id = asg.segment_id
            JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
            WHERE NOT ta.deleted AND NOT ta.is_empty_marker
            GROUP BY asg.segment_id, asg.canonical_place, asg.canonical_action
        """)

        demand_rows = await conn.fetch("""
            SELECT keyword, market, search_volume, people_also_ask
            FROM acp_contract.search_demand WHERE search_volume IS NOT NULL
        """)

    demand_tuples: list[tuple[str, str, int]] = []
    paa_tuples: list[tuple[str, str, list[str]]] = []
    for r in demand_rows:
        demand_tuples.append((r["keyword"], r["market"], r["search_volume"]))
        paa = r["people_also_ask"]
        if isinstance(paa, str):
            paa = json.loads(paa) if paa else []
        paa_tuples.append((r["keyword"], r["market"], paa or []))

    included: list[Candidate] = []
    excluded: list[ExcludedSegment] = []
    for row in segment_rows:
        place, action = row["canonical_place"], row["canonical_action"]
        tour_ids = tuple(str(t) for t in row["tour_ids"])
        reason = classify_exclusion(place, action)
        if reason:
            excluded.append(ExcludedSegment(row["segment_id"], tour_ids, reason))
            continue
        included.append(Candidate(
            segment_id=row["segment_id"], place=place, action=action, tour_ids=tour_ids,
            recurrence=len(tour_ids),
            questions=compute_questions(place, action, paa_tuples),
            said=row["said"],
            demand=compute_demand(place, action, demand_tuples),
        ))

    ranked = rank_segments(included, market)

    ranked_row_count = sum(len(r.tour_ids) for r in ranked)
    excluded_row_count = sum(len(e.tour_ids) for e in excluded)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM acp_contract.atom_ranking WHERE market = $1", market,
            )
            if ranked:
                await conn.executemany("""
                    INSERT INTO acp_contract.atom_ranking
                        (market, tour_id, segment_id, demand_rank, recurrence_rank,
                         questions_rank, said_rank, total_rank, demand_market, demand_volume,
                         recurrence, questions, said, excluded_reason)
                    VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                            NULL)
                """, [
                    (market, tour_id, r.segment_id, r.demand_rank, r.recurrence_rank,
                     r.questions_rank, r.said_rank, r.total_rank, r.demand_market,
                     r.demand_volume, r.recurrence, r.questions, r.said)
                    for r in ranked for tour_id in r.tour_ids
                ])
            if excluded:
                await conn.executemany("""
                    INSERT INTO acp_contract.atom_ranking
                        (market, tour_id, segment_id, recurrence, questions, said,
                         excluded_reason)
                    VALUES ($1, $2::uuid, $3, 0, 0, 0, $4)
                """, [
                    (market, tour_id, e.segment_id, e.reason)
                    for e in excluded for tour_id in e.tour_ids
                ])

    return {
        "segments_ranked": len(ranked), "segments_excluded": len(excluded),
        "rows_written": ranked_row_count + excluded_row_count,
    }


__all__ = [
    "Candidate", "RankedSegment", "ExcludedSegment",
    "rank_segments", "compute_demand", "compute_questions", "classify_exclusion",
    "run_atom_ranking",
]
