"""
services/acp_shared/slate.py — AA-511: the Slate (Weekly Slots' replacement).

Ported/adapted from Ms. Thư's aa-social-media `src/aa_social/stages/slate.py`
(`choose()`/`recommend()`) — full evidence and every deviation disclosed in
docs/claude_audit/AA-511-step0-slate-investigation.md. Read that file first for the "why", not
just this module's docstrings.

**Does not compute a NEW score formula** — still true after AA-545, though it now does real
aggregation work `atom_ranking`/`route` themselves no longer do. `subject.score` is `MIN(total_
rank)` across this tenant's own markets (Segment subjects — 7 non-Blog channels, `_fetch_segment_
candidates()`) or `AVG(total_rank)` of a Route's member Segments for its best market (Route/Blog
subjects, `_fetch_route_candidates()`, AA-510's own mean-of-total_rank formula, AA-545 Q3) —
copied into `subject.score` at propose time same as before. Before AA-545 this really was a
verbatim copy of one already-merged, per-tenant row; `atom_ranking`/`route` are now platform-wide
per-market with no merge step of their own left (AA-545 removed it from `rank_segments()` and
from `route.score` entirely), so THIS module is now where that merge happens, at read time —
disclosed explicitly (AA-545 grill round 3 caught the original draft's own claim that Slate
"already had" this logic; it didn't). Building a second, parallel scoring formula here (an
`acp_shared.segment_score` table) would still duplicate already-live AA-515 data — still not
built (STEP0 point 3a, unchanged by AA-545).

**Bar thresholds are the origin's real numbers** (`reference/channels.toml`, STEP0 Q3), not
invented, not a binary "has data" check:

    Blog        needs_demand=1000  needs_questions=3   (grain: Route)
    LinkedIn/Facebook/Instagram/TikTok   needs_said=150 (grain: Segment)
    Email/Landing Page/Ads               on_demand, no bar (needs_*=0, grain: Segment)

Bar reads `atom_ranking.demand_volume`/`questions`/`said` directly — NOT
`silver_aa_internal.seo_context` (that would re-derive a number AA-515 already computed and
persisted, and could disagree with it; STEP0 point 3a's correction to the build prompt's own
Bar section).

**`choose()`'s "one place per Channel" de-dup + `most_per_hub` hub-cap — ported (AA-511 Gap B,
post-Done follow-up, 2026-09-02).** Originally left unbuilt (the first AA-511 pass's own scope was
Bar + Score only) — see `_choose()` below for the ported mechanism and every grain difference
from the origin, disclosed there rather than here.

**Route/Blog pick now carries the whole Route (AA-511 Gap A, same follow-up)** —
`pick_subject()` persists `angle_gate_request.route_segment_ids` (migration 134) as the Route's
full `ordered_segment_ids`, not just one representative atom; T9's `start_write()`
(`services/acp_content_writing/service.py`) reads it and builds the write seed from every
Segment's text in Route order when present. See `pick_subject()`'s own docstring.

**Identity, not `hashlib.sha256(...)` like the origin's `subject_id()`.** `acp_shared.subject`
uses a random `uuid` PK (per the build prompt's own literal schema) — idempotent re-propose is
instead done via two partial unique indexes on `(tenant_id, channel, segment_id)` /
`(tenant_id, channel, route_id)` (migration 133) and an `ON CONFLICT ... DO UPDATE` that refreshes
`score`/`cleared_bar_reason` on a still-`proposed` row without ever touching one a tenant has
since `picked`/`used`/`cut` — same "a judgement already made is not made again" rule the origin's
own `_store()` implements, ported in spirit not in mechanism.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

import structlog

from services.acp_shared.audit_log import TenantAuditAction, write_audit_log

logger = structlog.get_logger()

# One row per Channel: the Bar (STEP0 Q3, literal reference/channels.toml numbers), which grain
# it reads (Blog is the one Channel scored at Route grain — AA-CIS's own extension, STEP0 Q2),
# and whether it is on-demand (no weekly rhythm, origin's derive_posting_rhythm() skips these
# entirely — STEP0 Q3's own finding: the origin has no rules-based Subject list for these at all,
# this build runs the same Bar/choose logic against them anyway, at their default-zero bar).
#
# most_per_hub (Gap B, 2026-09-02): the origin's OWN literal `reference/channels.toml` number —
# 99, "effectively off" per `choose()`'s own docstring, for every one of its 5 weekly-rhythm
# Channels (Blog/LinkedIn/Facebook/Instagram/TikTok all read 99 there, verified). The origin has
# NO number at all for Email/Landing Page/Ads — `derive_posting_rhythm()` skips on_demand
# Channels entirely, so `choose()` never runs for them there — `None` (no cap) is this build's
# own honest choice for the 3 on-demand Channels rather than inventing a number the origin never
# had; see `_choose()` for how `None` is handled.
CHANNEL_BARS: dict[str, dict] = {
    "blog":          {"needs_demand": 1000, "needs_questions": 3, "needs_said": 0,
                       "grain": "route", "on_demand": False, "most_per_hub": 99},
    "linkedin":      {"needs_demand": 0, "needs_questions": 0, "needs_said": 150,
                       "grain": "segment", "on_demand": False, "most_per_hub": 99},
    "facebook":      {"needs_demand": 0, "needs_questions": 0, "needs_said": 150,
                       "grain": "segment", "on_demand": False, "most_per_hub": 99},
    "instagram":     {"needs_demand": 0, "needs_questions": 0, "needs_said": 150,
                       "grain": "segment", "on_demand": False, "most_per_hub": 99},
    "tiktok":        {"needs_demand": 0, "needs_questions": 0, "needs_said": 150,
                       "grain": "segment", "on_demand": False, "most_per_hub": 99},
    "email":         {"needs_demand": 0, "needs_questions": 0, "needs_said": 0,
                       "grain": "segment", "on_demand": True, "most_per_hub": None},
    "landing_page":  {"needs_demand": 0, "needs_questions": 0, "needs_said": 0,
                       "grain": "segment", "on_demand": True, "most_per_hub": None},
    "ads":           {"needs_demand": 0, "needs_questions": 0, "needs_said": 0,
                       "grain": "segment", "on_demand": True, "most_per_hub": None},
}

WEEKLY_RHYTHM_CHANNELS = tuple(c for c, s in CHANNEL_BARS.items() if not s["on_demand"])
ON_DEMAND_CHANNELS = tuple(c for c, s in CHANNEL_BARS.items() if s["on_demand"])


@dataclass(frozen=True)
class Candidate:
    """One Segment or Route worth proposing to a Channel, before the Bar is applied."""

    segment_id: str | None
    route_id: str | None
    score: float
    demand: int | None
    questions: int
    said: int
    place: str | None = None
    action: str | None = None
    hub_name: str | None = None
    # Gap B — Route's own hub (None for a standalone Route; a Segment candidate's hub is instead
    # resolved separately via _fetch_segment_hub_map(), keyed by segment_id, not carried here).
    hub_id: str | None = None


def _clears_bar(channel: str, candidate: Candidate) -> tuple[bool, dict]:
    """Whether a Candidate clears one Channel's Bar, and the reason either way.

    Literal `>=` comparisons against `reference/channels.toml`'s own numbers (STEP0 Q3) —
    `choose()`'s `if (subject.demand or 0) < channel.needs_demand: continue`, inverted to a
    positive predicate plus a recorded reason (`cleared_bar_reason` is NOT NULL regardless of
    outcome — an unmet bar is as reportable as a cleared one, matching the origin's own
    "an exclusion is arguable rather than a silent absence" philosophy for transit/unnamed-place).
    """
    spec = CHANNEL_BARS[channel]
    demand = candidate.demand or 0
    demand_ok = demand >= spec["needs_demand"]
    questions_ok = candidate.questions >= spec["needs_questions"]
    said_ok = candidate.said >= spec["needs_said"]
    cleared = demand_ok and questions_ok and said_ok
    reason = {
        "channel": channel,
        "on_demand": spec["on_demand"],
        "needs_demand": spec["needs_demand"], "demand": candidate.demand, "demand_ok": demand_ok,
        "needs_questions": spec["needs_questions"], "questions": candidate.questions,
        "questions_ok": questions_ok,
        "needs_said": spec["needs_said"], "said": candidate.said, "said_ok": said_ok,
    }
    return cleared, reason


async def _tenant_tour_ids(tenant_id: UUID, conn) -> list:
    """AA-545 — tours this tenant has actually picked/rewritten. Segment/Score/Route are now
    platform-wide (no `tenant_id` column at all), so this scoping — implicit before via each
    table's own tenant_id filter — must now be explicit here, same join
    `segment_matching.py::run_segment_matching()`'s own docstring already established as "the
    tours this tenant made their own"."""
    # INTENTIONAL: the JOIN into published_tours below reads a shared reference pool (100%
    # sentinel tenant_id=aa_internal), not per-tenant data — the real per-tenant boundary is
    # already drawn by tenant_tour_versions.tenant_id above it. KHÔNG BAO GIỜ chuyển sang RLS
    # pool thật cho query này — xem AA-578.
    rows = await conn.fetch("""
        SELECT pt.tour_id
        FROM gold_aa_internal.tenant_tour_versions ttv
        JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
        WHERE ttv.tenant_id = $1::uuid
    """, tenant_id)
    return [r["tour_id"] for r in rows]


async def _tenant_market_codes(tenant_id: UUID, conn) -> tuple[list[str], list[str]]:
    """AA-545 Q3/segment-grain follow-up — every finite market this tenant targets, as the short
    codes `atom_ranking.market` stores (not DataForSEO location codes). Mirrors
    `resolve_buyer_markets()`'s own fallback (unknown/empty -> `["US"]`) so a tenant with no
    usable `target_market` still gets a real, non-empty market list to read against.

    AA-629: returns `(market_codes, unmatched)` — `unmatched` is non-empty ONLY when the tenant
    declared real `target_market.countries` that DFS_LOCATION_MAP doesn't know (never for a
    tenant who declared nothing at all — that case's `["US"]` default is a legitimate product
    choice, not a bug). Best-effort records a Tier-1 `unmapped_market_requests` row when this
    happens — a write failure here must NEVER break the tenant's own Slate read, so it is caught
    and logged, not raised."""
    from services.seo_intelligence.seed_builder import (
        LOCATION_CODE_TO_MARKET, resolve_buyer_markets, unmatched_countries,
    )
    from shared.services.tenant_config_service import TenantConfigService

    cfg = await TenantConfigService(conn).get_seo_config(tenant_id)
    unmatched = unmatched_countries(cfg.target_market)
    if unmatched:
        from shared.dfs_client.unmapped_market import record_unmapped_market_request
        for country_code in unmatched:
            try:
                await record_unmapped_market_request(conn, tenant_id, country_code)
            except Exception as exc:
                logger.warning(
                    "unmapped_market_request_record_failed",
                    tenant_id=str(tenant_id), country_code=country_code, error=str(exc),
                )
    codes = [
        LOCATION_CODE_TO_MARKET[loc] for loc, _name, _lang in resolve_buyer_markets(cfg.target_market)
    ]
    return codes, unmatched


async def _fetch_segment_candidates(tenant_id: UUID, pool, markets: list[str]) -> list[Candidate]:
    """One Candidate per ranked, non-excluded Segment, scoped to tours this tenant picked.

    AA-545 — `atom_ranking` is now platform-wide per (market, tour, segment), not per tenant.
    `MIN(total_rank)` across the tenant's OWN market list reproduces exactly what
    `rank_segments()` used to pick at write time ("market for the lowest total wins" — AA-545's
    Score section, confirmed NOT an existing Slate mechanism before this build, see the AA-545
    Linear issue's own grill-round-3 correction) — just relocated to read time, over rows that
    are now real per-market data instead of one already-merged row.
    """
    async with pool.acquire() as conn:
        tour_ids = await _tenant_tour_ids(tenant_id, conn)
        if not tour_ids:
            return []
        rows = await conn.fetch("""
            SELECT ar.segment_id,
                   MIN(ar.total_rank)     AS total_rank,
                   MIN(ar.demand_volume)  AS demand_volume,
                   MIN(ar.questions)      AS questions,
                   MIN(ar.said)           AS said,
                   MAX(asg.canonical_place)  AS canonical_place,
                   MAX(asg.canonical_action) AS canonical_action
            FROM acp_contract.atom_ranking ar
            JOIN acp_contract.atom_segment asg ON asg.segment_id = ar.segment_id
            WHERE ar.tour_id = ANY($1::uuid[]) AND ar.market = ANY($2::text[])
              AND ar.excluded_reason IS NULL
            GROUP BY ar.segment_id
        """, tour_ids, markets)
    return [
        Candidate(
            segment_id=r["segment_id"], route_id=None, score=r["total_rank"],
            demand=r["demand_volume"], questions=r["questions"] or 0, said=r["said"] or 0,
            place=r["canonical_place"], action=r["canonical_action"],
        )
        for r in rows
    ]


async def _fetch_route_candidates(tenant_id: UUID, pool, markets: list[str]) -> list[Candidate]:
    """One Candidate per Route (Blog-only grain, AA-CIS's own extension — STEP0 Q2), scoped to
    tours this tenant picked.

    AA-545 Q3 — `route` no longer stores `score`/`tenant_id` at all (platform-wide composition
    only). Score is computed HERE, at read time: `AVG(total_rank)` over the Route's member
    Segments (AA-510's own mean-of-total_rank formula, unchanged), for EACH of the tenant's
    markets, and the market giving the lowest average wins — same "best market wins, its numbers
    travel together" principle `rank_segments()` used to apply at write time, now applied to
    Route's read-time score exactly like Segment's above. Demand/questions travel with whichever
    market wins (not independently maximized across markets — mirrors the pre-AA-545 write-time
    `RankedSegment.demand_market`/`demand_volume` pairing).

    A Route with no matching `atom_ranking` row for ANY of the tenant's markets (e.g. a route
    just detected, one A3 ranking pass behind) does not appear here — same "derived, eventually
    consistent" gap every other consumer of freshly-derived data in this pipeline already
    tolerates, disclosed rather than silently different from the pre-AA-545 behavior (which
    always had a stored `score`, even if stale).
    """
    async with pool.acquire() as conn:
        tour_ids = await _tenant_tour_ids(tenant_id, conn)
        if not tour_ids:
            return []
        rows = await conn.fetch("""
            WITH per_market AS (
                SELECT r.route_id, r.hub_name, r.hub_id, ar.market,
                       AVG(ar.total_rank) AS avg_rank,
                       MAX(ar.demand_volume) AS demand_volume,
                       COALESCE(SUM(ar.questions), 0) AS questions
                FROM acp_contract.route r
                JOIN acp_contract.atom_ranking ar
                    ON ar.tour_id = r.tour_id
                   AND ar.segment_id = ANY (
                           SELECT jsonb_array_elements_text(r.ordered_segment_ids)
                       )
                   AND ar.excluded_reason IS NULL
                   AND ar.market = ANY($2::text[])
                -- AA-532: only the CURRENT version of a Route identity is a real candidate — a
                -- superseded row stays in the table (never deleted, so a Subject already
                -- pointing at it keeps resolving) but must not keep getting freshly proposed.
                WHERE r.superseded_at IS NULL AND r.tour_id = ANY($1::uuid[])
                GROUP BY r.route_id, r.hub_name, r.hub_id, ar.market
            )
            SELECT DISTINCT ON (route_id)
                   route_id, hub_name, hub_id, avg_rank AS score, demand_volume, questions
            FROM per_market
            ORDER BY route_id, avg_rank ASC
        """, tour_ids, markets)
    return [
        Candidate(
            segment_id=None, route_id=r["route_id"], score=float(r["score"]),
            demand=r["demand_volume"], questions=int(r["questions"] or 0), said=0,
            hub_name=r["hub_name"], hub_id=str(r["hub_id"]) if r["hub_id"] else None,
        )
        for r in rows
    ]


async def _fetch_segment_hub_map(tenant_id: UUID, pool) -> dict[str, str]:
    """Segment -> a hub key, for the hub-cap's Segment-grain half (Gap B) — the join the
    original AA-511 pass's own module docstring explicitly disclosed as NOT built ("requires a
    Hub/Route join for every one of the 7 non-Blog channels ... nothing in this build's scope
    asked for"). Built here, on request.

    A Segment sits in 0, 1, or several Routes (a Route is one tour's day-span, so the same
    Segment told by 3 tours can sit in 3 different Routes). Mirrors the origin's own
    `_available()`: "a moment told by several itineraries belongs to the hub most of them are
    in, and to the first by name when they are even" — counted here by containing-Route rather
    than by trip_code directly, since a Route already carries the tour-to-hub resolution
    (`route_detection.py::run_route_detection()`). A Segment in no Route at all is its own
    singleton hub (its own segment_id) — one grain down from the origin's "the itinerary itself
    where there is no family" fallback.
    """
    async with pool.acquire() as conn:
        tour_ids = await _tenant_tour_ids(tenant_id, conn)
        if not tour_ids:
            return {}
        rows = await conn.fetch("""
            SELECT r.route_id, r.hub_id, r.hub_name,
                   jsonb_array_elements_text(r.ordered_segment_ids) AS segment_id
            FROM acp_contract.route r
            WHERE r.tour_id = ANY($1::uuid[]) AND r.superseded_at IS NULL  -- AA-532
        """, tour_ids)

    counts: dict[str, dict[str, int]] = {}
    names: dict[str, str] = {}
    for r in rows:
        hub_key = str(r["hub_id"]) if r["hub_id"] else r["route_id"]
        names[hub_key] = r["hub_name"]
        held = counts.setdefault(r["segment_id"], {})
        held[hub_key] = held.get(hub_key, 0) + 1

    return {
        segment_id: min(held, key=lambda key: (-held[key], names.get(key, key)))
        for segment_id, held in counts.items()
    }


def _choose(channel: str, candidates: list[Candidate], hub_of: dict[str, str]) -> list[tuple[Candidate, dict]]:
    """Port of the origin's `choose()` (aa-social-media `stages/slate.py`) — Gap B, 2026-09-02.

    **Iterated strongest-first**: ascending `score` (rank-sum convention, AA-515 — lowest
    `total_rank`/Route-score wins), the same order `fetch_slate()` already presents. A tie is
    resolved first-come in that order — the origin has no "prefer newest" rule and neither does
    this; recency plays no part anywhere in `choose()`.

    **A place appears once per Channel** (Segment grain only — a Route has no single `place`,
    it already spans several; the origin's own Blog subjects stay Segment-grain, so this rule
    never had a Route-shaped case to handle there either). Once a `canonical_place` has been
    taken by a stronger Segment, a weaker Segment at the same place is skipped for this Channel.

    **`most_per_hub`** (real `reference/channels.toml` numbers, see `CHANNEL_BARS` — 99,
    "effectively off", for every weekly-rhythm Channel; `None`/uncapped for the 3 on-demand ones,
    this build's own honest choice where the origin has no number at all). Route candidates use
    their own Route's hub (`hub_id`, falling back to the `route_id` itself when standalone — "a
    family of one is not a family", the same rule `route_detection.py` already applies one layer
    up); Segment candidates use `hub_of` (`_fetch_segment_hub_map()`, Gap B's own new join).

    Applied PER CHANNEL — a fresh `taken`/`seen_places` for every call, matching `_choose()`
    being invoked once per Channel in `propose_slate()`'s own loop, never accumulated globally.

    Returns `(candidate, cleared_bar_reason)` pairs, already Bar-checked — only what `choose()`
    would put on the Recommendation's own `subjects` list, i.e. exactly what should end up
    `INSERT`ed as a `proposed` `acp_shared.subject` row this run.
    """
    most_per_hub = CHANNEL_BARS[channel]["most_per_hub"]
    ordered = sorted(candidates, key=lambda c: (c.score, c.segment_id or c.route_id or ""))
    taken: dict[str, int] = {}
    seen_places: set[str] = set()
    chosen: list[tuple[Candidate, dict]] = []
    for candidate in ordered:
        cleared, reason = _clears_bar(channel, candidate)
        if not cleared:
            continue
        if candidate.place is not None and candidate.place in seen_places:
            continue
        hub_key = None
        if candidate.route_id:
            hub_key = candidate.hub_id or candidate.route_id
        elif candidate.segment_id:
            hub_key = hub_of.get(candidate.segment_id, candidate.segment_id)
        if most_per_hub is not None and hub_key is not None:
            if taken.get(hub_key, 0) >= most_per_hub:
                continue
            taken[hub_key] = taken.get(hub_key, 0) + 1
        if candidate.place is not None:
            seen_places.add(candidate.place)
        chosen.append((candidate, reason))
    return chosen


async def propose_slate(tenant_id: UUID, pool) -> dict:
    """Recompute what clears each Channel's Bar right now, and persist it (AA-511's `run()`).

    Runs on every `GET /v1/slate` (matches the origin: "Storing is deterministic and always
    runs... the evidence a Subject carries has to be able to grow"). A row already `picked`/
    `used`/`cut` is NEVER touched by this function — only rows still sitting at `proposed` are
    refreshed or removed. A candidate that no longer clears the Bar, or was cleared but lost its
    `_choose()` seat this run (de-dup'd on place, or its hub is over `most_per_hub` — Gap B), has
    its stale `proposed` row deleted; this is the one place this build's scope still ports the
    origin's `delete_missing()` behavior, since leaving a phantom `proposed` row around would make
    `GET /v1/slate`'s own eligible-count wrong.
    """
    async with pool.acquire() as _mkt_conn:
        markets, unmatched_markets = await _tenant_market_codes(tenant_id, _mkt_conn)
    segments = await _fetch_segment_candidates(tenant_id, pool, markets)
    routes = await _fetch_route_candidates(tenant_id, pool, markets)
    hub_of = await _fetch_segment_hub_map(tenant_id, pool)

    # AA-631 (Debate stage) — applied AFTER rank-sum (fetch above) but BEFORE _choose()'s own
    # Bar/place-dedup/hub-cap, the earliest point data is per-tenant (tenant_id, brand profile
    # known) but not yet narrowed by Bar. Segments and Routes are cut independently (each is
    # its own candidate pool at this point, same as _choose() below treats them per-Channel).
    async with pool.acquire() as _debate_conn:
        from services.acp_shared.debate import apply_debate
        try:
            segments = await apply_debate(tenant_id, segments, _debate_conn)
            routes = await apply_debate(tenant_id, routes, _debate_conn)
        except Exception as exc:
            # Advisory per the origin's own safety rule — Debate must never be able to make the
            # Slate empty (or fail entirely) by erroring; a failure here means the rules-based
            # (pre-Debate) list stands unchanged for this run.
            logger.warning("debate_apply_failed", tenant_id=str(tenant_id), error=str(exc))

    async with pool.acquire() as conn:
        async with conn.transaction():
            live_segment_ids: dict[str, set[str]] = {}
            live_route_ids: dict[str, set[str]] = {}
            for channel in CHANNEL_BARS:
                spec = CHANNEL_BARS[channel]
                candidates = routes if spec["grain"] == "route" else segments
                live_segment_ids[channel] = set()
                live_route_ids[channel] = set()
                # _choose() already applies the Bar (returns only what cleared it) AND `choose()`'s
                # own place-de-dup/hub-cap (Gap B) -- only what survives both gets proposed.
                for candidate, reason in _choose(channel, candidates, hub_of):
                    if candidate.segment_id:
                        live_segment_ids[channel].add(candidate.segment_id)
                        # The ON CONFLICT target must match idx_subject_unique_segment's own
                        # predicate exactly (`WHERE segment_id IS NOT NULL`, migration 133) --
                        # `state` is checked in the DO UPDATE's own WHERE instead, so a conflict
                        # against an already-picked/used/cut row is a silent no-op (left alone)
                        # rather than an inference-target mismatch error.
                        await conn.execute("""
                            INSERT INTO acp_shared.subject
                                (tenant_id, segment_id, route_id, channel, cleared_bar_reason, score)
                            VALUES ($1::uuid, $2, NULL, $3, $4::jsonb, $5)
                            ON CONFLICT (tenant_id, channel, segment_id) WHERE segment_id IS NOT NULL
                            DO UPDATE SET cleared_bar_reason = excluded.cleared_bar_reason,
                                          score = excluded.score
                            WHERE acp_shared.subject.state = 'proposed'
                        """, tenant_id, candidate.segment_id, channel,
                            json.dumps(reason), candidate.score)
                    else:
                        live_route_ids[channel].add(candidate.route_id)
                        await conn.execute("""
                            INSERT INTO acp_shared.subject
                                (tenant_id, segment_id, route_id, channel, cleared_bar_reason, score)
                            VALUES ($1::uuid, NULL, $2, $3, $4::jsonb, $5)
                            ON CONFLICT (tenant_id, channel, route_id) WHERE route_id IS NOT NULL
                            DO UPDATE SET cleared_bar_reason = excluded.cleared_bar_reason,
                                          score = excluded.score
                            WHERE acp_shared.subject.state = 'proposed'
                        """, tenant_id, candidate.route_id, channel,
                            json.dumps(reason), candidate.score)

            for channel in CHANNEL_BARS:
                await conn.execute("""
                    DELETE FROM acp_shared.subject
                    WHERE tenant_id = $1::uuid AND channel = $2 AND state = 'proposed'
                      AND segment_id IS NOT NULL
                      AND NOT (segment_id = ANY($3::text[]))
                """, tenant_id, channel, list(live_segment_ids[channel]))
                await conn.execute("""
                    DELETE FROM acp_shared.subject
                    WHERE tenant_id = $1::uuid AND channel = $2 AND state = 'proposed'
                      AND route_id IS NOT NULL
                      AND NOT (route_id = ANY($3::text[]))
                """, tenant_id, channel, list(live_route_ids[channel]))

    # AA-629 — non-empty only when the tenant declared real countries DFS_LOCATION_MAP doesn't know.
    return {
        "segments_seen": len(segments), "routes_seen": len(routes),
        "unmatched_markets": unmatched_markets,
    }


async def fetch_slate(tenant_id: UUID, pool) -> dict:
    """Everything currently on the Slate, grouped by Channel, strongest (lowest score) first.

    Presentation fields (`place`/`action`/`hub_name`) are joined live rather than duplicated on
    the `subject` row (LEFT JOIN — a Subject's `place`/`hub_name` can still come back NULL if its
    Segment itself was deleted, though its Route no longer can be, see below). Before AA-532,
    `acp_contract.route` was rebuilt whole (DELETE+INSERT) every T5/T7 ranking run, so a `subject`
    whose Route had just been rebuilt away routinely hit this NULL path — expected for a
    `proposed` row (the next `propose_slate()` call cleans it up), disclosed as stale-but-harmless
    for a `picked` one. AA-532 replaced that with versioning (supersede, never delete) — a
    `subject.route_id` now always resolves to a real row (current or superseded), so this LEFT
    JOIN degrading to NULL for a Route-based Subject should no longer actually happen in
    practice; kept as a LEFT JOIN regardless, since it's still needed for the Segment-based half
    (`atom_segment` rows genuinely can be deleted) and costs nothing extra.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT s.subject_id, s.channel, s.state, s.score, s.cleared_bar_reason,
                   s.segment_id, s.route_id, s.created_at,
                   asg.canonical_place, asg.canonical_action,
                   r.hub_name, r.ordered_segment_ids, r.tour_id
            FROM acp_shared.subject s
            LEFT JOIN acp_contract.atom_segment asg ON asg.segment_id = s.segment_id
            LEFT JOIN acp_contract.route r ON r.route_id = s.route_id
            WHERE s.tenant_id = $1::uuid AND s.state != 'cut'
            ORDER BY s.channel, s.score ASC NULLS LAST, s.subject_id
        """, tenant_id)

    by_channel: dict[str, list[dict]] = {c: [] for c in CHANNEL_BARS}
    for r in rows:
        reason = r["cleared_bar_reason"]
        if isinstance(reason, str):
            reason = json.loads(reason)
        by_channel.setdefault(r["channel"], []).append({
            "subject_id": str(r["subject_id"]),
            "channel": r["channel"],
            "state": r["state"],
            "score": float(r["score"]) if r["score"] is not None else None,
            "cleared_bar_reason": reason,
            "segment_id": r["segment_id"],
            "route_id": r["route_id"],
            "place": r["canonical_place"],
            "action": r["canonical_action"],
            "hub_name": r["hub_name"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {
        channel: {
            "channel": channel,
            "on_demand": CHANNEL_BARS[channel]["on_demand"],
            "eligible_count": sum(1 for s in subjects if s["state"] == "proposed"),
            "subjects": subjects,
        }
        for channel, subjects in by_channel.items()
    }


class SubjectNotFoundError(Exception):
    """No `acp_shared.subject` row for this id, for this tenant."""


class SubjectNotEligibleError(Exception):
    """The Subject exists but is not in `state = 'proposed'` (already picked/used/cut)."""


async def pick_subject(tenant_id: UUID, subject_id: UUID, pool, selected_by: str) -> dict:
    """Flip a Subject `proposed -> picked` and create the matching `angle_gate_request`
    (T8's own entry point, `services/acp_angle_gate/service.py::create_request()` grain is
    per-atom). `atom_id`/`trip_id` still resolve to ONE representative atom (the Route's first
    ordered Segment's first member atom for its own `tour_id`) — kept for every existing
    atom-grain reader (`angle_gate_request.atom_id` is `NOT NULL`, and T8's own goal/angle steps
    only ever look at one atom).

    **Gap A fix (2026-09-02, post-Done follow-up)**: a Route/Blog pick ALSO persists
    `route_segment_ids` — the Route's full `ordered_segment_ids`, not just the one representative
    atom — into the new `angle_gate_request.route_segment_ids` column (migration 134). T9's
    `start_write()` (`services/acp_content_writing/service.py`) reads it and, when present, builds
    the write seed from every Segment's text along the walk instead of the single representative
    atom's — closing the "T9 sees only one atom, not the whole journey" gap the original AA-511
    pass's own docstring disclosed here. `channel` is set on `angle_gate_request` immediately
    (this build knows it already, from the Subject) rather than through the AA-469 Việc 4
    two-step atom-then-channel flow, which stays exactly as it is for the atom-picker's own
    (non-Slate) entry point.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT subject_id, channel, state, segment_id, route_id
            FROM acp_shared.subject WHERE subject_id = $1::uuid AND tenant_id = $2::uuid
        """, subject_id, tenant_id)
        if row is None:
            raise SubjectNotFoundError(f"subject_id={subject_id} not found for this tenant")
        if row["state"] != "proposed":
            raise SubjectNotEligibleError(
                f"subject_id={subject_id} is '{row['state']}', not 'proposed'"
            )

        atom_id, trip_id, route_segment_ids = await _resolve_representative_atom(
            conn, tenant_id, row["segment_id"], row["route_id"],
        )
        if atom_id is None:
            raise SubjectNotEligibleError(
                f"subject_id={subject_id} has no live atom left to write from "
                "(its Segment/Route was rebuilt away) — refresh the Slate and pick again."
            )

        async with conn.transaction():
            await conn.execute(
                "UPDATE acp_shared.subject SET state = 'picked' WHERE subject_id = $1::uuid",
                subject_id,
            )
            request_row = await conn.fetchrow("""
                INSERT INTO acp_shared.angle_gate_request
                    (tenant_id, atom_id, trip_id, channel, subject_id, route_segment_ids)
                VALUES ($1::uuid, $2, $3::uuid, $4, $5::uuid, $6::jsonb)
                RETURNING request_id, tenant_id, atom_id, trip_id, channel, status, created_at,
                          route_segment_ids
            """, tenant_id, atom_id, trip_id, row["channel"], subject_id,
                json.dumps(route_segment_ids) if route_segment_ids is not None else None)

            # AA-559 — semantic tenant-activity log, same transaction as the state change above
            # (matches every other tenant-self-service audit_log writer's own convention —
            # trip_reallocation.py's confirm_trip_reallocation() — a lost audit row fails the
            # whole pick, not a silently-dropped side effect).
            await write_audit_log(
                conn, tenant_id=str(tenant_id), actor=selected_by,
                action=TenantAuditAction.SLATE_SUBJECT_PICKED, resource_type="subject",
                resource_id=str(subject_id),
                details={
                    "channel": row["channel"], "request_id": str(request_row["request_id"]),
                    "atom_id": atom_id, "trip_id": str(trip_id) if trip_id else None,
                },
            )

    return {
        "subject_id": str(subject_id),
        "channel": row["channel"],
        "request_id": str(request_row["request_id"]),
        "atom_id": request_row["atom_id"],
        "trip_id": str(request_row["trip_id"]) if request_row["trip_id"] else None,
        "status": request_row["status"],
        "route_segment_ids": route_segment_ids,
    }


async def cut_subject(tenant_id: UUID, subject_id: UUID, pool) -> dict:
    """AA-554 mục H.2 — flip a Subject `proposed -> cut`. Built backend/API-only in that pass, per
    Nghiệp's final decision (Linear comment "Điều chỉnh quyết định mục H", 07/09/2026), to avoid
    scope creep — the tenant-facing "Cut" button itself shipped separately in AA-556
    (SlateTab.tsx's `SubjectRow`), which is this function's only real caller today.

    Only `proposed -> cut` is legal (mirrors `pick_subject()`'s own state guard above) — a Subject
    already `picked`/`used` has already become real tenant activity (an `angle_gate_request`, and
    for `used`, a real `content_piece`); retracting it after the fact is a different, unbuilt
    decision (it would need to also unwind or flag what it already spawned) and out of this
    function's scope."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT subject_id, channel, state FROM acp_shared.subject
            WHERE subject_id = $1::uuid AND tenant_id = $2::uuid
        """, subject_id, tenant_id)
        if row is None:
            raise SubjectNotFoundError(f"subject_id={subject_id} not found for this tenant")
        if row["state"] != "proposed":
            raise SubjectNotEligibleError(
                f"subject_id={subject_id} is '{row['state']}', not 'proposed'"
            )
        updated = await conn.fetchrow("""
            UPDATE acp_shared.subject SET state = 'cut' WHERE subject_id = $1::uuid
            RETURNING subject_id, channel, state
        """, subject_id)

    return {
        "subject_id": str(updated["subject_id"]), "channel": updated["channel"],
        "state": updated["state"],
    }


async def _resolve_representative_atom(
    conn, tenant_id: UUID, segment_id: str | None, route_id: str | None,
) -> tuple[str | None, str | None, list[str] | None]:
    """(atom_id, trip_id, route_segment_ids) to hand T8/T9 — the third element is `None` for a
    Segment pick and the Route's full `ordered_segment_ids` for a Route/Blog pick (Gap A). See
    `pick_subject()`'s own docstring for what now actually consumes it.

    AA-567 — `owner_scope IN ('platform', tenant_id)`, NOT `owner_scope = tenant_id` alone. Since
    AA-526, atomize runs exactly once, platform-wide (`owner_scope='platform'`) — a tenant-owned
    atom (`owner_scope=tenant_id`, from that tenant's own T5 rewrite) is the rare exception, not
    the main path. Every real Segment this function is ever called for is built from the shared
    platform atom pool (confirmed live: 100% of a live sample of WanderLux Travel's proposed
    Subjects resolved to `owner_scope='platform'`-only Segments, 0 tenant-owned atoms among
    them) — the old tenant-only filter meant this function, and everything downstream of it
    (T8/T9/T10), could never actually succeed for the real, common case. Confirmed via
    docs/adr/0001 + 0003 and CONTEXT.md's own cross-tenant table (row 2: "every tenant's Segment
    build reads the SAME acp_contract.tour_atoms rows... owner_scope='platform'") that Atom
    ownership carries no tenant-isolation meaning — atoms hold AA's own neutral, brand-agnostic
    text (AA-535), not tenant content; the tenant-facing security boundary is which
    angle_gate_request/content_piece ROW a tenant can access (tenant_id on those tables), never
    which underlying atom text they can read. Kept a tenant-owned atom acceptable too (not
    switched to 'platform'-only) so a tenant's own T5-rewritten-tour atoms, on the rare Segment
    built from them, still resolve exactly as before."""
    if segment_id:
        row = await conn.fetchrow("""
            SELECT m.atom_id, ta.tour_id
            FROM acp_contract.atom_segment_member m
            JOIN acp_contract.tour_atoms ta ON ta.atom_id = m.atom_id
            WHERE m.segment_id = $1 AND NOT m.is_alias
              AND ta.owner_scope IN ('platform', $2::text) AND NOT ta.deleted AND NOT ta.is_empty_marker
            ORDER BY m.atom_id LIMIT 1
        """, segment_id, str(tenant_id))
        return (row["atom_id"], str(row["tour_id"]), None) if row else (None, None, None)

    route = await conn.fetchrow(
        "SELECT tour_id, ordered_segment_ids FROM acp_contract.route WHERE route_id = $1", route_id,
    )
    if route is None:
        return None, None, None
    segment_ids = route["ordered_segment_ids"]
    if isinstance(segment_ids, str):
        segment_ids = json.loads(segment_ids)
    if not segment_ids:
        return None, None, None
    row = await conn.fetchrow("""
        SELECT m.atom_id
        FROM acp_contract.atom_segment_member m
        JOIN acp_contract.tour_atoms ta ON ta.atom_id = m.atom_id
        WHERE m.segment_id = $1 AND ta.tour_id = $2::uuid
          AND ta.owner_scope IN ('platform', $3::text)
          AND NOT m.is_alias AND NOT ta.deleted AND NOT ta.is_empty_marker
        ORDER BY m.atom_id LIMIT 1
    """, segment_ids[0], route["tour_id"], str(tenant_id))
    return (row["atom_id"], str(route["tour_id"]), list(segment_ids)) if row else (None, None, None)


__all__ = [
    "CHANNEL_BARS", "WEEKLY_RHYTHM_CHANNELS", "ON_DEMAND_CHANNELS",
    "Candidate", "propose_slate", "fetch_slate", "pick_subject",
    "SubjectNotFoundError", "SubjectNotEligibleError",
]
