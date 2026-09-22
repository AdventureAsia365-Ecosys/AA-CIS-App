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

**One adaptation still disclosed, one closed by AA-610 (Sub 2):**
1. **`questions` now lands PAA questions by embedding-match** (`services/acp_contract/
   atom_matching.py`, `acp_contract.atom_embedding`/`atom_matches`, migration 161) — ported from
   Ms. Thư's own `_questions()`/`match_queries_to_atoms()` (aa-social-media matching.py, ADR-0002/
   0018): each PAA question is embedded (Cohere Embed v4, `services/acp_shared/
   content_embedding.py` — the model migration 041/124 assumed, Titan Embed, turns out not to be
   offered on this account at all) and landed on the nearest Atom (k=1 cosine) among the
   Segment's own member atoms, rather than a global search — a question only means anything
   landing on an atom currently in the ranking pool. Soft-fails to the ORIGINAL claim-by-name
   test (`claim_by_name_fallback()`) on any embedding failure (Bedrock outage, or an atom that
   was never embedded) — ranking must never block on a model call. Which mechanism actually
   produced a match is recorded (`atom_matches.matched_by`), not silently indistinguishable.
2. **`_about_something_else()`/`elsewhere`-refusal (score.py's off-topic-PAA suspect-claim
   check) is NOT ported** — a secondary refinement layered on top of `_demand()`, not the
   rank-sum itself, and depends on a `trips`/country-word table shape this codebase doesn't
   share. Deferred, disclosed, not silently dropped — `_demand()`/`compute_questions()` below
   read every measured row, unfiltered by that refinement.

**`said` is `SUM(LENGTH(COALESCE(tour_atoms.evidence, tour_atoms.text)))`** — AA-610 (Sub 1)
fixed what this docstring used to flag as a disclosed limitation. `evidence` (migration 160) is
the verbatim source-text span an atom was extracted from (services/acp_shared/
atom_extraction.py SYSTEM_PROMPT), not `text`'s terse `f"{place} — {action}"` join — `said` now
varies by how much an itinerary elaborates on a moment, the signal it was always meant to carry.
`text` remains the fallback for any atom read before migration 160 (NULL `evidence` until its
day is next re-atomized), so this axis never silently drops to 0 for pre-AA-610 data — just
keeps the weaker name-length signal it always had until then.

Transit/unnamed-place exclusion (ADR 0019/0020, `ranking_reference.py`) runs before ranking —
excluded Segments still get a row (per tour they touch) in `atom_ranking`, `excluded_reason`
set, every rank column NULL — "an exclusion is arguable rather than a silent absence" (score.py's
own docstring), not hidden entirely.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from services.acp_contract.atom_matching import (
    claim_by_name_fallback,
    ensure_atom_embeddings,
    land_question_on_atom,
)
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


async def invalidate_questions_cache_for_keyword(conn, keyword: str) -> int:
    """AA-630 — the write-side half of the `questions_count` cache (migration 163, AA-610 Sub
    2) that never had one: this keyword's PAA just changed (`segment_research.py::_store_paa()`
    UPDATEd `search_demand.people_also_ask`), so every Segment that would even be a CANDIDATE
    for it (same claim-by-name test `_candidate_questions()` uses — a keyword-level word-overlap
    against the Segment's own `canonical_place`/`canonical_action`) has its cached
    `questions_count` reset to NULL.

    Reuses `precompute_question_landings()`'s OWN existing NULL semantic (migration 163's own
    comment: "NULL means never computed... precompute_question_landings() always computes those
    regardless of the segment_ids scope it was given") — zero changes needed there. The next
    tour-triggered recompute that scope-includes an invalidated Segment, or the next full
    platform-wide pass (`segment_ids=None`), lands this keyword's fresh PAA for real instead of
    silently reusing a count computed before this keyword's PAA changed.

    Does NOT itself call `precompute_question_landings()` — that would re-run the (currently
    6-hour-when-unscoped, AA-610 Sub 2's own finding) full ranking pass on every single DFS PAA
    write, the same performance mistake AA-610 Sub 2 fixed. Marking stale and recomputing are
    kept as separate steps on purpose (same "đánh dấu, không tự trigger job" convention
    `services/acp_planning/models.py::needs_recompute()` already uses for N4/N5/N6 staleness).

    Returns the number of Segments invalidated (0 is the common case — most keywords are not
    claimed by any current Segment)."""
    kw_words = keyword_words(keyword)
    segment_rows = await conn.fetch("""
        SELECT segment_id, canonical_place, canonical_action
        FROM acp_contract.atom_segment
        WHERE questions_count IS NOT NULL
    """)
    stale_ids = []
    for row in segment_rows:
        claimable = claimable_words(row["canonical_place"], row["canonical_action"])
        shared = kw_words & claimable
        if shared - PLACE_KINDS:
            stale_ids.append(row["segment_id"])
    if not stale_ids:
        return 0
    await conn.execute(
        """
        UPDATE acp_contract.atom_segment
        SET questions_count = NULL
        WHERE segment_id = ANY($1::text[])
        """,
        stale_ids,
    )
    return len(stale_ids)


def _candidate_questions(
    place: str, action: str, paa_rows: list[tuple[str, str, list[str]]],
) -> set[str]:
    """The PAA questions a Segment is even a CANDIDATE for — a keyword-level pre-filter
    (claim-by-name against the Segment's own place/action) run before the real embedding-match
    landing, so `land_questions_for_segment()` only pays an embedding-distance query against
    questions that already share some claimable word, not the platform's entire bought-PAA
    pool. Still claim-by-name at this stage (same test `compute_demand()` uses) — AA-610's
    embedding-match (below) decides which SEGMENT among the shortlist a question actually lands
    on, this decides which questions are even in the running for THIS Segment's atoms."""
    claimable = claimable_words(place, action)
    seen: set[str] = set()
    for keyword, _market, questions in paa_rows:
        shared = keyword_words(keyword) & claimable
        if not shared - PLACE_KINDS:
            continue
        seen.update(questions)
    return seen


# AA-610 (Sub 2 redesign) — a hard cap on how many shortlist candidates one Segment's PAA
# landing will actually pay an embedding-distance query for. The first live test found
# _candidate_questions()'s keyword-level shortlist can be large for a Segment whose place/action
# words are common (shared with many bought keywords' own PAA) — uncapped, that Segment alone
# can dominate a whole ranking run's wall-clock time. Sorted by length (below) before capping:
# a longer question shares more real words with the Segment's own claimable set, the same "more
# specific wins" principle compute_demand()'s own fit-then-volume tie-break already uses,
# so the candidates trimmed are disproportionately the short, generic, weakly-relevant ones.
_MAX_QUESTION_CANDIDATES_PER_SEGMENT = 20


async def land_questions_for_segment(
    conn, place: str, action: str, atom_ids: list[str], paa_rows: list[tuple[str, str, list[str]]],
) -> int:
    """AA-610 (Sub 2) — how many distinct PAA questions this Segment claims, now by
    embedding-match (`services/acp_contract/atom_matching.py`) instead of claim-by-name alone.
    For each candidate question (`_candidate_questions()`'s keyword-level shortlist, capped at
    `_MAX_QUESTION_CANDIDATES_PER_SEGMENT` — see that constant's own comment), embeds it (via
    `atom_matching`'s question-embedding cache — a real Cohere Embed v4 call only the first time
    any Segment/run ever asks about that exact question text) and finds the nearest of THIS
    Segment's own atoms (`atom_matching.land_question_on_atom()`, restricted to `atom_ids` — a
    question landing on some OTHER Segment's atom does not count here); records the match
    (`acp_contract.atom_matches`) and counts it. Falls back to the original claim-by-name test
    per-question (`claim_by_name_fallback()`) whenever the embedding path can't produce a
    landing (Bedrock failure, or this Segment's atoms were never embedded) — every candidate
    question the shortlist found still gets a chance to count, never silently dropped just
    because the model call failed."""
    candidates = _candidate_questions(place, action, paa_rows)
    if not candidates or not atom_ids:
        return 0
    if len(candidates) > _MAX_QUESTION_CANDIDATES_PER_SEGMENT:
        candidates = set(
            sorted(candidates, key=len, reverse=True)[:_MAX_QUESTION_CANDIDATES_PER_SEGMENT],
        )

    atom_rows = await conn.fetch(
        "SELECT atom_id, place, action FROM acp_contract.tour_atoms WHERE atom_id = ANY($1::text[])",
        atom_ids,
    )
    for row in atom_rows:
        await ensure_atom_embeddings(conn, row["atom_id"], row["place"] or "", row["action"] or "")
    name_candidates = [(r["atom_id"], r["place"] or "", r["action"] or "") for r in atom_rows]

    claimed: set[str] = set()
    for question in candidates:
        atom_id, distance, matched_by = await land_question_on_atom(conn, question, atom_ids)
        if atom_id is None:
            atom_id = claim_by_name_fallback(question, name_candidates)
            distance, matched_by = None, "tokens"
        if atom_id is None:
            continue
        claimed.add(question)
        await conn.execute(
            """
            INSERT INTO acp_contract.atom_matches (query, kind, atom_id, distance, matched_by)
            VALUES ($1, 'question', $2, $3, $4)
            ON CONFLICT (query, atom_id) DO UPDATE SET
                distance = excluded.distance, matched_by = excluded.matched_by, matched_at = now()
            """,
            question, atom_id, distance if distance is not None else 0.0, matched_by,
        )
    return len(claimed)


async def precompute_question_landings(
    pool, segment_ids: set[str] | None = None,
) -> dict[str, int]:
    """AA-610 (Sub 2 redesign, then Sub 2 scope fix) — lands PAA questions on every
    currently-ranked Segment's atoms, independent of market (`questions` does not vary by
    market — a PAA question landing on a Segment's atom has nothing to do with which of the 6
    finite buyer markets `run_atom_ranking()` happens to be scoring right now). Its result
    (segment_id -> questions count) is passed into every `run_atom_ranking(market, pool,
    question_counts)` call so none of them re-lands a single question — and, because
    `run_atom_ranking()` rebuilds `atom_ranking` for the WHOLE platform every call, this
    function's returned dict must always have an entry for EVERY currently-ranked Segment, not
    just the ones actually recomputed this call (see `segment_ids` below).

    `segment_ids` (AA-610 Sub 2 scope fix) — a live re-test of the redesign above (still
    platform-wide on every call) found a single tour-triggered recompute
    (`recompute_segment_score_route()`, one PATCH or one atomize run) taking OVER 6 HOURS
    without completing, because it re-landed PAA questions for every OTHER platform Segment too
    — real, correct work, just none of it caused by the tour that triggered this call. Passing
    the segment_ids of ONLY that tour's own Segments here (`recompute_segment_score_route()`
    now does) recomputes `land_questions_for_segment()` for just those, and reads
    `atom_segment.questions_count` (migration 163) back for every OTHER Segment instead of
    recomputing it — a Segment whose own tours did not change gets the same count it already
    had, not silently zeroed (that would be a correctness regression, not just a performance
    one: `run_atom_ranking()` writes 0 for the `questions` axis of a Segment absent from the
    returned dict).

    `segment_ids=None` (the default) keeps the ORIGINAL platform-wide behavior — every Segment
    recomputed, no cache read — for any caller that genuinely needs a from-scratch pass (a
    backfill, or a future scheduled full re-check of `questions_count` staleness).

    A Segment with `questions_count IS NULL` (never computed — brand new) is ALWAYS recomputed
    this call regardless of `segment_ids`, never served a cache value that does not exist yet."""
    async with pool.acquire() as conn:
        segment_rows = await conn.fetch("""
            SELECT asg.segment_id, asg.canonical_place, asg.canonical_action,
                   asg.questions_count, array_agg(DISTINCT ta.atom_id) AS atom_ids
            FROM acp_contract.atom_segment asg
            JOIN acp_contract.atom_segment_member asm ON asm.segment_id = asg.segment_id
            JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
            WHERE NOT ta.deleted AND NOT ta.is_empty_marker
            GROUP BY asg.segment_id, asg.canonical_place, asg.canonical_action,
                     asg.questions_count
        """)

        demand_rows = await conn.fetch("""
            SELECT keyword, market, people_also_ask
            FROM acp_contract.search_demand WHERE search_volume IS NOT NULL
        """)
        paa_tuples: list[tuple[str, str, list[str]]] = []
        for r in demand_rows:
            paa = r["people_also_ask"]
            if isinstance(paa, str):
                paa = json.loads(paa) if paa else []
            paa_tuples.append((r["keyword"], r["market"], paa or []))

        counts: dict[str, int] = {}
        recomputed_ids: list[str] = []
        for row in segment_rows:
            place, action = row["canonical_place"], row["canonical_action"]
            if classify_exclusion(place, action):
                continue
            segment_id = row["segment_id"]
            cached = row["questions_count"]
            in_scope = segment_ids is None or segment_id in segment_ids or cached is None
            if not in_scope:
                counts[segment_id] = cached
                continue
            atom_ids = [str(a) for a in row["atom_ids"]]
            count = await land_questions_for_segment(conn, place, action, atom_ids, paa_tuples)
            counts[segment_id] = count
            recomputed_ids.append(segment_id)

        if recomputed_ids:
            await conn.executemany(
                """
                UPDATE acp_contract.atom_segment
                SET questions_count = $2, questions_computed_at = now()
                WHERE segment_id = $1
                """,
                [(segment_id, counts[segment_id]) for segment_id in recomputed_ids],
            )
    return counts


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

async def run_atom_ranking(market: str, pool, question_counts: dict[str, int]) -> dict:
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

    `question_counts` (AA-610 Sub 2 redesign) — a Segment's PAA-landing count, precomputed ONCE
    for the whole platform by `precompute_question_landings()` and passed in here rather than
    recomputed per market — `questions` does not vary by market (see that function's own
    docstring), so the first live test's finding that this real embedding-matching work was
    being redone 6x per atomize run (once per `DFS_LOCATION_MAP` entry) is closed by never
    calling the landing logic from inside this per-market function at all anymore. A
    segment_id absent from `question_counts` (should not happen for anything currently in
    `atom_segment` — `precompute_question_landings()` reads the identical Segment set — but
    defended anyway) counts as 0 questions, not a crash."""
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
            SELECT keyword, market, search_volume
            FROM acp_contract.search_demand WHERE search_volume IS NOT NULL
        """)
        demand_tuples: list[tuple[str, str, int]] = [
            (r["keyword"], r["market"], r["search_volume"]) for r in demand_rows
        ]

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
                questions=question_counts.get(row["segment_id"], 0),
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
    "rank_segments", "compute_demand", "land_questions_for_segment", "classify_exclusion",
    "run_atom_ranking",
]
