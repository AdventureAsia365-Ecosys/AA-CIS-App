"""services.acp_shared.route_guard — AA-914 (S245): a blog request's Route Segments, read safely.

`acp_shared.angle_gate_request.route_segment_ids` is a frozen snapshot of the Route at pick time
(`slate.pick_subject`), cloned verbatim by the admin regenerate. Between 08 and 11/10/2026 Segment
ids were shared and relabelled across countries (the AA-857 collision class), so six old requests
kept ids that now hold another tour's Segment ("Dal Lake | shikara ride" on a Mongolia blog). T9
already skipped such a Segment's atom text (its query needs the trip's own member atom), but the
Segment itself still flowed: into the T8 real-question chain (its `atom_matches` questions and its
`canonical_place`), into `route_segment_count`, and forward into every regenerated request.

One rule, applied by every reader (T8 angles, T9 write, Slate pick, admin regenerate):

  a Route Segment counts for a request only when, after following a merged id to its live id
  (`atom_segment_alias`, `resolve_segment_aliases`), the request's trip has a live member atom in
  it — the same member test `content_writing._fetch_route_segments` uses for the atom text.

Every dropped id is logged (`route_segment_dropped`: request, Segment id, live id, reason). A read
error is not swallowed: the caller's job fails and retries, never writes from an unguarded list.
`current_route` is the one read of a Route for a pick / regenerate: only the current version
(`superseded_at IS NULL`); a superseded Route is never used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

import structlog

from services.acp_contract.segment_matching import resolve_segment_aliases

logger = structlog.get_logger()

# Why a stored Route Segment id was dropped.
DROP_NO_TRIP = "no_trip_id"                  # the request has no trip, so no Segment can be checked
DROP_UNKNOWN = "unknown_segment"             # the (live) id is not an atom_segment row at all
DROP_NOT_MEMBER = "trip_has_no_live_atom"    # the trip has no live, non-alias member atom in it
DROP_DUPLICATE = "duplicate_after_alias"     # two stored ids resolve to the same live Segment


@dataclass(frozen=True)
class SegmentDrop:
    segment_id: str          # the id as stored in the snapshot
    live_id: Optional[str]   # where it resolves through atom_segment_alias (itself when never merged)
    reason: str


def parse_segment_ids(value) -> list[str]:
    """`route_segment_ids` as a list of non-empty ids: the column arrives as a JSON string (no jsonb
    codec on the app's connections), a list once parsed, or NULL."""
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value) if value.strip() else []
    return [str(s) for s in value or [] if s]


def filter_route_segments(
    segment_ids: Sequence[str], live_ids: Mapping[str, str], known: Iterable[str], trip_members: Iterable[str],
) -> tuple[list[str], list[SegmentDrop]]:
    """Pure core of the guard. `live_ids` maps a stored id to its live id (missing = itself),
    `known` are the live ids that exist in `atom_segment`, `trip_members` the live ids the trip has
    a live member atom in. Returns (kept live ids in snapshot order, drops in snapshot order)."""
    known, trip_members = set(known), set(trip_members)
    kept: list[str] = []
    drops: list[SegmentDrop] = []
    for sid in segment_ids:
        live = live_ids.get(sid, sid)
        if live not in known:
            drops.append(SegmentDrop(sid, live, DROP_UNKNOWN))
        elif live not in trip_members:
            drops.append(SegmentDrop(sid, live, DROP_NOT_MEMBER))
        elif live in kept:
            drops.append(SegmentDrop(sid, live, DROP_DUPLICATE))
        else:
            kept.append(live)
    return kept, drops


# Same member test as content_writing._ROUTE_SEGMENT_TEXT_QUERY (live atom of this trip, platform or
# this tenant's own, not an alias member), answered for every Segment of the snapshot in one read.
_TRIP_MEMBER_SQL = """
    SELECT s.segment_id,
           EXISTS (
               SELECT 1 FROM acp_contract.atom_segment_member m
               JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id
               WHERE m.segment_id = s.segment_id AND ta.tour_id = $2::uuid
                 AND ta.owner_scope IN ('platform', $3::text)
                 AND NOT m.is_alias AND NOT ta.deleted AND NOT ta.is_empty_marker
           ) AS trip_member
    FROM acp_contract.atom_segment s
    WHERE s.segment_id = ANY($1::text[])
"""


async def trip_route_segment_ids(
    conn, trip_id, segment_ids, *, tenant_id=None, request_id=None, consumer: str = "",
) -> list[str]:
    """The request's Route Segments the trip really has: stored ids resolved to their live id,
    kept only when the trip has a live member atom in that Segment, in snapshot order, without
    duplicates. Each dropped id is logged with the reason. `consumer` names the reader in the log
    (t8_angles, t9_write, slate_pick, admin_regenerate)."""
    ids = parse_segment_ids(segment_ids)
    if not ids:
        return []
    log = logger.bind(request_id=str(request_id) if request_id else None,
                      trip_id=str(trip_id) if trip_id else None, consumer=consumer)
    if not trip_id:
        for sid in ids:
            log.warning("route_segment_dropped", segment_id=sid, live_id=None, reason=DROP_NO_TRIP)
        return []
    live_ids = await resolve_segment_aliases(conn, ids)
    rows = await conn.fetch(_TRIP_MEMBER_SQL, sorted(set(live_ids.values())), str(trip_id),
                            str(tenant_id) if tenant_id else "")
    kept, drops = filter_route_segments(
        ids, live_ids, known=[r["segment_id"] for r in rows],
        trip_members=[r["segment_id"] for r in rows if r["trip_member"]],
    )
    for d in drops:
        log.warning("route_segment_dropped", segment_id=d.segment_id, live_id=d.live_id, reason=d.reason)
    moved = {s: live_ids[s] for s in ids if live_ids.get(s, s) != s}
    if moved:
        log.info("route_segment_alias_resolved", moved=moved)
    return kept


async def request_route_segment_ids(pool, req, *, tenant_id, consumer: str) -> list[str]:
    """`trip_route_segment_ids` for one angle_gate_request row or dict (the T8 and T9 entry point)."""
    if not parse_segment_ids(req.get("route_segment_ids")):
        return []
    async with pool.acquire() as conn:
        return await trip_route_segment_ids(
            conn, req.get("trip_id"), req.get("route_segment_ids"), tenant_id=tenant_id,
            request_id=req.get("request_id"), consumer=consumer,
        )


async def current_route(conn, route_id: Optional[str]) -> Optional[dict]:
    """{tour_id, segment_ids} of the Route `route_id` when it is the CURRENT version
    (`superseded_at IS NULL`); None when it is superseded or missing. A Subject on a superseded
    Route is gone from the next Slate proposal anyway (slate.propose_slate deletes it)."""
    if not route_id:
        return None
    row = await conn.fetchrow(
        "SELECT tour_id, ordered_segment_ids FROM acp_contract.route "
        "WHERE route_id = $1 AND superseded_at IS NULL", route_id,
    )
    if row is None:
        return None
    return {"tour_id": str(row["tour_id"]), "segment_ids": parse_segment_ids(row["ordered_segment_ids"])}


__all__ = [
    "DROP_NO_TRIP", "DROP_UNKNOWN", "DROP_NOT_MEMBER", "DROP_DUPLICATE", "SegmentDrop",
    "parse_segment_ids", "filter_route_segments", "trip_route_segment_ids", "request_route_segment_ids",
    "current_route",
]
