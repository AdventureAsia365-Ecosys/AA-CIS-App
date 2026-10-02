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

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import structlog

from services.acp_contract.atom_matching import (
    claim_by_name_fallback,
    embed_questions_cached,
    ensure_atom_embeddings_batch,
    land_question_on_atom,
)
from services.acp_contract.ranking_reference import (
    PLACE_KINDS,
    claimable_words,
    is_transit,
    keyword_words,
    names_somewhere,
)
from shared.llm_client.decide import decide


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


def demand_candidates(
    place: str, action: str, demand_rows: list[tuple[str, str, int]], market: str,
) -> list[tuple[str, int]]:
    """AA-694 A3-6 — every keyword this Segment could claim for one market, best first by the same
    (fit, volume) order `compute_demand()` uses. `compute_demand()` returns the head of this list."""
    claimable = claimable_words(place, action)
    scored = []
    for keyword, kw_market, volume in demand_rows:
        if kw_market != market:
            continue
        shared = keyword_words(keyword) & claimable
        if not shared - PLACE_KINDS:
            continue
        scored.append((len(shared), volume, keyword))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [(keyword, volume) for _fit, volume, keyword in scored]


# AA-694 A3-6 — the demand-ownership Jev Question (migration 188). One shared word lets a Segment claim
# a keyword's whole volume ("Kyoto" 165k → "Kyoto incense-making"). Ask about the best candidate; a
# confident no (enforce only) moves to the next one, up to DEMAND_MAX_TRIES. The Verdict does not depend
# on the market, so the decide() cache makes the other markets free.
DEMAND_STAGE = "a3_demand"
DEMAND_Q = "a3_demand_belongs"
DEMAND_MAX_TRIES = 3
DEMAND_CONCURRENCY = 8


async def resolve_demand(pool, segments: list[tuple[str, str, str]], demand_rows, market: str) -> dict[str, dict]:
    """segment_id -> {market: volume} (empty when nothing is claimed or every tried keyword is
    rejected). `segments` = (segment_id, place, action)."""
    sem = asyncio.Semaphore(DEMAND_CONCURRENCY)
    rejected_count = 0

    async def _one(segment_id: str, place: str, action: str):
        nonlocal rejected_count
        moment = landing_moment(place, action)
        for keyword, volume in demand_candidates(place, action, demand_rows, market)[:DEMAND_MAX_TRIES]:
            async with sem:
                dec = await decide(DEMAND_STAGE, f"demand:{moment[:200]}:{keyword[:200]}",
                                   {"keyword": keyword, "moment": moment}, [DEMAND_Q], pool=pool)
            if not dec.rejected(DEMAND_Q):
                return segment_id, {market: volume}
            rejected_count += 1
        return segment_id, {}

    out = dict(await asyncio.gather(*[_one(*s) for s in segments]))
    logger.info("demand_ownership", market=market, segments=len(segments), rejected=rejected_count,
                claimed=sum(1 for v in out.values() if v))
    return out


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


async def invalidate_contested_cache_for_keyword(conn, keyword: str) -> int:
    """AA-631 (Debate `contested` standard) — same write-side invalidation shape as
    `invalidate_questions_cache_for_keyword()` (AA-630) above, for the `contested` cache
    (migration 165) instead of `questions_count`. Called from `segment_research.py::_store_
    serp_domains()` right after this keyword's `serp_domains` changes — every Segment that
    would even be a candidate for this keyword (same claim-by-name test) has its cached
    `contested` reset to NULL, picked up by the next tour-triggered recompute (never triggers
    one itself, same "đánh dấu, không tự trigger job" reasoning AA-630 already established)."""
    kw_words = keyword_words(keyword)
    segment_rows = await conn.fetch("""
        SELECT segment_id, canonical_place, canonical_action
        FROM acp_contract.atom_segment
        WHERE contested IS NOT NULL
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
        SET contested = NULL
        WHERE segment_id = ANY($1::text[])
        """,
        stale_ids,
    )
    return len(stale_ids)


# AA-631 (Debate standard #1, "contested") — domains this build treats as "claims everything":
# an aggregator/reference site (Wikipedia, TripAdvisor, Lonely Planet, etc) that ranks for
# nearly EVERY travel keyword regardless of how specific the place/action is, not because it
# wrote something distinctive about THIS keyword. A Segment whose claimed keyword's SERP is
# dominated by these domains is one "every operator has already written this" — Ms. Thư's own
# origin standard #1 (see AA-631 issue). This list is deliberately short and travel-specific
# rather than a generic "top sites" list — the origin's own `everywhere_domains()` is computed
# from the platform's OWN harvested SERP history (which domains appear across nearly every
# keyword THIS platform has actually bought), not a hardcoded list; ported here as a starting
# seed set (real domains observed dominating travel SERPs) that `everywhere_domains()` below
# still recomputes from real platform data, not hardcoded scoring — see its own docstring.
_KNOWN_AGGREGATOR_DOMAINS: frozenset[str] = frozenset({
    "wikipedia.org", "en.wikipedia.org", "tripadvisor.com", "lonelyplanet.com",
    "booking.com", "expedia.com", "getyourguide.com", "viator.com", "britannica.com",
})


def everywhere_domains(all_serp_domains: list[list[str]], *, threshold: float = 0.5) -> set[str]:
    """AA-631 — platform-wide (per AA-545's own precedent for Segment/Route: computed over the
    WHOLE harvested pool, not per-tenant) set of domains that appear in at least `threshold`
    fraction of this platform's OWN harvested SERPs — Ms. Thư's origin `everywhere_domains()`,
    ported as "recomputed from real data" rather than "hardcoded list" (see
    `_KNOWN_AGGREGATOR_DOMAINS` above, which seeds `compute_contested()`'s fallback when a
    platform has too little harvested SERP history yet for this frequency count to mean
    anything — see `compute_contested()`).

    `all_serp_domains` — one list per harvested (keyword, market) row's `serp_domains` (ranked,
    may contain duplicates within one list — deduped to a set per-row here before counting, so
    a domain ranking twice in ONE keyword's SERP does not inflate its cross-keyword frequency).
    Empty input -> empty set (not `_KNOWN_AGGREGATOR_DOMAINS` — that fallback is
    `compute_contested()`'s own decision to make when it has nothing else, not this function's).
    """
    if not all_serp_domains:
        return set()
    counts: dict[str, int] = {}
    for domains in all_serp_domains:
        for domain in set(domains):
            counts[domain] = counts.get(domain, 0) + 1
    total = len(all_serp_domains)
    return {domain for domain, count in counts.items() if count / total >= threshold}


def compute_contested(serp_domains: list[str], everywhere: set[str]) -> float:
    """AA-631 — the `contested` score itself: fraction of a keyword's ranked organic results
    that belong to an `everywhere`-set domain (platform-computed via `everywhere_domains()`, or
    `_KNOWN_AGGREGATOR_DOMAINS` when the platform has too little harvested history for that
    computation to mean anything yet — a cold-start fallback, not a permanent hardcode; the
    live threshold decision itself, made against real measured data before this ships, is
    tracked separately from this pure function).

    Pure, deterministic, no LLM — per the issue's own scope boundary against AA-610's rank-sum
    axes ("rank-sum, deterministic, KHÔNG LLM"). 0.0 for an empty `serp_domains` (never
    harvested, or a SERP with zero organic results — not treated as "fully contested", the
    opposite of what an empty signal should mean) rather than raising or returning 1.0.
    """
    if not serp_domains:
        return 0.0
    claimed = sum(1 for domain in serp_domains if domain in everywhere)
    return claimed / len(serp_domains)


# AA-631 — measured against 30 REAL keywords already in acp_contract.search_demand (live DFS
# calls, cost ~$0.06, 22/09/2026) before this shipped, not invented. everywhere_domains()'s own
# 0.5 default already reflects the same measurement (see its docstring); this is the SEPARATE
# decision of where `contested` itself should cut a Segment.
#
# At everywhere_domains(threshold=0.5) (2 domains found: en.wikipedia.org, www.tripadvisor.com
# — exactly the "claims everything" global sites the standard is meant to catch, NOT the
# region-specific travel blogs that dominated at lower thresholds, e.g. discoverlaos.today,
# migrationology.com, travelfish.org, which are real but Laos-cluster-specific, not globally
# "everywhere"), the sample's contested scores were: min=0.00, median=0.12, mean=0.16,
# max=0.33 (histogram: 17 keywords in [0.0,0.2), 13 in [0.2,0.4), ZERO at or above 0.4). A
# 30-keyword, single-region (Laos) sample is not enough evidence to set a number that could
# cut real Segments on a still-thin platform history — 0.5 is chosen deliberately ABOVE the
# entire observed range (nothing in this sample would be cut), a conservative floor rather than
# a number fit to this one sample. Revisit once contested has accumulated real history across
# more markets/regions (same "revisit later" precedent CHANNEL_BARS/DFS_LOCATION_MAP's own
# additions already follow in this codebase).
CONTESTED_CUT_THRESHOLD = 0.5


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


def _capped_candidates(
    place: str, action: str, paa_rows: list[tuple[str, str, list[str]]],
) -> set[str]:
    """`_candidate_questions()` trimmed to `_MAX_QUESTION_CANDIDATES_PER_SEGMENT` (see that
    constant's own comment). Shared by the landing itself and AA-688's embedding pre-pass, so the
    pre-pass embeds exactly the questions the landing will ask about."""
    candidates = _candidate_questions(place, action, paa_rows)
    if len(candidates) > _MAX_QUESTION_CANDIDATES_PER_SEGMENT:
        candidates = set(
            sorted(candidates, key=len, reverse=True)[:_MAX_QUESTION_CANDIDATES_PER_SEGMENT],
        )
    return candidates


async def land_questions_for_segment(
    conn, place: str, action: str, atom_ids: list[str], paa_rows: list[tuple[str, str, list[str]]],
    question_vectors: dict[str, list[float]] | None = None, atoms_embedded: bool = False,
    candidates: set[str] | None = None,
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
    because the model call failed.

    AA-688 — embeddings are fetched in batches, not one paced call per text.
    `precompute_question_landings()` embeds every segment's atoms and questions up front and
    passes `question_vectors` + `atoms_embedded=True`; a direct caller that passes neither gets
    the same batching for this one Segment. A question absent from `question_vectors` (its
    embedding call failed) goes straight to claim-by-name."""
    # AA-694 — `candidates`, when given, is the shortlist already filtered by the Jev gates (country
    # scope, landing); recomputing it here let a dropped question count again through the
    # claim-by-name fallback (it has no vector, so it fell straight to that path).
    if candidates is None:
        candidates = _capped_candidates(place, action, paa_rows)
    if not candidates or not atom_ids:
        return 0

    atom_rows = await conn.fetch(
        "SELECT atom_id, place, action FROM acp_contract.tour_atoms WHERE atom_id = ANY($1::text[])",
        atom_ids,
    )
    name_candidates = [(r["atom_id"], r["place"] or "", r["action"] or "") for r in atom_rows]
    if not atoms_embedded:
        await ensure_atom_embeddings_batch(conn, name_candidates)
    if question_vectors is None:
        question_vectors = await embed_questions_cached(conn, candidates)

    claimed: set[str] = set()
    for question in candidates:
        vector = question_vectors.get(question)
        if vector is not None:
            atom_id, distance, matched_by = await land_question_on_atom(
                conn, question, atom_ids, vector=vector,
            )
        else:
            atom_id, distance, matched_by = None, None, "tokens"
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


# AA-694 — the two country-scope Jev Questions (migration 185). Both are asked in one call about one
# state; a question stops counting when it confidently names somewhere outside the Segment's tour
# countries, or confidently names nothing particular to them ("What do Buddhists eat?"). The scope is
# the Segment's own tour countries (Q4), stored in the subject key, so a Segment that gains a country
# is asked again. Only enforce-mode, confident Verdicts drop anything (ADR 0007).
SCOPE_STAGE = "a3_question_scope"
SCOPE_FOREIGN_Q = "a3_question_foreign"
SCOPE_HERE_Q = "a3_question_about_here"
SCOPE_CONCURRENCY = 4


def scope_dropped(decision) -> bool:
    """True when the question must stop counting for this country set."""
    return decision.accepted(SCOPE_FOREIGN_Q) or decision.rejected(SCOPE_HERE_Q)


logger = structlog.get_logger()


async def filter_candidates_by_country_scope(pool, rows, segment_candidates: dict[str, set[str]]) -> dict:
    """Removes out-of-scope questions from `segment_candidates` in place. One Verdict per distinct
    (country set, question) — the decide() cache makes a repeat pair free. Segments with no known
    country are left alone. Returns counts for the log."""
    pairs: dict[tuple[str, str], None] = {}
    seg_scope: dict[str, str] = {}
    for row in rows:
        countries = sorted({c for c in (row.get("countries") or []) if c})
        if not countries:
            continue
        scope = ", ".join(countries)
        seg_scope[row["segment_id"]] = scope
        for q in segment_candidates.get(row["segment_id"], ()):
            pairs[(scope, q)] = None
    if not pairs:
        return {"scope_pairs": 0, "scope_dropped": 0}
    sem = asyncio.Semaphore(SCOPE_CONCURRENCY)

    async def _one(scope: str, q: str):
        async with sem:
            dec = await decide(SCOPE_STAGE, f"paa:{scope}:{q[:300]}", {"question": q, "countries": scope},
                               [SCOPE_FOREIGN_Q, SCOPE_HERE_Q], pool=pool)
        return scope, q, scope_dropped(dec)

    dropped = {(scope, q) for scope, q, drop in await asyncio.gather(*[_one(*p) for p in pairs]) if drop}
    removed = 0
    for segment_id, scope in seg_scope.items():
        before = segment_candidates.get(segment_id, set())
        after = {q for q in before if (scope, q) not in dropped}
        removed += len(before) - len(after)
        segment_candidates[segment_id] = after
    logger.info("question_country_scope", pairs=len(pairs), dropped_pairs=len(dropped), removed=removed)
    return {"scope_pairs": len(pairs), "scope_dropped": len(dropped)}


# AA-694 (A3-5) — the landing Jev Question (migration 187). Every atom of a Segment shares its
# canonical place + action, so "would the asker be served by an article about this moment" is asked
# once per (moment, question) before landing, like the country scope above. Only an enforce-mode,
# confident no removes the question (ADR 0007); Q5: shadow until a Calibration Record exists.
LANDING_STAGE = "a3_question_landing"
LANDING_Q = "a3_landing_belongs"
LANDING_CONCURRENCY = 8


def landing_moment(place: str, action: str) -> str:
    return f"{place} — {action}" if action else place


async def filter_candidates_by_landing(pool, rows, segment_candidates: dict[str, set[str]]) -> dict:
    """Removes questions Jev confidently says do not belong to the Segment's moment, in place. One
    Verdict per distinct (moment, question) — Segments sharing a moment share it, and the decide()
    cache makes a repeat pair free. Returns counts for the log."""
    pairs: dict[tuple[str, str], None] = {}
    seg_moment: dict[str, str] = {}
    for row in rows:
        moment = landing_moment(row["canonical_place"] or "", row["canonical_action"] or "")
        seg_moment[row["segment_id"]] = moment
        for q in segment_candidates.get(row["segment_id"], ()):
            pairs[(moment, q)] = None
    if not pairs:
        return {"landing_pairs": 0, "landing_rejected": 0}
    sem = asyncio.Semaphore(LANDING_CONCURRENCY)

    async def _one(moment: str, q: str):
        async with sem:
            dec = await decide(LANDING_STAGE, f"land:{moment[:200]}:{q[:300]}", {"query": q, "moment": moment},
                               [LANDING_Q], pool=pool)
        return moment, q, dec.rejected(LANDING_Q)

    rejected = {(m, q) for m, q, rej in await asyncio.gather(*[_one(*p) for p in pairs]) if rej}
    removed = 0
    for segment_id, moment in seg_moment.items():
        before = segment_candidates.get(segment_id, set())
        after = {q for q in before if (moment, q) not in rejected}
        removed += len(before) - len(after)
        segment_candidates[segment_id] = after
    logger.info("question_landing_belongs", pairs=len(pairs), rejected_pairs=len(rejected), removed=removed)
    return {"landing_pairs": len(pairs), "landing_rejected": len(rejected)}


async def precompute_question_landings(
    pool, segment_ids: set[str] | None = None,
    progress: Callable[[dict], None] | None = None,
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
    this call regardless of `segment_ids`, never served a cache value that does not exist yet.

    AA-688 — before landing, one pre-pass embeds every in-scope Segment's missing atom views and
    uncached candidate questions in batches of 96 (`ensure_atom_embeddings_batch()` /
    `embed_questions_cached()`). Before this, each text was its own paced 3.5 s call: the first
    a3_atomize after the data reset had ~16,000 atom views + ~1,500 questions to embed (every
    NULL-count Segment is in scope), about 16 hours. `progress`, when given, receives
    `{"step", "done", "total"}` dicts (a sync callback — the A3 job forwards them to the Jobs
    page)."""
    async with pool.acquire() as conn:
        segment_rows = await conn.fetch("""
            SELECT asg.segment_id, asg.canonical_place, asg.canonical_action,
                   asg.questions_count, array_agg(DISTINCT ta.atom_id) AS atom_ids,
                   array_remove(array_agg(DISTINCT rt.country), NULL) AS countries,
                   array_agg(ta.activity_type) AS activity_types
            FROM acp_contract.atom_segment asg
            JOIN acp_contract.atom_segment_member asm ON asm.segment_id = asg.segment_id
            JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = asm.atom_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
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
        in_scope_rows = []
        exclusions = await resolve_exclusions(pool, segment_rows)   # AA-694 A3-2
        for row in segment_rows:
            if exclusions[row["segment_id"]]:
                continue
            segment_id = row["segment_id"]
            cached = row["questions_count"]
            if segment_ids is None or segment_id in segment_ids or cached is None:
                in_scope_rows.append(row)
            else:
                counts[segment_id] = cached

        # AA-688 pre-pass — embed everything the landing below will need, in batches.
        segment_candidates = {
            row["segment_id"]: _capped_candidates(row["canonical_place"], row["canonical_action"],
                                                  paa_tuples)
            for row in in_scope_rows
        }
        # AA-694 — drop questions about another country, or about no country in particular, before
        # they can land and count (Ms. Thư's ticket 04; decisions Q3/Q4 of 30/09/2026).
        await filter_candidates_by_country_scope(pool, in_scope_rows, segment_candidates)
        # AA-694 A3-5 — then drop questions that do not belong to the Segment's moment (ticket 05).
        await filter_candidates_by_landing(pool, in_scope_rows, segment_candidates)
        landing_rows = [r for r in in_scope_rows if segment_candidates[r["segment_id"]]]
        atom_ids_needed = sorted({str(a) for r in landing_rows for a in r["atom_ids"]})
        question_vectors: dict[str, list[float]] = {}
        if atom_ids_needed:
            atom_rows = await conn.fetch(
                "SELECT atom_id, place, action FROM acp_contract.tour_atoms "
                "WHERE atom_id = ANY($1::text[])",
                atom_ids_needed,
            )
            await ensure_atom_embeddings_batch(
                conn, [(r["atom_id"], r["place"] or "", r["action"] or "") for r in atom_rows],
                progress,
            )
            all_questions = {q for r in landing_rows for q in segment_candidates[r["segment_id"]]}
            question_vectors = await embed_questions_cached(conn, all_questions, progress)

        recomputed_ids: list[str] = []
        for done, row in enumerate(in_scope_rows, start=1):
            segment_id = row["segment_id"]
            atom_ids = [str(a) for a in row["atom_ids"]]
            counts[segment_id] = await land_questions_for_segment(
                conn, row["canonical_place"], row["canonical_action"], atom_ids, paa_tuples,
                question_vectors=question_vectors, atoms_embedded=True,
                candidates=segment_candidates[segment_id],
            )
            recomputed_ids.append(segment_id)
            if progress and (done % 100 == 0 or done == len(in_scope_rows)):
                progress({"step": "landing_questions", "done": done, "total": len(in_scope_rows)})

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


# AA-694 A3-2 — two independent opinions on "is this moment transit": the verb rule above and the
# `activity_type` atomize gave the Segment's atoms. Where they disagree (192 transit-typed atoms the
# rule misses, S204), Jev picks; the pick is used only when enforced and confident (ADR 0007).
TYPE_STAGE = "a3_segment_type"
TYPE_Q = "a3_activity_type"
TYPE_CONCURRENCY = 8


def atoms_say_transit(activity_types) -> bool:
    """True when most of the Segment's typed atoms are `transit` (untyped atoms do not vote)."""
    typed = [t for t in (activity_types or []) if t]
    return bool(typed) and sum(t == "transit" for t in typed) * 2 > len(typed)


def exclusion_after_pick(place: str, rule_reason: str | None, pick: str | None) -> str | None:
    """classify_exclusion()'s answer with Jev's transit/experience pick applied (None pick = keep it)."""
    if pick == "transit":
        return "transit"
    if pick == "experience" and rule_reason == "transit":
        return None if names_somewhere(place) else "unnamed_place"
    return rule_reason


async def resolve_exclusions(pool, rows) -> dict[str, str | None]:
    """segment_id -> exclusion reason, for rows with segment_id/canonical_place/canonical_action and
    `activity_types` (the member atoms' types). Jev is asked only where the rule and the atoms
    disagree (a Segment with no typed atom has no second opinion); one Verdict per distinct moment,
    cached by decide()."""
    out: dict[str, str | None] = {}
    disputed: dict[str, list[str]] = {}
    for row in rows:
        place, action = row["canonical_place"] or "", row["canonical_action"] or ""
        reason = classify_exclusion(place, action)
        out[row["segment_id"]] = reason
        types = [t for t in (row.get("activity_types") or []) if t]
        if types and reason != "unnamed_place" and (reason == "transit") != atoms_say_transit(types):
            disputed.setdefault(landing_moment(place, action), []).append(row["segment_id"])
    if not disputed:
        return out
    sem = asyncio.Semaphore(TYPE_CONCURRENCY)

    async def _one(moment: str):
        async with sem:
            dec = await decide(TYPE_STAGE, f"type:{moment[:300]}", {"moment": moment}, [TYPE_Q], pool=pool)
        return moment, dec.choice(TYPE_Q)

    changed = 0
    by_id = {r["segment_id"]: r for r in rows}
    for moment, pick in await asyncio.gather(*[_one(m) for m in disputed]):
        for segment_id in disputed[moment]:
            new = exclusion_after_pick(by_id[segment_id]["canonical_place"] or "", out[segment_id], pick)
            changed += new != out[segment_id]
            out[segment_id] = new
    logger.info("segment_activity_type", disputed_moments=len(disputed), changed=changed)
    return out


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
                   COALESCE(SUM(LENGTH(COALESCE(ta.evidence, ta.text, ''))), 0) AS said,
                   array_agg(ta.activity_type) AS activity_types
            FROM acp_contract.atom_segment asg
            JOIN acp_contract.atom_segment_member asm ON asm.segment_id = asg.segment_id
            JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = asm.atom_id
            GROUP BY asg.segment_id, asg.canonical_place, asg.canonical_action
        """)

        demand_rows = await conn.fetch("""
            SELECT keyword, market, search_volume
            FROM acp_contract.search_demand WHERE search_volume IS NOT NULL
        """)
        demand_tuples: list[tuple[str, str, int]] = [
            (r["keyword"], r["market"], r["search_volume"]) for r in demand_rows
        ]

    # AA-694 A3-6 — demand per Segment for this market, through the ownership gate (no connection held
    # across the Jev calls).
    exclusions = await resolve_exclusions(pool, segment_rows)   # AA-694 A3-2
    rankable = [(r["segment_id"], r["canonical_place"], r["canonical_action"]) for r in segment_rows
                if not exclusions[r["segment_id"]]]
    demand_by_segment = await resolve_demand(pool, rankable, demand_tuples, market)

    included: list[Candidate] = []
    excluded: list[ExcludedSegment] = []
    for row in segment_rows:
        place, action = row["canonical_place"], row["canonical_action"]
        tour_ids = tuple(str(t) for t in row["tour_ids"])
        reason = exclusions[row["segment_id"]]
        if reason:
            excluded.append(ExcludedSegment(row["segment_id"], tour_ids, reason))
            continue
        included.append(Candidate(
            segment_id=row["segment_id"], place=place, action=action, tour_ids=tour_ids,
            recurrence=len(tour_ids),
            questions=question_counts.get(row["segment_id"], 0),
            said=row["said"],
            demand=demand_by_segment.get(row["segment_id"], {}),
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
    "rank_segments", "compute_demand", "demand_candidates", "resolve_demand", "land_questions_for_segment",
    "classify_exclusion", "resolve_exclusions",
    "run_atom_ranking",
]
