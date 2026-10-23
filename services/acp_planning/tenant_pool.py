"""
services.acp_planning.tenant_pool — AA-448, T7's tenant-scoped trip+atom source.

Per the task's explicit decision #3 (STOP-free — no approval needed, this is a direct
instruction): T7 must NOT read from the platform-wide catalog `fetch_trips()`
(`runway.py:205`, deliberately un-tenant-filtered, "every tenant shares the full catalog until
Marketplace/N1 licensing ships" — see AA-448-00 STEP0 §1) or the buggy
`fetch_atoms_by_trip()` (`quarter.py:262`, joins `raw_tours.tenant_id` instead of
`tour_atoms.owner_scope` — the bug AA-448 exists to route around). Instead T7 reads the SAME
source-of-truth as `GET /v1/marketplace` (AA-444, `api/routers/v1_marketplace.py`): a tenant's
own `gold_aa_internal.tenant_tour_versions` (latest version per tour) and their own
`acp_contract.tour_atoms` (`owner_scope = tenant_id`).

**Not a literal call to `GET /v1/marketplace`** — that endpoint returns an aggregate rollup
(atom_count/high_atom_count only) built for a browse UI; `compute_quarter_plan()`/
`compute_slot_grid()` need full `Trip`/`AtomRecord` rows (period, itinerary_source, per-atom
weight/cooldown_until/usage_log, etc.) that an aggregate can't supply. This
module re-derives from the SAME two source tables/join key instead
(`tenant_tour_versions.tenant_id` for trips, `tour_atoms.owner_scope` for atoms) — if the two
queries' idea of "which tenant_tour_versions rows count as this tenant's current tours" ever
needs to change (e.g. adding a status filter), keep `v1_marketplace.py`'s `_MARKETPLACE_QUERY`
and this module's `_TENANT_TRIP_QUERY` in sync; they are intentionally NOT filtering
`ttv.status` today, matching Marketplace's own current behavior exactly (ADR-2026-038 §0.2 — no
AA-side content gate at any T0-T11 step, so an unreviewed/draft rewrite still counts as "this
tenant's tour" for planning purposes, same as Marketplace already treats it).

Atom scope is `owner_scope = tenant_id` only — NOT `owner_scope IN ('platform', tenant_id)`.
This matches `v1_marketplace.py`'s own atom aggregate exactly (its `ac` subquery: `WHERE
owner_scope = $2`), which is the precedent this task explicitly told T7 to follow. AA-440's
`fetch_atoms_by_trip()` bug writeup floated `IN ('platform', tenant_id)` as a *possible* fix
shape for the OLD function, but that was never actually decided/built anywhere (confirmed: AA-444
did not adopt it for Marketplace) — T7 follows the real, already-shipped precedent instead of
that unbuilt speculative alternative.

Reuses `_row_to_trip`/`_row_to_atom` (runway.py/quarter.py's own row-shape parsers, incl.
`_parse_jsonb`'s asyncpg-has-no-jsonb-codec workaround) unchanged — these two new queries return
the exact same column names/types those parsers already expect, so there is no reason to
duplicate that mapping logic.
"""
from __future__ import annotations

from datetime import date
from typing import NamedTuple
from uuid import UUID

from .models import AtomRecord, Trip
from .quarter import _row_to_atom
from .runway import _row_to_trip

# The JOIN into published_tours/raw_tours below reads a shared reference pool (100% sentinel tenant_id =
# aa_internal), not per-tenant data — the real per-tenant boundary is drawn by tenant_tour_versions.tenant_id.
# This is the query behind T7's quarterly reallocation panel (trip_reallocation.py), the T10 review list / detail
# and the T8 trip context. AA-859 wave 4: it runs on the RLS tenant pool too (TRIP_REALLOCATION_FLAG,
# REVIEWS_FLAG): since 239 the tenant role reads the AA master rows of published_tours / raw_tours
# (aa_app_user_aa_master_read), and since 265 tenant_tour_pages through its own policy (own rows + the master's).
# The old "never move this to the RLS pool, AA-578" note predates 239 and no longer holds.
#
# AA-859 wave 4 (C12) — the trip page is one LATERAL row per tour: the tenant's own page first, else the AA
# master's. The old LEFT JOIN on tour_id alone returned one duplicate trip row per tenant with a page for that
# tour (on the main pool). $1 is the tenant as uuid (tenant_tour_versions), $2 the same tenant as text
# (tenant_tour_pages.tenant_id is text): one type per placeholder (the asyncpg lesson of
# content_writing._TENANT_REVIEWS_QUERY).
_MASTER_TENANT_TEXT = "00000000-0000-0000-0000-000000000001"
_TENANT_TRIP_QUERY = f"""
    WITH latest_versions AS (
        SELECT DISTINCT ON (ttv.published_tour_id) ttv.published_tour_id
        FROM gold_aa_internal.tenant_tour_versions ttv
        WHERE ttv.tenant_id = $1::uuid
        ORDER BY ttv.published_tour_id, ttv.version_number DESC
    )
    SELECT
        rt.tour_id AS id, pt.aa_name AS name, rt.country AS destination,
        rt.period AS period, rt.duration AS duration_raw, rt.src_itineraries AS itinerary_source,
        rt.lifecycle_stage AS lifecycle_stage, ttp.url AS trip_url, ttp.url_alive AS url_alive,
        tl.state AS served_state
    FROM latest_versions lv
    JOIN gold_aa_internal.published_tours pt ON pt.id = lv.published_tour_id
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    -- AA-986 L3: the served state of the tour's catalog key (CIS 272: SELECT to cis_app and aa_app_user)
    LEFT JOIN gold_aa_internal.v_tour_liveness tl ON tl.source_id = (COALESCE(rt.source_group_id, rt.tour_id))::text
    LEFT JOIN LATERAL (
        SELECT p.url, p.url_alive
        FROM acp_deliver.tenant_tour_pages p
        WHERE p.tour_id = rt.tour_id AND p.tenant_id IN ($2::text, '{_MASTER_TENANT_TEXT}')
        ORDER BY (p.tenant_id = $2::text) DESC
        LIMIT 1
    ) ttp ON true
"""

_TENANT_ATOM_QUERY = """
    SELECT atom_id, tour_id, text, activity_type, owner_scope,
           deleted, weight, cooldown_until, usage_log
    FROM acp_contract.tour_atoms
    WHERE owner_scope = $1 AND NOT deleted AND NOT is_empty_marker
"""

# AA-494 Decision 6 — the atom-availability rule, worded exactly per
# docs/claude_tasks/AA-494-design-atom-angle-piece-reuse.md ("Atom-availability rule for the
# month"): an atom is "used" (locked) for calendar month X if it has at least one content_piece
# with status='approved' (the only status meaning "passed T10" — content_piece.status's real
# value set as of migration 118 is processing/approved/held/failed, confirmed live 29/08/2026;
# there is no ordinal "beyond approved" state to compare against, so the design doc's "at or
# beyond approved" collapses to exactly status='approved') whose created_at (actual write date,
# NOT the slot's pre-assigned plan month) falls within month X. A piece that never reaches
# approved (held/failed, or still processing) does NOT lock the atom.
#
# Joins through angle_gate_request for atom_id/tenant_id — content_piece itself has no atom_id
# column (by design, migration 115's own header: a child table doesn't copy its parent's fields).
#
# AA-469 Việc 4 (flow-order fix) — `channel` now reads cp.channel (content_piece), NOT
# agr.channel (angle_gate_request), a deliberate switch made THIS session: channel moved to a
# separate step 8 (services/acp_angle_gate/service.py::set_channel()) that can be called more
# than once on the same request while still 'approved' (pick a channel, change your mind, pick a
# different one, before ever writing) — agr.channel is therefore no longer a stable per-piece
# value the way it was when channel was fixed once at request-creation time (pre-this-session).
# cp.channel (migration 124's column, populated by this session's change to
# _insert_placeholder_piece()) is denormalized specifically so a piece's own channel stays
# accurate regardless of what the parent request's channel is later changed to for its NEXT
# write — same reasoning AA-497 already established for angle_gate_option_id on this same table.
# 0 real content_piece rows existed at the time of this change (confirmed live) — no backfill
# needed; every row from here on is written under the new code path and has cp.channel set.
_USED_ATOMS_QUERY = """
    SELECT agr.atom_id, MIN(cp.created_at) AS used_at,
           (ARRAY_AGG(cp.channel ORDER BY cp.created_at))[1] AS channel,
           (ARRAY_AGG(cp.angle_gate_request_id::text ORDER BY cp.created_at DESC))[1] AS request_id
    FROM acp_shared.content_piece cp
    JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
    WHERE agr.tenant_id = $1 AND cp.status = 'approved'
      AND cp.created_at >= $2 AND cp.created_at < $3
    GROUP BY agr.atom_id
"""


class UsedAtom(NamedTuple):
    """One atom locked for a given month — `used_at` is the real write date (the atom's earliest
    approved content_piece in that month), `channel` is the channel of that earliest piece
    (best-effort provenance for the T7 slot-view's "already written" display, not itself part of
    the availability rule).

    AA-497 — `request_id` is the LATEST approved piece's angle_gate_request_id (not the earliest,
    unlike `used_at`/`channel` above), so the T7 slot-view's "Change angle" action reopens
    whichever request most recently produced a real, approved piece for this atom — the one a
    tenant editing this slot actually means. Edge case, not solved here: nothing in this codebase
    prevents create_request() from opening a genuinely SEPARATE request for the same (atom_id,
    channel) pair (no uniqueness check ever existed) — if that ever happens, this picks the most
    recently-written request among possibly-several, which is the best available default without
    a broader "which request owns this atom+channel" model change (out of AA-497's scope)."""
    used_at: str
    channel: str
    request_id: str


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


async def fetch_tenant_trips_on(conn, tenant_id: UUID) -> list[Trip]:
    """fetch_tenant_trips() on the caller's connection (AA-859 wave 4: the tenant pool's block)."""
    rows = await conn.fetch(_TENANT_TRIP_QUERY, tenant_id, str(tenant_id))
    return [_row_to_trip(r) for r in rows]


async def fetch_tenant_trips(tenant_id: UUID, pool) -> list[Trip]:
    """Replaces `runway.fetch_trips()` for T7 — one tenant's own rewritten tours only (via
    `tenant_tour_versions`), not the platform-wide 763-trip catalog."""
    async with pool.acquire() as conn:
        return await fetch_tenant_trips_on(conn, tenant_id)


async def fetch_tenant_atoms_by_trip_on(conn, tenant_id: UUID) -> dict[UUID, list[AtomRecord]]:
    """fetch_tenant_atoms_by_trip() on the caller's connection (AA-859 wave 4: the tenant pool's block, where
    266 shows the tenant role its own atoms and the platform's; this query reads its own only)."""
    rows = await conn.fetch(_TENANT_ATOM_QUERY, str(tenant_id))
    by_trip: dict[UUID, list[AtomRecord]] = {}
    for r in rows:
        atom = _row_to_atom(r)
        by_trip.setdefault(atom.trip_id, []).append(atom)
    return by_trip


async def fetch_tenant_atoms_by_trip(tenant_id: UUID, pool) -> dict[UUID, list[AtomRecord]]:
    """Replaces `quarter.fetch_atoms_by_trip()` for T7 — `owner_scope = tenant_id`, not the
    buggy `raw_tours.tenant_id` join. `tenant_id` is passed as a plain str/UUID positional
    param (matches `tour_atoms.owner_scope`'s column type — free-text per ADR-2026-038 Hướng B,
    not a UUID FK — asyncpg will stringify a UUID param automatically for a text column)."""
    async with pool.acquire() as conn:
        return await fetch_tenant_atoms_by_trip_on(conn, tenant_id)


async def fetch_used_atom_ids(tenant_id: UUID, year: int, month: int, pool) -> dict[str, UsedAtom]:
    """AA-494 Decision 6 — the atom-availability rule. Returns every atom_id "used" (locked) for
    calendar month `year`-`month`, keyed to its real write date + channel (see `_USED_ATOMS_QUERY`
    above for the exact rule). Callers (T7's slot-view, T8's free-atom picker) treat any atom_id
    NOT in this dict's keys as free for the month."""
    start, end = _month_bounds(year, month)
    async with pool.acquire() as conn:
        rows = await conn.fetch(_USED_ATOMS_QUERY, tenant_id, start, end)
    return {
        r["atom_id"]: UsedAtom(used_at=r["used_at"].isoformat(), channel=r["channel"], request_id=r["request_id"])
        for r in rows
    }


__all__ = ["fetch_tenant_trips", "fetch_tenant_trips_on", "fetch_tenant_atoms_by_trip", "fetch_tenant_atoms_by_trip_on",
           "fetch_used_atom_ids", "UsedAtom"]
