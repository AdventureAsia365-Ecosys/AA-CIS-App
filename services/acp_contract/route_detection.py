"""services/acp_contract/route_detection.py — AA-510, Route/Hub detection + route_pick snapshot.

(`create_subject()`/`acp_contract.subject` renamed to `create_route_pick()`/
`acp_contract.route_pick` at AA-511 STEP0, migration 132 — freed the name `subject` for the
unrelated Slate-proposal concept `acp_shared.subject`.)

Ported from Ms. Thư's aa-social-media `src/aa_social/routes.py` (`derive_routes()`/`families()`/
`stops()`) and `stages/score.py`'s `_store_routes()` (rebuild-whole persistence). Full evidence
and every deviation from the origin/build prompt, disclosed not silent: docs/claude_audit/
AA-510-step0-route-hub-investigation.md, docs/implementation-notes/AA-510.md.

**A Route is one tour's ordered run of ranked, non-excluded Segments** — Magome, then the pass,
then Tsumago. Built from whatever `atom_ranking` (AA-515) last ranked, so ADR 0019/0020's
transit/unnamed-place exclusion is inherited, not re-applied here. A run breaks where a day has
nothing ranked on it (usually the transfer); a run of <2 days or <2 places is not a journey —
that stays a Segment.

**A Hub is the journey a family of Routes tells** — six Nakasendo itineraries are six sales of
one Hub, not six unrelated Routes. Family membership is measured in shared ranked Segments
(Jaccard over the SMALLER tour's set, >= SHARED_ENOUGH) because two tours sharing a region
without sharing a week are not one journey.

Grain is the Segment (AA-515's own grain), not the Atom (the origin's grain) — implementation
notes Decision 4.
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

# A journey is at least 2 days and 2 places -- one day is a moment, two days in one place is a
# stay, both already a Segment's job (ported verbatim, routes.py:36-37).
LEAST_DAYS = 2
LEAST_PLACES = 2

# At most 5 -- a run of ranked days is often most of the itinerary; a Blog piece cannot walk 13
# days and a reader would not follow it. Longer runs are cut into consecutive spans, in day
# order, never reduced to the strongest few days (ported verbatim, routes.py:39-45).
MOST_DAYS = 5

# How much of the smaller tour's ranked Segment set two tours must share before they are one
# journey -- measured on Ms. Thư's own Japan export at 0.3 (routes.py:47-53). AA-CIS has no
# equivalent catalog to re-measure against yet; kept as the starting point, a named constant so
# it is never inlined (build prompt: "config, không hardcode"). Reused for BOTH family-forming
# (route-to-route grouping by tour) and Hub-reuse matching (implementation notes Decision 6) --
# one threshold, not two unvalidated magic numbers.
SHARED_ENOUGH = 0.3


@dataclass(frozen=True)
class Moment:
    """One ranked, non-excluded Segment, as a Route needs to see it — for ONE tour.

    AA-545 — no `score` field. Route composition (which days/Segments form a journey) never
    read `atom_ranking.total_rank` values, only `excluded_reason IS NULL` membership (STEP0 Q2)
    — and that flag doesn't vary by market (`classify_exclusion()` is pure place/action text, no
    demand signal), so Route needs no market context to build a platform-wide-correct Route at
    all. Ordering/scoring for DISPLAY is computed at read time instead (`services/acp_shared/
    slate.py`, AA-545 Q3).
    """

    segment_id: str
    tour_id: str
    day: int
    place: str


@dataclass(frozen=True)
class Route:
    """Consecutive days of one tour, and the ranked Segments along them. Platform-wide (AA-545)
    — no `tenant_id`, no `score` (see `Moment`'s own docstring)."""

    route_id: str
    tour_id: str
    first_day: int
    last_day: int
    segment_ids: tuple[str, ...]
    places: tuple[str, ...]
    hub_name: str = ""
    hub_id: str | None = None

    @property
    def days(self) -> int:
        return self.last_day - self.first_day + 1


def derive_routes(moments: Iterable[Moment]) -> list[Route]:
    """Every journey in the platform's ranked inventory. Same moments in, same Routes out.

    AA-545 — platform-wide (no `tenant_id` in `route_id`), no `score`. Sorted by `route_id`
    alone (was: score-then-route_id) — deterministic, but carries no ranking meaning of its own;
    a reader wanting "best first" computes that itself from `atom_ranking` for its own market
    (`hub_id`/`hub_name` are resolved separately, after family detection — every Route here
    starts with `hub_name=""`).
    """
    by_tour: dict[str, list[Moment]] = {}
    for moment in moments:
        by_tour.setdefault(moment.tour_id, []).append(moment)

    routes: list[Route] = []
    for tour_id, held in sorted(by_tour.items()):
        for run in _spans(_runs(sorted(held, key=lambda m: (m.day, m.segment_id)))):
            places = tuple(dict.fromkeys(moment.place for moment in run))
            days = {moment.day for moment in run}
            if len(days) < LEAST_DAYS or len(places) < LEAST_PLACES:
                continue
            first, last = min(days), max(days)
            routes.append(Route(
                route_id=f"{tour_id}:{first}-{last}",
                tour_id=tour_id,
                first_day=first,
                last_day=last,
                segment_ids=tuple(m.segment_id for m in run),
                places=places,
            ))
    return sorted(routes, key=lambda route: route.route_id)


def _runs(held: list[Moment]) -> list[list[Moment]]:
    """Moments split into consecutive-day runs. A gap is a day ranking left empty — almost
    always the transfer (ported verbatim, routes.py:119-130)."""
    runs: list[list[Moment]] = []
    for moment in held:
        if runs and moment.day - runs[-1][-1].day <= 1:
            runs[-1].append(moment)
        else:
            runs.append([moment])
    return runs


def _spans(runs: list[list[Moment]]) -> list[list[Moment]]:
    """Runs cut to a length a piece can walk. A trailing short span joins the one before it
    rather than being dropped (ported verbatim, routes.py:133-150)."""
    spans: list[list[Moment]] = []
    for run in runs:
        days = sorted({moment.day for moment in run})
        cuts = [days[at:at + MOST_DAYS] for at in range(0, len(days), MOST_DAYS)]
        if len(cuts) > 1 and len(cuts[-1]) < LEAST_DAYS:
            cuts[-2] = cuts[-2] + cuts[-1]
            cuts.pop()
        for cut in cuts:
            within = set(cut)
            spans.append([moment for moment in run if moment.day in within])
    return spans


def families(tours: Mapping[str, set[str]], share: float = SHARED_ENOUGH) -> dict[str, str]:
    """Which tours sell one journey, as tour_id -> family key (the alphabetically-smallest
    member's tour_id). A tour sharing nothing with any other is not in the map — a family of
    one is not a family (ported verbatim algorithm, routes.py:153-190, `trip_code` -> `tour_id`).
    """
    parent = {tour: tour for tour in tours}

    def find(tour: str) -> str:
        while parent[tour] != tour:
            parent[tour] = parent[parent[tour]]
            tour = parent[tour]
        return tour

    ordered = sorted(tours)
    for index, one in enumerate(ordered):
        for other in ordered[index + 1:]:
            smaller = min(len(tours[one]), len(tours[other]))
            if not smaller:
                continue
            if len(tours[one] & tours[other]) / smaller >= share:
                left, right = find(one), find(other)
                if left != right:
                    parent[max(left, right)] = min(left, right)

    grouped: dict[str, list[str]] = {}
    for tour in ordered:
        grouped.setdefault(find(tour), []).append(tour)
    return {
        tour: min(members)
        for members in grouped.values()
        if len(members) > 1
        for tour in members
    }


@dataclass(frozen=True)
class Stop:
    """One place on one day, and everything that happens there — presentation only, used to
    make a Subject snapshot human-readable without a live join (ported verbatim, routes.py:
    193-227)."""

    day: int
    place: str
    actions: tuple[str, ...] = ()

    @property
    def said(self) -> str:
        doing = [one for one in self.actions if one]
        if not doing:
            return ""
        if len(doing) == 1:
            return doing[0]
        return ", ".join(doing[:-1]) + " and " + doing[-1]

    def __str__(self) -> str:
        said = self.said
        return f"day {self.day} {self.place}" + (f" — {said}" if said else "")


def stops(steps: Iterable[tuple[int, str, str]]) -> list[Stop]:
    """A journey as it is shown. Order is the order given. A place named twice on one day is
    one Stop with 2 actions; a place revisited on a later day is a second Stop (ported verbatim,
    routes.py:230-248)."""
    held: dict[tuple[int, str], list[str]] = {}
    order: list[tuple[int, str]] = []
    for day, place, action in steps:
        key = (day, place)
        if key not in held:
            held[key] = []
            order.append(key)
        if action and action not in held[key]:
            held[key].append(action)
    return [Stop(day=d, place=p, actions=tuple(held[(d, p)])) for d, p in order]


def journey_name(places: Sequence[str], limit: int = 4) -> str:
    """A readable placeholder Hub/Route name from a day-ordered place sequence —
    "Kyoto → Magome → Tsumago". NOT marketer-authored copy (no naming/rename UI exists yet,
    implementation notes Decision 9) — a disclosed placeholder for CONTEXT.md's "named as a
    traveller would say it", capped at `limit` places so a long itinerary doesn't produce an
    unreadable name.
    """
    trimmed = list(dict.fromkeys(places))[:limit]
    return " → ".join(trimmed) if trimmed else "Untitled journey"


# ── DB-facing wrappers (impure) ─────────────────────────────────────────────────────────────

async def run_route_detection(pool) -> dict:
    """Rebuild acp_contract.route platform-wide — VERSIONED (AA-532), not DELETE+INSERT-whole
    (AA-510's original behavior, matching the origin's own `_store_routes()`, "derived, never
    accumulated"). Changed 05/09/2026 after a real, live FK violation: `acp_shared.subject.
    route_id` (migration 133, AA-511) is a real FK into this table with NO ACTION on delete — a
    tenant with an active Subject picking a Route that a re-run then deletes made the whole
    rebuild fail (docs/implementation-notes/AA-532.md has the full trace).

    AA-545 — no `tenant_id` parameter, no per-tenant scoping: a route's identity is now
    `(tour_id, first_day, last_day)` alone. Reads platform-wide `atom_ranking`/`atom_segment`/
    `route`/`hub`; needs no market context at all (STEP0 Q2 confirmed composition only depends on
    `excluded_reason IS NULL` membership, which doesn't vary by market — see `Moment`'s own
    docstring). Per identity, this run:
      - leaves it alone if the newly-derived Route is byte-for-byte the same as the current row
        (no write at all — "tránh version rác mỗi lần chạy", the build prompt's own ask);
      - supersedes the current row (`superseded_at = now()`, never deleted) and inserts a new
        current row (`version` bumped, `route_id` gains a `:v{n}` suffix for n>=2) if the content
        changed;
      - supersedes the current row with no replacement if this run's derivation no longer
        produces anything for that identity at all (the journey stopped qualifying);
      - inserts a brand-new version-1 row (unsuffixed `route_id`, same deterministic composite
        key AA-510 always used) for an identity that never existed before.
    `acp_contract.hub` is still never rebuilt the same way (unaffected by this change — it
    already persists and is matched/reused, implementation notes Decision 10 from AA-510).

    Reads only non-excluded (`excluded_reason IS NULL`) atom_ranking rows — the transit/
    unnamed-place gate (ADR 0019/0020) already ran one layer down (AA-515) and is not
    re-applied here. Dedupes the per-market fan-out (a (tour_id, segment_id) pair now has up to
    6 `atom_ranking` rows, one per finite market) down to distinct ranked pairs FIRST — exclusion
    doesn't vary by market, so any one of them agrees.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ar.segment_id, ar.tour_id::text AS tour_id,
                   asg.canonical_place, asg.canonical_action,
                   MIN(ta.itinerary_day) AS day
            FROM (
                SELECT DISTINCT segment_id, tour_id FROM acp_contract.atom_ranking
                WHERE excluded_reason IS NULL
            ) ar
            JOIN acp_contract.atom_segment asg ON asg.segment_id = ar.segment_id
            JOIN acp_contract.atom_segment_member asm ON asm.segment_id = ar.segment_id
            JOIN acp_contract.v_active_tour_atoms ta
                ON ta.atom_id = asm.atom_id AND ta.tour_id = ar.tour_id
            WHERE ta.itinerary_day IS NOT NULL
            GROUP BY ar.segment_id, ar.tour_id, asg.canonical_place, asg.canonical_action
        """)

        old_hubs = await conn.fetch("""
            SELECT h.hub_id, h.hub_name, array_agg(DISTINCT r.tour_id::text) AS tour_ids
            FROM acp_contract.hub h
            JOIN acp_contract.route r ON r.hub_id = h.hub_id AND r.superseded_at IS NULL
            GROUP BY h.hub_id, h.hub_name
        """)

        # AA-532 — the CURRENT route per identity (tour_id, first_day, last_day), to diff this
        # run's fresh derivation against instead of blindly deleting everything.
        current_routes = await conn.fetch("""
            SELECT route_id, tour_id::text AS tour_id, hub_id, hub_name, ordered_segment_ids,
                   first_day, last_day, version
            FROM acp_contract.route
            WHERE superseded_at IS NULL
        """)
        # AA-723 — the highest version ever used for each identity, INCLUDING superseded rows.
        # A tour that was deactivated then reactivated (AA-713) superseded its routes; when it
        # comes back the identity is "new" in current_routes, but the raw route_id ({tour}:{fd}-
        # {ld}) still exists as a superseded row, so inserting it again violated route_pkey. This
        # lets the new-identity insert pick the next free version instead of colliding.
        max_version_rows = await conn.fetch("""
            SELECT tour_id::text AS tour_id, first_day, last_day, max(version) AS max_version
            FROM acp_contract.route
            GROUP BY tour_id, first_day, last_day
        """)

    moments = [
        Moment(segment_id=r["segment_id"], tour_id=r["tour_id"], day=r["day"],
               place=r["canonical_place"])
        for r in rows
    ]
    tour_segments: dict[str, set[str]] = defaultdict(set)
    tour_steps: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for r in rows:
        tour_segments[r["tour_id"]].add(r["segment_id"])
        tour_steps[r["tour_id"]].append((r["day"], r["canonical_place"], r["canonical_action"]))

    routes = derive_routes(moments)
    family_of = families(dict(tour_segments), SHARED_ENOUGH)

    routes_by_tour: dict[str, list[Route]] = defaultdict(list)
    for r in routes:
        routes_by_tour[r.tour_id].append(r)

    grouped: dict[str, set[str]] = defaultdict(set)
    for tour_id, key in family_of.items():
        grouped[key].add(tour_id)
    # Only pursue Hub resolution for families that actually produced >=1 Route this run — a
    # family whose every member failed the LEAST_DAYS/LEAST_PLACES gate has nothing to attach
    # a Hub to; skipping it avoids minting an immediately-orphaned Hub row for no reason.
    grouped = {k: v for k, v in grouped.items() if any(t in routes_by_tour for t in v)}

    old_hub_tours = {str(r["hub_id"]): set(r["tour_ids"]) for r in old_hubs}
    old_hub_names = {str(r["hub_id"]): r["hub_name"] for r in old_hubs}

    resolved_hub: dict[str, tuple[str, str]] = {}  # family_key -> (hub_id, hub_name)
    hubs_created = 0
    hubs_reused = 0
    async with pool.acquire() as conn:
        for family_key, member_tours in grouped.items():
            # Primary signal: which real tours a hub covers — durable across a Segment-level
            # reshuffle, unlike segment_id sets. hub_name equality is checked as a tie-break
            # only when tour-set overlap alone doesn't clear the bar (implementation notes
            # Decision 7).
            best_hub_id, best_ratio = None, 0.0
            for hub_id, old_tours in old_hub_tours.items():
                smaller = min(len(member_tours), len(old_tours))
                if not smaller:
                    continue
                ratio = len(member_tours & old_tours) / smaller
                if ratio > best_ratio:
                    best_hub_id, best_ratio = hub_id, ratio

            candidates = routes_by_tour.get(family_key, [])
            if candidates:
                # AA-545 Q3 condition 3 — market-independent tie-break (was: min by score,
                # which no longer exists on Route at all): most member Segments first, then
                # alphabetically by route_id for a total, deterministic order.
                canonical_places = list(
                    min(candidates, key=lambda r: (-len(r.segment_ids), r.route_id)).places
                )
            else:
                canonical_places = [
                    p for _d, p, _a in sorted(tour_steps.get(family_key, []))
                ]
            name = journey_name(canonical_places)

            if best_hub_id is not None and best_ratio >= SHARED_ENOUGH:
                # hub_name is deliberately NOT overwritten on reuse (Decision 8) — a future
                # marketer rename must survive a rebuild that still regroups the same tours.
                resolved_hub[family_key] = (best_hub_id, old_hub_names[best_hub_id])
                await conn.execute(
                    "UPDATE acp_contract.hub SET updated_at = now() WHERE hub_id = $1::uuid",
                    best_hub_id,
                )
                hubs_reused += 1
            else:
                new_hub_id = await conn.fetchval("""
                    INSERT INTO acp_contract.hub (hub_name)
                    VALUES ($1) RETURNING hub_id
                """, name)
                resolved_hub[family_key] = (str(new_hub_id), name)
                hubs_created += 1

    finished: list[Route] = []
    for route in routes:
        family_key = family_of.get(route.tour_id)
        if family_key is not None and family_key in resolved_hub:
            hub_id, hub_name = resolved_hub[family_key]
            finished.append(replace(route, hub_id=hub_id, hub_name=hub_name))
        else:
            # Standalone tour — "a family of one is not a family" (origin's own rule): no Hub
            # row created or reused, hub_name is a per-route placeholder from its own places.
            finished.append(replace(
                route, hub_id=None, hub_name=journey_name(list(route.places)),
            ))

    # AA-532 — diff this run's fresh derivation against the CURRENT row per identity
    # (tour_id, first_day, last_day) instead of deleting everything. `existing` keys off the
    # same identity `finished` Routes key off, so a Route with unchanged content is left alone
    # untouched (no supersede, no insert) and a Subject pointing at ANY current or superseded
    # route_id keeps resolving — nothing in this table is ever deleted.
    existing_by_identity = {
        (r["tour_id"], r["first_day"], r["last_day"]): r for r in current_routes
    }
    finished_by_identity = {
        (r.tour_id, r.first_day, r.last_day): r for r in finished
    }
    # AA-723 — max version ever used per identity (incl. superseded), for the reactivation case.
    max_version_by_identity = {
        (r["tour_id"], r["first_day"], r["last_day"]): r["max_version"] for r in max_version_rows
    }

    def _unchanged(route: Route, old: object) -> bool:
        old_segments = old["ordered_segment_ids"]
        if isinstance(old_segments, str):
            old_segments = json.loads(old_segments)
        return (
            list(route.segment_ids) == list(old_segments)
            and route.hub_id == (str(old["hub_id"]) if old["hub_id"] else None)
            and route.hub_name == old["hub_name"]
        )

    to_supersede: list[str] = []  # route_id of every current row this run replaces or removes
    to_insert: list[tuple] = []   # (route_id, tour_id, hub_id, hub_name, segment_ids json,
    #                                first_day, last_day, version)
    unchanged_count = 0

    for identity, route in finished_by_identity.items():
        old = existing_by_identity.get(identity)
        if old is None:
            # New in the CURRENT set. If this identity was never seen at all, keep the raw
            # route_id (version 1). If it existed before and was fully superseded (AA-713
            # deactivate->reactivate), the raw route_id row still lives superseded — reuse it as a
            # new version instead of colliding on route_pkey (AA-723).
            prior_max = max_version_by_identity.get(identity)
            if prior_max is None:
                to_insert.append((
                    route.route_id, route.tour_id, route.hub_id, route.hub_name,
                    json.dumps(list(route.segment_ids)), route.first_day, route.last_day, 1,
                ))
            else:
                new_version = prior_max + 1
                to_insert.append((
                    f"{route.route_id}:v{new_version}", route.tour_id, route.hub_id,
                    route.hub_name, json.dumps(list(route.segment_ids)),
                    route.first_day, route.last_day, new_version,
                ))
        elif _unchanged(route, old):
            unchanged_count += 1
        else:
            to_supersede.append(old["route_id"])
            # Next version off the max EVER used for this identity (incl. superseded), not just
            # the current row's version, so the new route_id can never collide with an old one.
            new_version = max(old["version"], max_version_by_identity.get(identity, old["version"])) + 1
            versioned_id = f"{route.route_id}:v{new_version}"
            to_insert.append((
                versioned_id, route.tour_id, route.hub_id, route.hub_name,
                json.dumps(list(route.segment_ids)), route.first_day, route.last_day,
                new_version,
            ))

    # An identity that existed before but this run's derivation no longer produces at all — the
    # journey stopped qualifying (e.g. its Segments dropped below LEAST_DAYS/LEAST_PLACES).
    # Superseded, never deleted, same as a changed one.
    for identity, old in existing_by_identity.items():
        if identity not in finished_by_identity:
            to_supersede.append(old["route_id"])

    async with pool.acquire() as conn:
        async with conn.transaction():
            if to_supersede:
                await conn.execute(
                    "UPDATE acp_contract.route SET superseded_at = now() "
                    "WHERE route_id = ANY($1::text[])",
                    to_supersede,
                )
            if to_insert:
                await conn.executemany("""
                    INSERT INTO acp_contract.route
                        (route_id, tour_id, hub_id, hub_name, ordered_segment_ids,
                         first_day, last_day, version)
                    VALUES ($1, $2::uuid, $3::uuid, $4, $5::jsonb, $6, $7, $8)
                """, to_insert)

    return {
        "routes_written": len(to_insert),
        "routes_superseded": len(to_supersede),
        "routes_unchanged": unchanged_count,
        "hubs_created": hubs_created,
        "hubs_reused": hubs_reused,
        "families_found": len(grouped),
        "tours_ranked": len(tour_segments),
    }


async def create_route_pick(
    tenant_id: str, route_id: str, pool, selected_by: str | None = None,
) -> dict | None:
    """Snapshot one Route into a route_pick at the moment a marketer picks it (ADR 0024) — no
    live FK, ever, into route.route_id (the same lesson the origin's own Subject layer learned
    the hard way, docs/adr/0024-a-subject-outlives-the-segment-it-came-from.md, applied one
    layer up here).

    Named `create_subject()`/`acp_contract.subject` at AA-510; renamed here (AA-511 STEP0,
    migration 132) to free the name `subject` for the unrelated Slate-proposal concept
    `acp_shared.subject` this issue builds — the two are a different grain/purpose entirely, not
    a compatibility rename.

    Returns None if the Route no longer exists, OR (AA-532) if it exists but has since been
    superseded by a newer version — the caller's job to surface as "pick again", not this
    function's; a marketer should never snapshot a Route re-detection has already moved past
    even though the old row itself is never deleted. Re-joins the underlying Segments at snapshot
    time (best-effort — a partial/empty join degrades the snapshot's `stops` detail but never
    fails route_pick creation) so the snapshot is a human-readable, self-sufficient record that no
    longer depends on anything staying in place afterward.

    `tenant_id` here is the PICKING tenant, written onto the (still per-tenant, unchanged by
    AA-545) `route_pick` row it creates — NOT a filter on `route` itself, which is platform-wide
    and carries no tenant column at all.
    """
    async with pool.acquire() as conn:
        route = await conn.fetchrow("""
            SELECT route_id, tour_id, hub_id, hub_name, ordered_segment_ids,
                   first_day, last_day
            FROM acp_contract.route
            WHERE route_id = $1 AND superseded_at IS NULL
        """, route_id)
        if not route:
            return None

        segment_ids = route["ordered_segment_ids"]
        if isinstance(segment_ids, str):
            segment_ids = json.loads(segment_ids)

        step_rows = await conn.fetch("""
            SELECT asg.segment_id, asg.canonical_place, asg.canonical_action,
                   MIN(ta.itinerary_day) AS day
            FROM acp_contract.atom_segment asg
            JOIN acp_contract.atom_segment_member asm ON asm.segment_id = asg.segment_id
            JOIN acp_contract.tour_atoms ta
                ON ta.atom_id = asm.atom_id AND ta.tour_id = $1::uuid
            WHERE asg.segment_id = ANY($2::text[])
            GROUP BY asg.segment_id, asg.canonical_place, asg.canonical_action
        """, route["tour_id"], segment_ids)

        snapshot = _build_snapshot(route, segment_ids, step_rows)
        route_pick_id = await conn.fetchval("""
            INSERT INTO acp_contract.route_pick (tenant_id, hub_name, route_snapshot, selected_by)
            VALUES ($1::uuid, $2, $3::jsonb, $4)
            RETURNING route_pick_id
        """, tenant_id, route["hub_name"], json.dumps(snapshot), selected_by)

    return {
        "route_pick_id": str(route_pick_id),
        "hub_name": route["hub_name"],
        "route_snapshot": snapshot,
    }


def _build_snapshot(route, segment_ids: list[str], step_rows) -> dict:
    by_day = sorted(
        (r["day"], r["canonical_place"], r["canonical_action"] or "")
        for r in step_rows if r["day"] is not None
    )
    resolved_stops = stops(by_day)
    return {
        "route_id": route["route_id"],
        "tour_id": str(route["tour_id"]),
        "hub_id": str(route["hub_id"]) if route["hub_id"] else None,
        "hub_name": route["hub_name"],
        "ordered_segment_ids": segment_ids,
        "first_day": route["first_day"],
        "last_day": route["last_day"],
        "places": list(dict.fromkeys(s.place for s in resolved_stops)),
        "stops": [
            {"day": s.day, "place": s.place, "actions": list(s.actions)}
            for s in resolved_stops
        ],
    }


__all__ = [
    "Moment", "Route", "Stop",
    "LEAST_DAYS", "LEAST_PLACES", "MOST_DAYS", "SHARED_ENOUGH",
    "derive_routes", "families", "stops", "journey_name",
    "run_route_detection", "create_route_pick",
]
