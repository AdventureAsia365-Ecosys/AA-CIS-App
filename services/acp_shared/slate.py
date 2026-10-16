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
Bar + Score only) — see `_choose_slate()` below for the ported mechanism and every grain difference
from the origin, disclosed there rather than here.

**Tenant channels + per-channel place rule (AA-840, S238).** The Slate proposes only for the
tenant's enabled Channels (`acp_shared.tenant_config.channels`; no row = every Channel, the
behaviour before AA-840). A place may appear once per Channel and on at most
`MAX_CHANNELS_PER_PLACE` Channels in one Slate, and Channels take turns (round-robin, strongest
candidate first) so the first Channel in `CHANNEL_BARS` no longer takes every place. This replaces
AA-632's single `seen_places` set shared across Channels, which let LinkedIn (first, same bar as
Facebook/Instagram/TikTok) starve every later Channel.

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
from typing import Optional
from uuid import UUID

import structlog

from services.acp_shared.audit_log import TenantAuditAction, write_audit_log
from services.acp_shared.subject_seed import (
    choose_representative, load_alias_cards, load_segment_members,
)

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

# AA-840 — the most Channels one place (Segment `canonical_place`) may be proposed on in one
# tenant's Slate. A place still appears at most once per Channel. 3 of the 7 Segment-grain
# Channels keeps one strong place from filling the whole Slate while leaving enough seats for
# every enabled Channel to get proposals (a tenant with N bar-clearing places has 3*N seats).
# Routes (Blog) carry no place and are never counted.
MAX_CHANNELS_PER_PLACE = 3


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
              AND ar.superseded_at IS NULL  -- AA-734: current ranking row only
            GROUP BY ar.segment_id
        """, tour_ids, markets)
        rows = await _segments_with_seed(tenant_id, conn, rows, {str(t) for t in tour_ids})
    return [
        Candidate(
            segment_id=r["segment_id"], route_id=None, score=r["total_rank"],
            demand=r["demand_volume"], questions=r["questions"] or 0, said=r["said"] or 0,
            place=r["canonical_place"], action=r["canonical_action"],
        )
        for r in rows
    ]


async def _segments_with_seed(tenant_id: UUID, conn, rows: list, tour_ids: set[str]) -> list:
    """AA-857 — keep only Segment candidates that have a seed atom for this tenant: a live member on
    the tenant's own tours whose place is the Segment's `canonical_place`, in one country
    (`subject_seed.choose_representative`, the same rule `pick_subject()` uses). A Segment without
    one is not offered — it would be a Subject whose post is about another place (S239: "Gyeongju"
    written about a Pakistan museum). Each skip is logged with its reason; the count goes to the
    `slate_subject_skipped` log line once per proposal."""
    if not rows:
        return rows
    segment_ids = [r["segment_id"] for r in rows]
    members = await load_segment_members(conn, segment_ids, str(tenant_id))
    alias_cards = await load_alias_cards(
        conn, [r["canonical_place"] for r in rows] + [m.place for ms in members.values() for m in ms],
    )
    kept, skipped = [], {}
    for r in rows:
        member, skip = choose_representative(
            r["canonical_place"], members.get(r["segment_id"], []),
            tenant_tour_ids=tour_ids, alias_cards=alias_cards,
        )
        if member is None:
            skipped[r["segment_id"]] = skip
            continue
        kept.append(r)
    if skipped:
        reasons: dict[str, int] = {}
        for reason in skipped.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        logger.info("slate_subject_skipped", tenant_id=str(tenant_id), skipped=len(skipped),
                    reasons=reasons, segment_ids=sorted(skipped)[:50])
    return kept


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
                   AND ar.superseded_at IS NULL  -- AA-734: current ranking row only
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


async def _tenant_enabled_channels(tenant_id: UUID, conn) -> list[str]:
    """AA-840 — the Channels this tenant's Slate proposes for, in `CHANNEL_BARS` order.

    Source: `acp_shared.tenant_config.channels` (admin Tenant 360 > Planning config, migration
    101). A tenant with no row has never been configured and keeps the pre-AA-840 behaviour: every
    Channel. Values the Slate does not know are ignored rather than raised (the column is a free
    `text[]`, guarded only by a CHECK)."""
    configured = await conn.fetchval(
        "SELECT channels FROM acp_shared.tenant_config WHERE tenant_id = $1::uuid", tenant_id,
    )
    if not configured:
        return list(CHANNEL_BARS)
    wanted = set(configured)
    return [channel for channel in CHANNEL_BARS if channel in wanted]


def _hub_key(candidate: Candidate, hub_of: dict[str, str]) -> str | None:
    """The hub a candidate counts against for `most_per_hub` (Gap B): a Route's own hub (or the
    Route itself when standalone — "a family of one is not a family"), a Segment's hub from
    `_fetch_segment_hub_map()` (or the Segment itself when it sits in no Route)."""
    if candidate.route_id:
        return candidate.hub_id or candidate.route_id
    if candidate.segment_id:
        return hub_of.get(candidate.segment_id, candidate.segment_id)
    return None


def _choose_slate(
    channels: list[str], segments: list[Candidate], routes: list[Candidate], hub_of: dict[str, str],
    max_channels_per_place: int | None = MAX_CHANNELS_PER_PLACE,
) -> dict[str, list[tuple[Candidate, dict]]]:
    """Port of the origin's `choose()` (aa-social-media `stages/slate.py`, Gap B 2026-09-02),
    run over every enabled Channel of one tenant's Slate at once (AA-840).

    **Bar first**: a candidate that does not clear the Channel's Bar is never proposed there.

    **Iterated strongest-first** per Channel: ascending `score` (rank-sum convention, AA-515 —
    lowest `total_rank`/Route-score wins), ties broken by id. Recency plays no part.

    **Channels take turns** (AA-840): each round, every Channel in `channels` order (the
    `CHANNEL_BARS` order) takes its strongest candidate still allowed, until no Channel can take
    another. Before AA-840 each Channel took everything it could before the next one started, so
    with one shared place set LinkedIn (first, same Bar as Facebook/Instagram/TikTok) took every
    place and the later Channels got nothing.

    **Place rule** (Segment grain only — a Route has no single `place`):
      - a place appears at most once per Channel (the origin's own rule; the strongest Segment at
        that place wins the seat);
      - a place appears on at most `max_channels_per_place` Channels of the Slate (`None` = no
        cross-Channel cap), so one place cannot fill the whole Slate. Replaces AA-632's shared
        `seen_places` (a place on ONE Channel only), which caused the starvation above.

    **`most_per_hub`** (`CHANNEL_BARS`, 99 "effectively off" for weekly-rhythm Channels, `None` for
    the on-demand ones) is counted per Channel, as before: it is a per-Channel Bar setting.

    Deterministic: same inputs, same output. Returns `{channel: [(candidate, cleared_bar_reason)]}`
    for every Channel in `channels` — exactly what should be `INSERT`ed as `proposed` rows.
    """
    queues: dict[str, list[tuple[Candidate, dict]]] = {}
    for channel in channels:
        pool = routes if CHANNEL_BARS[channel]["grain"] == "route" else segments
        ordered = sorted(pool, key=lambda c: (c.score, c.segment_id or c.route_id or ""))
        queue = []
        for candidate in ordered:
            cleared, reason = _clears_bar(channel, candidate)
            if cleared:
                queue.append((candidate, reason))
        queues[channel] = queue

    chosen: dict[str, list[tuple[Candidate, dict]]] = {channel: [] for channel in channels}
    cursor = {channel: 0 for channel in channels}
    places_in_channel: dict[str, set[str]] = {channel: set() for channel in channels}
    hubs_in_channel: dict[str, dict[str, int]] = {channel: {} for channel in channels}
    channels_per_place: dict[str, int] = {}

    progressed = True
    while progressed:
        progressed = False
        for channel in channels:
            most_per_hub = CHANNEL_BARS[channel]["most_per_hub"]
            queue, taken = queues[channel], hubs_in_channel[channel]
            # Every skip below is final for this Channel (place/hub counts only ever grow), so the
            # cursor never needs to look back.
            while cursor[channel] < len(queue):
                candidate, reason = queue[cursor[channel]]
                cursor[channel] += 1
                place = candidate.place
                if place is not None:
                    if place in places_in_channel[channel]:
                        continue
                    if (max_channels_per_place is not None
                            and channels_per_place.get(place, 0) >= max_channels_per_place):
                        continue
                hub_key = _hub_key(candidate, hub_of)
                if most_per_hub is not None and hub_key is not None:
                    if taken.get(hub_key, 0) >= most_per_hub:
                        continue
                    taken[hub_key] = taken.get(hub_key, 0) + 1
                if place is not None:
                    places_in_channel[channel].add(place)
                    channels_per_place[place] = channels_per_place.get(place, 0) + 1
                chosen[channel].append((candidate, reason))
                progressed = True
                break
    return chosen


def _choose(
    channel: str, candidates: list[Candidate], hub_of: dict[str, str],
) -> list[tuple[Candidate, dict]]:
    """One Channel on its own — `_choose_slate()` with a single Channel (no cross-Channel cap can
    bind). Kept for callers and tests that look at one Channel in isolation."""
    if CHANNEL_BARS[channel]["grain"] == "route":
        return _choose_slate([channel], [], candidates, hub_of)[channel]
    return _choose_slate([channel], candidates, [], hub_of)[channel]


async def propose_slate(tenant_id: UUID, pool) -> dict:
    """Recompute what clears each Channel's Bar right now, and persist it (AA-511's `run()`).

    AA-764 — runs in a `t7_slate` job (services/jobs/t7_slate_job.py), enqueued by
    request_slate_proposal(); it used to run inside every `GET /v1/slate` (origin: "Storing is
    deterministic and always runs... the evidence a Subject carries has to be able to grow"). A row already `picked`/
    `used`/`cut` is NEVER touched by this function — only rows still sitting at `proposed` are
    refreshed or removed. A candidate that no longer clears the Bar, or was cleared but lost its
    `_choose_slate()` seat this run (place rule, or its hub is over `most_per_hub` — Gap B), or
    sits on a Channel the tenant no longer has enabled (AA-840), has its stale `proposed` row
    deleted; this is the one place this build's scope still ports the
    origin's `delete_missing()` behavior, since leaving a phantom `proposed` row around would make
    `GET /v1/slate`'s own eligible-count wrong.
    """
    async with pool.acquire() as _mkt_conn:
        markets, unmatched_markets = await _tenant_market_codes(tenant_id, _mkt_conn)
        # AA-840 — only the tenant's enabled Channels get proposals. Read first so a grain no
        # enabled Channel uses (Route = Blog only; Segment = the other 7) is neither fetched nor
        # sent through Debate (Debate's brand-fit cut is a paid call on a cache miss).
        enabled_channels = await _tenant_enabled_channels(tenant_id, _mkt_conn)
    grains = {CHANNEL_BARS[channel]["grain"] for channel in enabled_channels}
    segments = await _fetch_segment_candidates(tenant_id, pool, markets) if "segment" in grains else []
    routes = await _fetch_route_candidates(tenant_id, pool, markets) if "route" in grains else []
    hub_of = await _fetch_segment_hub_map(tenant_id, pool) if "segment" in grains else {}

    # AA-631 (Debate stage) — applied AFTER rank-sum (fetch above) but BEFORE _choose_slate()'s
    # own Bar/place rule/hub-cap, the earliest point data is per-tenant (tenant_id, brand profile
    # known) but not yet narrowed by Bar. Segments and Routes are cut independently (each is
    # its own candidate pool at this point, same as _choose_slate() below treats them).
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
        # AA-840 — proposals only for `enabled_channels`; the DELETE loop below still runs over
        # every Channel, so a Channel the tenant switched off loses its `proposed` rows
        # (picked/used/cut rows stay).
        # _choose_slate() applies the Bar AND `choose()`'s place rule/hub-cap (Gap B, AA-840) --
        # only what survives both gets proposed.
        chosen = _choose_slate(enabled_channels, segments, routes, hub_of)
        async with conn.transaction():
            live_segment_ids: dict[str, set[str]] = {c: set() for c in CHANNEL_BARS}
            live_route_ids: dict[str, set[str]] = {c: set() for c in CHANNEL_BARS}
            for channel in enabled_channels:
                for candidate, reason in chosen[channel]:
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
    # AA-840 — `channels` / `proposed_per_channel` make the job result show what was planned for.
    return {
        "segments_seen": len(segments), "routes_seen": len(routes),
        "unmatched_markets": unmatched_markets,
        "channels": enabled_channels,
        "proposed_per_channel": {channel: len(chosen[channel]) for channel in enabled_channels},
    }


T7_SLATE_KIND = "t7_slate"
# AA-764 — a proposal older than this is refreshed on the next GET /v1/slate (in the background).
SLATE_STALE_AFTER_HOURS = 24


async def latest_slate_job(tenant_id: UUID, conn) -> Optional[dict]:
    """AA-764 — the tenant's most recent `t7_slate` job (any status), or None if never proposed."""
    row = await conn.fetchrow(
        """
        SELECT id, status, result, created_at, finished_at FROM shared.job
        WHERE kind = $1 AND payload->>'tenant_id' = $2
        ORDER BY created_at DESC LIMIT 1
        """,
        T7_SLATE_KIND, str(tenant_id),
    )
    if row is None:
        return None
    d = dict(row)
    if isinstance(d["result"], str):
        d["result"] = json.loads(d["result"])
    return d


async def last_proposal(tenant_id: UUID, conn) -> Optional[dict]:
    """AA-764 — the result of the tenant's latest SUCCEEDED proposal (unmatched_markets etc.)."""
    row = await conn.fetchrow(
        """
        SELECT result, finished_at FROM shared.job
        WHERE kind = $1 AND payload->>'tenant_id' = $2 AND status = 'succeeded'
        ORDER BY finished_at DESC LIMIT 1
        """,
        T7_SLATE_KIND, str(tenant_id),
    )
    if row is None:
        return None
    result = row["result"]
    if isinstance(result, str):
        result = json.loads(result)
    return {"result": result or {}, "finished_at": row["finished_at"]}


async def request_slate_proposal(tenant_id: UUID, pool, *, only_if_stale: bool) -> dict:
    """AA-764 (G5, ADR 0003) — enqueue a `t7_slate` job (services/jobs/t7_slate_job.py) unless
    one is already queued/running for this tenant. propose_slate() runs Debate (Jev + LLM brand
    fit) and took 2′48″–4′+ in the S231 pilot — it used to run inside every GET /v1/slate and
    504 at the gateway. `only_if_stale` (the GET path): enqueue only when the tenant has never
    had a successful proposal or the last one is older than SLATE_STALE_AFTER_HOURS. A tenant
    advisory lock serializes the check-then-enqueue so two loads never start two proposals.
    Returns the latest job row (the new one, the in-flight one, or the last finished one)."""
    from shared.jobs.registry import enqueue  # local: keeps this module importable without the runner
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{T7_SLATE_KIND}:{tenant_id}")
            latest = await latest_slate_job(tenant_id, conn)
            if latest and latest["status"] in ("queued", "running"):
                return latest
            if only_if_stale:
                last = await last_proposal(tenant_id, conn)
                fresh = last is not None and last["finished_at"] is not None and await conn.fetchval(
                    "SELECT $1::timestamptz > now() - make_interval(hours => $2)",
                    last["finished_at"], SLATE_STALE_AFTER_HOURS,
                )
                if fresh:
                    return latest
            await enqueue(conn, T7_SLATE_KIND, {"tenant_id": str(tenant_id)},
                          created_by=f"tenant:{tenant_id}")
            return await latest_slate_job(tenant_id, conn)


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

    AA-840 — read-only like before (one extra SELECT). Each Channel carries `enabled` (the
    tenant's channel config, see `_tenant_enabled_channels()`), and a still-`proposed` Subject on a
    Channel the tenant has switched off is not returned (the next `propose_slate()` deletes it);
    its picked/used rows still are.
    """
    async with pool.acquire() as conn:
        enabled = set(await _tenant_enabled_channels(tenant_id, conn))
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
        if r["state"] == "proposed" and r["channel"] not in enabled:
            continue
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
            "enabled": channel in enabled,
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
    per-atom). `atom_id`/`trip_id` still resolve to ONE representative atom (AA-857: for a Segment,
    the member on this tenant's tours whose place is the Subject's place; for a Route, a member of
    its first ordered Segment on the Route's own `tour_id`) — kept for every existing
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
                "(its Segment/Route was rebuilt away, or no atom on your tours is about its place) "
                "— refresh the Slate and pick again."
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
    built from them, still resolve exactly as before.

    AA-857 (S239) — the Segment branch no longer takes `ORDER BY atom_id LIMIT 1`: a Segment can
    hold other places (and, before S218, other countries), so the first atom by id seeded
    "Gyeongju" with a Pakistan museum and "National Library" with a craft school. The member is now
    chosen by `subject_seed.choose_representative()` (place = the Subject's `canonical_place`,
    alias-aware; on this tenant's tours; one country). No such member → `(None, None, None)`, which
    `pick_subject()` turns into SubjectNotEligibleError, and the next proposal does not offer it."""
    if segment_id:
        # AA-857 — the member whose place IS the Subject's place, on this tenant's own tours, in one
        # country (services/acp_shared/subject_seed.py); never `ORDER BY atom_id LIMIT 1`, which
        # seeded "Gyeongju" with a Pakistan museum (S239). No such member → no seed (logged).
        place = await conn.fetchval(
            "SELECT canonical_place FROM acp_contract.atom_segment WHERE segment_id = $1", segment_id,
        )
        members = (await load_segment_members(conn, [segment_id], str(tenant_id))).get(segment_id, [])
        alias_cards = await load_alias_cards(conn, [place] + [m.place for m in members])
        tour_ids = {str(t) for t in await _tenant_tour_ids(tenant_id, conn)}
        member, skip = choose_representative(
            place, members, tenant_tour_ids=tour_ids, alias_cards=alias_cards,
        )
        if member is None:
            logger.warning("slate_subject_no_seed", tenant_id=str(tenant_id), segment_id=segment_id,
                           place=place, reason=skip, members=len(members))
            return None, None, None
        return member.atom_id, str(member.tour_id), None

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
    # AA-857 — the Route's own tour only (one tour = one country); prefer the member that is the
    # Segment's place, else any live member of that tour (T9 seeds a Route from every Segment along
    # the walk, `route_segment_ids`, so this atom is only the atom-grain anchor).
    first = segment_ids[0]
    place = await conn.fetchval(
        "SELECT canonical_place FROM acp_contract.atom_segment WHERE segment_id = $1", first,
    )
    route_tour = str(route["tour_id"])
    members = [m for m in (await load_segment_members(conn, [first], str(tenant_id))).get(first, [])
               if str(m.tour_id) == route_tour]
    member, _skip = choose_representative(place, members, tenant_tour_ids={route_tour})
    if member is None and members:
        member = min(members, key=lambda m: (not (m.evidence or "").strip(), m.atom_id))
    return (member.atom_id, route_tour, list(segment_ids)) if member else (None, None, None)


__all__ = [
    "CHANNEL_BARS", "WEEKLY_RHYTHM_CHANNELS", "ON_DEMAND_CHANNELS", "MAX_CHANNELS_PER_PLACE",
    "Candidate", "propose_slate", "fetch_slate", "pick_subject",
    "T7_SLATE_KIND", "latest_slate_job", "last_proposal", "request_slate_proposal",
    "SubjectNotFoundError", "SubjectNotEligibleError",
]
