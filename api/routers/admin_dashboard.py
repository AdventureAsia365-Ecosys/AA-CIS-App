"""
api/routers/admin_dashboard.py — AA-527 (bổ sung, 05/09/2026), reworked AA-551 (07/09/2026).

AA-551 STEP0 finding (docs/investigation/AA-550-admin-ui-real-audit.md): `tour_id` was hard-
required (`Query(...)`) on all 4 endpoints below, which meant Segment/Score/Route could never
show anything in "All tours" mode — directly contradicting AA-545's own platform-wide redesign
(these 3 tables dropped `tenant_id` entirely that same day, but stayed hard-scoped to one Tour at
the API layer regardless). `tour_id` is now OPTIONAL on `segments`/`score`/`routes` — omitting it
returns every tour's matching rows (each tagged with its own `tour_id`/`tour_name`, paginated),
which is what `/admin/atom-curation`'s rebuilt 01-05 platform-wide page actually calls when no
Tour is picked. `slate` is the one deliberate exception in the other direction (AA-564 2.2,
AA-563's investigation): acp_shared.subject really is per-tenant (ADR-0003 confirms this is
correct, not tech debt), so `slate` requires `tenant_id` instead of `tour_id` — the OPPOSITE of
what it required before AA-564 (which wrongly scoped it by Tour, contradicting the page's own
"Slate is tenant-specific" copy). Each row also now carries its originating topic name (`place`/
`action`/`hub_name`/`tour_name`), joined the same way the Tenant Portal's `fetch_slate()` already
does, instead of a raw `segment_id`/`route_id` the FE could only label generically.

New optional filters, shared by `segments`/`score`/`routes`: `market` (one of the 6 real markets;
Score/Route rows all carry a `market` column post-AA-545, Segment's join to `atom_ranking` does
too). Section-specific filters: `place_search` (Segment — ILIKE canonical_place OR
canonical_action), `min_recurrence` (Segment), `min_total_rank`/`max_total_rank` (Score),
`min_days`/`max_days`/`hub_name_search` (Route). `limit`/`offset` pagination added to all 3 (same
50-row-page convention `admin_atoms.py`'s `GET /atoms` already uses) — a platform-wide, unfiltered
query can return far more rows than any single tour ever could.

New `GET /admin/dashboard/summary` — the header stat bar's single data source (Tour/Atom/Segment/
Score row/Route/Hub counts, all re-filtered by the same `tour_id`/`market` the page's common
filter currently has selected). See its own docstring for why "Hub" is a derived count, not a
real `acp_contract.hub` row count.

All endpoints below are read-only, x-admin-secret only (same `verify_admin_secret` convention as
`admin_atoms.py`/`admin_a4.py`). The other 3 non-Atomize panels (Write-Gate, Review, Publish) live
on the separate `/admin/tenant-activity` page now (AA-551) but are still served by the EXISTING
`admin_a4.py` `content-log`/`publish-log` endpoints, untouched by this file.

Cross-tenant by design (same stance as admin_a4.py, STEP0/AA-437): Route/Slate can in principle
touch more than one tenant's own T7 run over the same platform-shared atoms — `slate` still
returns every tenant's rows with `tenant_name` attached; Segment/Score/Route carry no tenant
dimension at all post-AA-545, so this doesn't apply to them anymore.

AA-554 mục G (07/09/2026) — new `GET /admin/dashboard/hubs`, a real Route/Hub table split (see
its own docstring and `list_routes`' corrected one for why `acp_contract.hub` reads 0 rows today
and why that's accurate, not a missing wire). `dashboard_summary`'s `hub_count` now counts the
real table too, for consistency with what this new endpoint shows.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, Query, Request

from api.routers.admin import verify_admin_secret

router = APIRouter(prefix="/admin/dashboard", tags=["admin-dashboard"])

# AA-551 — the 6 real markets DataForSEO/Search Demand cover today (Score/Route panels already
# rendered these as free text; kept as a plain list, not a DB enum, so a new market doesn't need a
# migration to become filterable here — same "no server-side enum" choice AA-527's own Atomize
# filters made for owner_scope/lifecycle_stage).
MARKETS = ["US", "UK", "AU", "DE", "FR", "NL"]


def _safe(row, exclude: tuple = ()) -> dict:
    """Same local-safe()-per-router convention as admin_atoms.py/v1_tours.py (no shared
    api/utils.safe() exists in this repo) — UUID/Decimal/datetime -> JSON-safe, plus JSONB
    columns that come back as a raw string (no jsonb codec registered on this app's asyncpg
    connections, same gap AA-314/admin_atoms.py already found for `media`).

    AA-551 — `exclude` drops window-function bookkeeping columns (e.g. `full_count`, used to get
    a total alongside a paginated page in one query) that were only ever meant for this function's
    own callers, never the JSON response itself."""
    import json
    from decimal import Decimal
    from uuid import UUID

    if not row:
        return {}
    d = {k: v for k, v in dict(row).items() if k not in exclude}
    for k, v in d.items():
        if isinstance(v, UUID):
            d[k] = str(v)
        elif isinstance(v, Decimal):
            d[k] = float(v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, str) and k in ("ordered_segment_ids", "cleared_bar_reason"):
            try:
                d[k] = json.loads(v)
            except (TypeError, ValueError):
                pass
    return d


# ── GET /admin/dashboard/segments — Section 02, audit view ─────────────────

@router.get("/segments")
async def list_segments(
    request: Request,
    tour_id: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    place_search: Optional[str] = Query(None),
    min_recurrence: Optional[int] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    x_admin_secret: str = Header(None),
):
    """acp_contract.atom_segment — a Segment has no tour_id column of its own (it's grouped by
    place/action across the WHOLE platform atom pool, AA-509/AA-545), so this reaches it through
    atom_segment_member -> tour_atoms.tour_id, one row per (Segment, Tour, market) that has >=1
    member atom on that tour. total_rank/recurrence/route linkage joined in (same LATERAL Route
    lookup admin_atoms.py's GET /atoms already uses) so this one panel shows Segment + its Score +
    its Route membership together, rather than 3 separate near-empty tables.

    AA-545 — no more `tenant_id`/`tenant_name` (`atom_segment` is platform-wide now); `ar.market`
    is surfaced instead of collapsed, one row per (Segment, market) the way `atom_ranking` itself
    is now shaped — an audit view should show the real per-market breakdown, not hide it.

    AA-551 — `tour_id` is now OPTIONAL (was hard-required, the exact gap AA-550 found: this made
    "All tours" mode show nothing for Segment/Score/Route/Slate despite AA-545 having already made
    3 of those 4 genuinely platform-wide). Omitting it returns every tour's matching rows —
    `tour_id`/`tour_name` added to the SELECT/GROUP BY so a platform-wide row can still say which
    tour it came from. `market`/`place_search`/`min_recurrence` are new filters; `limit`/`offset`
    pagination (50/page, same convention as `admin_atoms.py`) since an unfiltered platform query
    can return far more rows than any single tour ever could. `member_count` is now counted
    per-tour (not globally per-Segment) since a Segment can span multiple tours and this is an
    audit-per-row count, not the Segment's own cross-tour recurrence (that's `recurrence` below,
    unchanged — `atom_ranking.recurrence` already counts distinct tours platform-wide)."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    conditions = ["NOT ta.deleted", "NOT ta.is_empty_marker"]
    params: list = []
    if tour_id:
        params.append(tour_id)
        conditions.append(f"ta.tour_id = ${len(params)}::uuid")
    if market:
        params.append(market)
        conditions.append(f"ar.market = ${len(params)}")
    if place_search:
        params.append(f"%{place_search}%")
        conditions.append(f"(asg.canonical_place ILIKE ${len(params)} OR asg.canonical_action ILIKE ${len(params)})")
    if min_recurrence is not None:
        params.append(min_recurrence)
        conditions.append(f"ar.recurrence >= ${len(params)}")
    where = " AND ".join(conditions)
    params.append(limit)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT ta.tour_id, rt.src_name AS tour_name,
                   asg.segment_id, asg.canonical_place, asg.canonical_action,
                   count(DISTINCT asm.atom_id) AS member_count,
                   ar.market, ar.total_rank, ar.recurrence, ar.excluded_reason,
                   rte.route_id, rte.hub_name AS route_hub_name,
                   count(*) OVER() AS full_count
            FROM acp_contract.atom_segment_member asm
            JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
            JOIN acp_contract.atom_segment asg ON asg.segment_id = asm.segment_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
            LEFT JOIN acp_contract.atom_ranking ar
                ON ar.segment_id = asm.segment_id AND ar.tour_id = ta.tour_id
               AND ar.superseded_at IS NULL  -- AA-734: current ranking row only
            LEFT JOIN LATERAL (
                SELECT r.route_id, r.hub_name
                FROM acp_contract.route r
                -- AA-532: current version only, same reasoning as admin_atoms.py's identical
                -- LATERAL join — a superseded route (never deleted) must not read as "part of
                -- Route X" once re-detection has moved this Segment on.
                WHERE r.tour_id = ta.tour_id AND r.superseded_at IS NULL
                  AND r.ordered_segment_ids @> jsonb_build_array(asm.segment_id)
                LIMIT 1
            ) rte ON true
            WHERE {where}
            GROUP BY ta.tour_id, rt.src_name, asg.segment_id, asg.canonical_place,
                     asg.canonical_action, ar.market, ar.total_rank, ar.recurrence,
                     ar.excluded_reason, rte.route_id, rte.hub_name
            ORDER BY rt.src_name, asg.canonical_place, ar.market, ar.total_rank ASC NULLS LAST
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *params,
        )

    total = rows[0]["full_count"] if rows else 0
    return {
        "data": [_safe(r, exclude=("full_count",)) for r in rows], "total": total,
        "tour_id": tour_id, "limit": limit, "offset": offset,
    }


# ── GET /admin/dashboard/score — Section 03, audit view ─────────────────────

@router.get("/score")
async def list_score(
    request: Request,
    tour_id: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    min_total_rank: Optional[int] = Query(None),
    max_total_rank: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    x_admin_secret: str = Header(None),
):
    """acp_contract.atom_ranking rows, in rank order (lower total_rank = better, same convention
    Route's read-time score computation now uses, AA-545 Q3) — the ranked list feeding Route
    detection, distinct from the Segment panel above (which shows grouping, not the demand/
    recurrence/questions/said breakdown a rank is actually made of). A row with excluded_reason
    set (transit/unnamed_place) is real output too (AA-515: "an exclusion is arguable rather than
    a silent absence"), sorted after every ranked row rather than hidden.

    AA-545 — no more `tenant_id`/`tenant_name` (`atom_ranking` is platform-wide, PK
    `(market, tour_id, segment_id)` now); `ar.market` (the row's own PK column, which market this
    ranking pass was computed for) is shown alongside `ar.demand_market` — the two are now always
    equal (the old best-of-N-market pick this column used to record is gone, `rank_segments()` is
    called once per market), kept as a harmless redundant column rather than dropped mid-build,
    out of this endpoint's own audit-view scope.

    AA-551 — `tour_id` optional (see `list_segments` above for the full reasoning); omitting it
    returns every tour's ranking rows with `tour_id`/`tour_name` attached. `market`/
    `min_total_rank`/`max_total_rank` new filters, `limit`/`offset` pagination added."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    conditions = ["1 = 1"]
    params: list = []
    if tour_id:
        params.append(tour_id)
        conditions.append(f"ar.tour_id = ${len(params)}::uuid")
    if market:
        params.append(market)
        conditions.append(f"ar.market = ${len(params)}")
    if min_total_rank is not None:
        params.append(min_total_rank)
        conditions.append(f"ar.total_rank >= ${len(params)}")
    if max_total_rank is not None:
        params.append(max_total_rank)
        conditions.append(f"ar.total_rank <= ${len(params)}")
    where = " AND ".join(conditions)
    params.append(limit)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT ar.tour_id, rt.src_name AS tour_name, ar.market, ar.segment_id,
                   asg.canonical_place, asg.canonical_action,
                   ar.demand_rank, ar.recurrence_rank, ar.questions_rank, ar.said_rank,
                   ar.total_rank, ar.demand_market, ar.demand_volume,
                   ar.recurrence, ar.questions, ar.said, ar.excluded_reason, ar.computed_at,
                   count(*) OVER() AS full_count
            FROM acp_contract.atom_ranking ar
            LEFT JOIN acp_contract.atom_segment asg ON asg.segment_id = ar.segment_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ar.tour_id
            -- AA-734: current rows only (atom_ranking history is deleted post-swap, not retained like route)
            WHERE {where} AND ar.superseded_at IS NULL
            ORDER BY rt.src_name, ar.market, (ar.excluded_reason IS NOT NULL), ar.total_rank ASC NULLS LAST
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *params,
        )

    total = rows[0]["full_count"] if rows else 0
    return {
        "data": [_safe(r, exclude=("full_count",)) for r in rows], "total": total,
        "tour_id": tour_id, "limit": limit, "offset": offset,
    }


# ── GET /admin/dashboard/routes — Section 04, audit view ────────────────────

@router.get("/routes")
async def list_routes(
    request: Request,
    tour_id: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    min_days: Optional[int] = Query(None, ge=1),
    max_days: Optional[int] = Query(None, ge=1),
    hub_name_search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    x_admin_secret: str = Header(None),
):
    """acp_contract.route rows (has its own tour_id column directly, migration 131 — no
    join-through needed, unlike Segment above).

    AA-554 mục G correction: this endpoint used to carry a `hub_grouping_backlog: true` flag and a
    docstring claiming "Route detection isn't wiring hub_id yet" (AA-525 Phần 12 mục 8). That was
    stale — `services/acp_contract/route_detection.py`'s `families()`/`resolved_hub` logic (lines
    ~330-390) has created/reused real `acp_contract.hub` rows since AA-510/migration 131, whenever
    2+ tours share enough Segments (`SHARED_ENOUGH` ratio) to form a family — "a family of one is
    not a family" is the origin's own rule, so a single, un-shared tour's Route never gets a
    `hub_id`. The real reason `acp_contract.hub` has 0 rows in current prod data is that no 2 tours
    currently share enough of a route to qualify, not a missing wire. See `GET
    /admin/dashboard/hubs` below for the now-real, separate Hub table this drives (mục G: "tách 2
    bảng riêng").

    AA-532: when scoped to ONE Tour, deliberately does NOT filter `superseded_at IS NULL` the way
    every other reader of this table now does (v1_route_hub.py, slate.py, admin_atoms.py) — this
    IS the audit view, the one place seeing a Route's version history (current AND superseded) is
    the actual point, not a bug. AA-551: in "All tours" mode (no `tour_id`), DOES filter to
    current-only — a platform-wide table listing every historical version of every tour's routes
    at once would be noisy with no UI ask for it; the per-tour full-history view is unchanged.
    `version`/`superseded_at` are still exposed either way so a single-tour view can show history;
    current rows sort first, then best score.

    AA-545 — no more `tenant_id`/`tenant_name`/stored `score` (`route` is platform-wide,
    composition-only now). `score` here is computed per market (`AVG(total_rank)` over the
    Route's member Segments, same formula `services/acp_shared/slate.py::
    _fetch_route_candidates()` uses at read time for a real tenant) — one row per (Route version,
    market), same per-market-breakdown treatment the Segment/Score panels above already apply.

    AA-551 — `tour_id` optional, `market`/`min_days`/`max_days`/`hub_name_search` new filters,
    `limit`/`offset` pagination, `tour_id`/`tour_name` added to output for the platform-wide
    view."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    conditions = []
    params: list = []
    if tour_id:
        params.append(tour_id)
        conditions.append(f"r.tour_id = ${len(params)}::uuid")
    else:
        conditions.append("r.superseded_at IS NULL")  # AA-551 — platform view: current only
    if min_days is not None:
        params.append(min_days)
        conditions.append(f"(r.last_day - r.first_day + 1) >= ${len(params)}")
    if max_days is not None:
        params.append(max_days)
        conditions.append(f"(r.last_day - r.first_day + 1) <= ${len(params)}")
    if hub_name_search:
        params.append(f"%{hub_name_search}%")
        conditions.append(f"r.hub_name ILIKE ${len(params)}")
    where = " AND ".join(conditions) if conditions else "1 = 1"
    # market filters the joined atom_ranking, applied after GROUP BY (it's not a route column) —
    # HAVING, not WHERE.
    having = ""
    if market:
        params.append(market)
        having = f"HAVING bool_or(ar.market = ${len(params)}) "
    params.append(limit)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT r.route_id, r.tour_id, rt.src_name AS tour_name, r.hub_id, r.hub_name,
                   r.ordered_segment_ids, r.first_day, r.last_day, r.created_at,
                   r.version, r.superseded_at, ar.market, AVG(ar.total_rank) AS score,
                   count(*) OVER() AS full_count
            FROM acp_contract.route r
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = r.tour_id
            LEFT JOIN acp_contract.atom_ranking ar
                ON ar.tour_id = r.tour_id
               AND ar.segment_id = ANY (SELECT jsonb_array_elements_text(r.ordered_segment_ids))
               AND ar.excluded_reason IS NULL
               AND ar.superseded_at IS NULL  -- AA-734: score from current ranking rows only
            WHERE {where}
            GROUP BY r.route_id, r.tour_id, rt.src_name, r.hub_id, r.hub_name,
                     r.ordered_segment_ids, r.first_day, r.last_day, r.created_at, r.version,
                     r.superseded_at, ar.market
            {having}
            ORDER BY rt.src_name, (r.superseded_at IS NOT NULL), ar.market, score ASC NULLS LAST
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *params,
        )

    total = rows[0]["full_count"] if rows else 0
    return {
        "data": [_safe(r, exclude=("full_count",)) for r in rows], "total": total,
        "tour_id": tour_id, "limit": limit, "offset": offset,
    }


# ── GET /admin/dashboard/routes/{route_id}/days — AA-557 F.12 ───────────────

@router.get("/routes/{route_id}/days")
async def route_day_breakdown(
    route_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Per-Day breakdown of one Route's member Segments — AA-557 F.12 ("chỉ ghi '1-5' ngày là vô
    nghĩa, không biết Day 1 có gì").

    STEP0 (read `services/acp_contract/route_detection.py` before building this): `acp_contract.
    route.ordered_segment_ids` is segment_id order ONLY — the day number itself is NOT persisted
    on the route row (`derive_routes()`'s own `Route.segment_ids` tuple drops the `Moment.day` it
    was built from; only the route's own `first_day`/`last_day` SPAN survives). So a naive
    `first_day + index` mapping would be wrong whenever a day holds >1 Segment (routes.py's
    `_runs()` groups multiple same-day Moments together) — real day-per-segment data is NOT at
    the response layer today, but it IS re-derivable: `acp_contract.tour_atoms.itinerary_day`
    (migration 093) is the real source `route_detection.py` itself reads at generation time
    (`MIN(ta.itinerary_day) AS day`, route_detection.py:272). This endpoint re-runs that exact
    same aggregation for one Route's own segment_ids instead of fabricating a day count.
    """
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        route = await conn.fetchrow(
            "SELECT route_id, tour_id, ordered_segment_ids, first_day, last_day "
            "FROM acp_contract.route WHERE route_id = $1",
            route_id,
        )
        if not route:
            return {"route_id": route_id, "days": [], "found": False}

        segment_ids = list(route["ordered_segment_ids"] or [])
        if isinstance(route["ordered_segment_ids"], str):
            import json
            segment_ids = json.loads(route["ordered_segment_ids"])

        rows = await conn.fetch(
            """
            SELECT asg.segment_id, asg.canonical_place, asg.canonical_action,
                   MIN(ta.itinerary_day) AS day
            FROM acp_contract.atom_segment_member asm
            JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
            JOIN acp_contract.atom_segment asg ON asg.segment_id = asm.segment_id
            WHERE asm.segment_id = ANY($1::text[]) AND ta.tour_id = $2::uuid
              AND NOT ta.deleted AND NOT ta.is_empty_marker
            GROUP BY asg.segment_id, asg.canonical_place, asg.canonical_action
            """,
            segment_ids, route["tour_id"],
        )
    by_segment = {r["segment_id"]: r for r in rows}
    by_day: dict[int, list] = {}
    for seg_id in segment_ids:
        r = by_segment.get(seg_id)
        day = r["day"] if r else None
        by_day.setdefault(day, []).append({
            "segment_id": seg_id,
            "canonical_place": r["canonical_place"] if r else None,
            "canonical_action": r["canonical_action"] if r else None,
        })
    days = [
        {"day": day, "segments": segs}
        for day, segs in sorted(by_day.items(), key=lambda kv: (kv[0] is None, kv[0]))
    ]
    return {
        "route_id": route_id, "first_day": route["first_day"], "last_day": route["last_day"],
        "days": days, "found": True,
    }


# ── GET /admin/dashboard/hubs — Section 04, Hub half of AA-554 mục G's table split ──────────

@router.get("/hubs")
async def list_hubs(
    request: Request,
    tour_id: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    hub_name_search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    x_admin_secret: str = Header(None),
):
    """acp_contract.hub — AA-554 mục G: a genuine Hub table, split out from the single Route table
    it used to be folded into (`list_routes` above only ever exposed `route.hub_name`, a
    per-route denormalized placeholder, never a real `acp_contract.hub` row). A Hub row only
    exists once `route_detection.py`'s `families()` finds 2+ tours sharing enough Segments to
    group ("a family of one is not a family") — see `list_routes`' own docstring above for the
    full correction. Real prod data has exactly 0 Hub rows today for that reason, which is why the
    frontend's empty-state here reads "No Hub yet — needs 2+ tours sharing a route segment"
    instead of implying anything is broken.

    Only CURRENT routes (`superseded_at IS NULL`) count toward a Hub's `route_count`/`tour_names`
    — same "no historical-version noise in a platform-wide view" convention `list_routes` uses in
    "All tours" mode. `market` filters the same way `list_routes` does: through the joined
    `atom_ranking` rows for each member Route's Segments, applied as a HAVING (it's not a Hub
    column)."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    conditions = ["r.superseded_at IS NULL"]
    params: list = []
    if tour_id:
        params.append(tour_id)
        conditions.append(f"r.tour_id = ${len(params)}::uuid")
    if hub_name_search:
        params.append(f"%{hub_name_search}%")
        conditions.append(f"h.hub_name ILIKE ${len(params)}")
    where = " AND ".join(conditions)
    having = ""
    if market:
        params.append(market)
        having = f"HAVING bool_or(ar.market = ${len(params)}) "
    params.append(limit)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT h.hub_id, h.hub_name, h.created_at, h.updated_at,
                   array_agg(DISTINCT rt.src_name) FILTER (WHERE rt.src_name IS NOT NULL) AS tour_names,
                   count(DISTINCT r.route_id) AS route_count,
                   count(*) OVER() AS full_count
            FROM acp_contract.hub h
            JOIN acp_contract.route r ON r.hub_id = h.hub_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = r.tour_id
            LEFT JOIN acp_contract.atom_ranking ar
                ON ar.tour_id = r.tour_id
               AND ar.segment_id = ANY (SELECT jsonb_array_elements_text(r.ordered_segment_ids))
               AND ar.excluded_reason IS NULL
               AND ar.superseded_at IS NULL  -- AA-734: score from current ranking rows only
            WHERE {where}
            GROUP BY h.hub_id, h.hub_name, h.created_at, h.updated_at
            {having}
            ORDER BY h.updated_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *params,
        )

    total = rows[0]["full_count"] if rows else 0
    return {
        "data": [_safe(r, exclude=("full_count",)) for r in rows], "total": total,
        "tour_id": tour_id, "limit": limit, "offset": offset,
    }


# ── GET /admin/dashboard/slate — Section 05, audit view ──────────────────────

@router.get("/slate")
async def list_slate(
    request: Request,
    tenant_id: str = Query(...),
    channel: Optional[str] = Query(None),
    x_admin_secret: str = Header(None),
):
    """acp_shared.subject (the Slate proposal, AA-511) for this tenant — AA-564 2.2 (per AA-563's
    investigation): Slate is genuinely per-tenant by design (ADR-0003 confirms this is correct,
    not tech debt — a Subject is one specific tenant's own proposal/decision, never shared), so
    this endpoint is scoped by `tenant_id` directly, same as the Tenant Portal's own
    `services/acp_shared/slate.py::fetch_slate()`. It used to be scoped by `tour_id` instead (a
    real bug — subject has no tour_id column of its own, so that required an awkward OR-based
    lookup through Segment/Route, AND made Admin pick a Tour first even though the UI's own copy
    said "Slate is tenant-specific"). Each row now also carries the ORIGINATING topic name
    (`place`/`action`/`hub_name`, `tour_name`) via the same LEFT JOINs `fetch_slate()` already
    runs in production (AA-564 2.1) — previously this endpoint returned only raw `segment_id`/
    `route_id`, which the FE could only render as a generic "Segment"/"Route" label.

    AA-556 fix (found live while verifying that issue's own "cut count rises on the admin
    dashboard" acceptance criteria): the SQL used to hard-filter `s.state != 'cut'`, which meant
    `by_state["cut"]` below could never be anything but 0 no matter how many Subjects were
    actually cut — the query never even fetched those rows for the Python loop to count. Real,
    pre-existing bug (predates AA-556's tenant-facing Cut button; the backend `cut_subject()`
    itself was already correctly writing `state='cut'` since AA-554 H.2, this endpoint just never
    surfaced it). Confirmed directly against RDS: 2 real `linkedin` Subjects for tenant
    WanderLux Travel had `state='cut'` while this endpoint reported `cut: 0`. Removed the filter
    — cut rows are meant to be visible (SLATE_STATE_COLOR/SLATE_STATE_TOOLTIP on the frontend
    already have a real `cut` = red entry, and the H.3 comment on the frontend explicitly says
    the badge "is NOT hidden (shows the real count)"), so excluding them was never intentional."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.subject_id, s.tenant_id, t.name AS tenant_name, s.channel, s.state,
                   s.score, s.segment_id, s.route_id, s.cleared_bar_reason, s.created_at,
                   asg.canonical_place, asg.canonical_action,
                   r.hub_name, rt_route.src_name AS route_tour_name,
                   CASE WHEN r.ordered_segment_ids IS NOT NULL
                        THEN jsonb_array_length(r.ordered_segment_ids) END AS route_segment_count,
                   seg_tours.tour_names AS segment_tour_names
            FROM acp_shared.subject s
            LEFT JOIN shared.tenants t ON t.tenant_id = s.tenant_id
            LEFT JOIN acp_contract.atom_segment asg ON asg.segment_id = s.segment_id
            LEFT JOIN acp_contract.route r ON r.route_id = s.route_id
            LEFT JOIN silver_aa_internal.raw_tours rt_route ON rt_route.tour_id = r.tour_id
            -- A Segment's own definition (CONTEXT.md) is "usually from different tours" — unlike
            -- a Route (inherently one tour's day-span), a Segment-based Subject can legitimately
            -- point at more than one tour, so this aggregates rather than assuming a single one.
            LEFT JOIN LATERAL (
                SELECT array_agg(DISTINCT rt2.src_name) AS tour_names
                FROM acp_contract.atom_segment_member asm2
                JOIN acp_contract.tour_atoms ta2 ON ta2.atom_id = asm2.atom_id
                LEFT JOIN silver_aa_internal.raw_tours rt2 ON rt2.tour_id = ta2.tour_id
                WHERE asm2.segment_id = s.segment_id AND NOT ta2.deleted AND NOT ta2.is_empty_marker
            ) seg_tours ON s.segment_id IS NOT NULL
            WHERE s.tenant_id = $1::uuid
              AND ($2::text IS NULL OR s.channel = $2::text)
            ORDER BY s.channel, s.score ASC NULLS LAST, s.created_at DESC
            """,
            tenant_id, channel,
        )

    by_state = {"proposed": 0, "picked": 0, "used": 0, "cut": 0}
    data = []
    for r in rows:
        if r["state"] in by_state:
            by_state[r["state"]] += 1
        row = _safe(r, exclude=("route_tour_name", "route_segment_count", "segment_tour_names"))
        if r["route_id"]:
            row["tour_name"] = r["route_tour_name"]
            row["segment_count"] = r["route_segment_count"]
        else:
            row["tour_name"] = ", ".join(r["segment_tour_names"] or [])
            row["segment_count"] = None
        data.append(row)

    return {
        "data": data, "total": len(rows), "tenant_id": tenant_id,
        "by_state": by_state,
    }


# ── GET /admin/dashboard/summary — AA-551, header stat bar for the platform-wide page ───────

@router.get("/summary")
async def dashboard_summary(
    request: Request,
    tour_id: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    x_admin_secret: str = Header(None),
):
    """AA-551 — the rebuilt platform-wide page's header stat bar: Tour/Atom/Segment/Score-row/
    Route/Hub counts, each re-filtered by the same `tour_id`/`market` the page's common filter
    currently has selected (AA-550 mục F point 4 — "tự cập nhật theo bộ lọc", not a fixed
    platform-wide total).

    `atom_count` only ever responds to `tour_id` — atoms have no `market` column at all (market is
    a property of `atom_ranking`, computed downstream of Atom), so a market filter can't narrow it
    further; this is a real property of the data, not an oversight.

    `hub_count` — AA-554 mục G correction: now a real `COUNT(DISTINCT acp_contract.hub.hub_id)`,
    matching what the new `GET /admin/dashboard/hubs` table actually shows. Before this build it
    was `COUNT(DISTINCT route.hub_name)` (a per-route placeholder string, non-zero even for a
    single, un-shared tour) on the reasoning that the real `acp_contract.hub` table was believed
    unwired and would always read 0 — `list_routes`' own docstring above explains why that belief
    was wrong. Reading 0 today is correct, not misleading: no 2 tours currently share enough of a
    route to form a Hub.

    `route_count`/`hub_count` use CURRENT routes only (`superseded_at IS NULL`) — matches the
    default `list_routes` above uses in "All tours" mode (no historical-version noise in a
    platform-wide total)."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT count(DISTINCT ta.tour_id)
                 FROM acp_contract.tour_atoms ta
                 WHERE NOT ta.deleted AND NOT ta.is_empty_marker
                   AND ($1::uuid IS NULL OR ta.tour_id = $1::uuid)
                   AND ($2::text IS NULL OR EXISTS (
                       SELECT 1 FROM acp_contract.atom_ranking ar
                       WHERE ar.tour_id = ta.tour_id AND ar.market = $2::text
                         AND ar.superseded_at IS NULL  -- AA-734
                   ))
                ) AS tour_count,
                (SELECT count(*)
                 FROM acp_contract.tour_atoms ta
                 WHERE NOT ta.deleted AND NOT ta.is_empty_marker
                   AND ($1::uuid IS NULL OR ta.tour_id = $1::uuid)
                ) AS atom_count,
                (SELECT count(DISTINCT asm.segment_id)
                 FROM acp_contract.atom_segment_member asm
                 JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
                 WHERE NOT ta.deleted AND NOT ta.is_empty_marker
                   AND ($1::uuid IS NULL OR ta.tour_id = $1::uuid)
                   AND ($2::text IS NULL OR EXISTS (
                       SELECT 1 FROM acp_contract.atom_ranking ar
                       WHERE ar.segment_id = asm.segment_id AND ar.tour_id = ta.tour_id
                         AND ar.market = $2::text
                         AND ar.superseded_at IS NULL  -- AA-734
                   ))
                ) AS segment_count,
                (SELECT count(*)
                 FROM acp_contract.atom_ranking ar
                 WHERE ($1::uuid IS NULL OR ar.tour_id = $1::uuid)
                   AND ($2::text IS NULL OR ar.market = $2::text)
                   AND ar.superseded_at IS NULL  -- AA-734: current rows only (no dip during recompute)
                ) AS score_count,
                (SELECT count(*)
                 FROM acp_contract.route r
                 WHERE r.superseded_at IS NULL
                   AND ($1::uuid IS NULL OR r.tour_id = $1::uuid)
                   AND ($2::text IS NULL OR EXISTS (
                       SELECT 1 FROM acp_contract.atom_ranking ar
                       WHERE ar.segment_id = ANY (SELECT jsonb_array_elements_text(r.ordered_segment_ids))
                         AND ar.tour_id = r.tour_id AND ar.market = $2::text
                         AND ar.superseded_at IS NULL  -- AA-734
                   ))
                ) AS route_count,
                (SELECT count(DISTINCT h.hub_id)
                 FROM acp_contract.hub h
                 JOIN acp_contract.route r ON r.hub_id = h.hub_id
                 WHERE r.superseded_at IS NULL
                   AND ($1::uuid IS NULL OR r.tour_id = $1::uuid)
                   AND ($2::text IS NULL OR EXISTS (
                       SELECT 1 FROM acp_contract.atom_ranking ar
                       WHERE ar.segment_id = ANY (SELECT jsonb_array_elements_text(r.ordered_segment_ids))
                         AND ar.tour_id = r.tour_id AND ar.market = $2::text
                         AND ar.superseded_at IS NULL  -- AA-734
                   ))
                ) AS hub_count
            """,
            tour_id, market,
        )

    return {
        "tour_count": row["tour_count"], "atom_count": row["atom_count"],
        "segment_count": row["segment_count"], "score_count": row["score_count"],
        "route_count": row["route_count"], "hub_count": row["hub_count"],
        "tour_id": tour_id, "market": market,
    }
