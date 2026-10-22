"""
services.acp_planning.trip_reallocation — AA-448 round 6: feedback-informed trip-reallocation
suggestion for the NEXT quarter.

NOT part of aa-marketing-v2's design — Module H's `rollup_atoms()` (the reference's real
feedback loop) only ever adjusted `tour_atoms.weight` and fed that into future MONTH allocation
(N6). It never re-touched `QuarterPlan`/trip selection at all. This module is Nghiep's own
explicit extension (round 6): once real feedback has adjusted atom weights
(`services/acp_shared/content_metrics.py::rollup_atom_weights()`), those adjusted weights now
also flow into `compute_quarter_plan()`'s 5th scoring term (`engagement_adjustment`,
quarter.py) — this module surfaces that as an actionable, tenant-reviewed suggestion for the
NEXT quarter, rather than silently re-planning anything.

Shape mirrors `services/acp_produce/trust_ramp.py::suggest_ramp_transition()`/
`confirm_ramp_transition()` exactly (Nghiep's own suggested naming/pattern): `suggest_...()` is
pure-ish — computes, diffs, never writes; `confirm_...()` ALWAYS writes an
`acp_shared.audit_log` entry (same table trust_ramp.py already reuses, no new logging shape),
whether the tenant accepts or rejects, and on accept applies the change through the SAME
Gate-B-Option-A finalize path T7's own `POST /v1/planning/quarter-plan` uses — an accepted
reallocation becomes a normal new quarter_plan_version, not a special kind of object.
"""
from __future__ import annotations

import json
from uuid import UUID

import asyncpg
import structlog

from services.acp_shared.dfs_relevance import fetch_dfs_relevance_by_tour, fetch_dfs_relevance_by_tour_on

from .models import QuarterPlan
from .quarter import (approve_quarter_plan_version, approve_quarter_plan_version_on, compute_quarter_plan,
                      fetch_approved_quarter_plan, fetch_approved_quarter_plan_on, save_quarter_plan_version,
                      save_quarter_plan_version_on)
from .runway import compute_runway_map
from .tenant_config import fetch_tenant_planning_config
from .tenant_pool import (fetch_tenant_atoms_by_trip, fetch_tenant_atoms_by_trip_on, fetch_tenant_trips,
                          fetch_tenant_trips_on)

logger = structlog.get_logger()


class VersionConflictError(Exception):
    """AA-859 wave 4 — another confirm saved the same version number of this quarter first."""


async def suggest_trip_reallocation(tenant_id: UUID, year: int, quarter: int, pool) -> dict:
    """Never writes. Computes a fresh QuarterPlan for (year, quarter) using the tenant's
    CURRENT, feedback-adjusted atom weights (whatever rollup_atom_weights() last wrote to
    tour_atoms.weight — this function does not trigger a rollup itself, it only consumes
    whatever weights already exist), and diffs its trip_ids against the tenant's existing
    finalized plan for that quarter, if any."""
    config = await fetch_tenant_planning_config(tenant_id, pool)
    # fetch_tenant_trips() reads published_tours/raw_tours as a shared reference pool (sentinel
    # tenant_id), not per-tenant data — see tenant_pool.py's own _TENANT_TRIP_QUERY comment (the
    # tenant-pool route path is suggest_trip_reallocation_scoped below, AA-859 wave 4).
    trips = await fetch_tenant_trips(tenant_id, pool)
    atoms_by_trip = await fetch_tenant_atoms_by_trip(tenant_id, pool)
    dfs_relevance_by_trip = await fetch_dfs_relevance_by_tour([t.id for t in trips], pool)
    existing_plan = await fetch_approved_quarter_plan(tenant_id, year, quarter, pool)
    return _suggestion(tenant_id, year, quarter, config, trips, atoms_by_trip, dfs_relevance_by_trip,
                       existing_plan)


def _suggestion(tenant_id, year, quarter, config, trips, atoms_by_trip, dfs_relevance_by_trip,
                existing_plan) -> dict:
    """The pure half of a suggestion: the fresh plan from the tenant's trips / atoms / DFS relevance, diffed
    against its approved plan for the quarter."""
    runway = compute_runway_map(tenant_id, year, trips, config.markets)
    fresh_plan = compute_quarter_plan(
        tenant_id, year, quarter, trips, config.markets, config.capacity_posts_per_week,
        specials=[], runway=runway, atoms_by_trip=atoms_by_trip,
        dfs_relevance_by_trip=dfs_relevance_by_trip,
    )
    existing_ids = set(existing_plan.trip_ids) if existing_plan else set()
    fresh_ids = set(fresh_plan.trip_ids)

    return {
        "plan": fresh_plan.model_dump(mode="json"),
        "has_existing_plan": existing_plan is not None,
        "added": sorted(str(i) for i in (fresh_ids - existing_ids)),
        "removed": sorted(str(i) for i in (existing_ids - fresh_ids)),
        "unchanged": sorted(str(i) for i in (fresh_ids & existing_ids)),
    }


async def confirm_trip_reallocation(
    pool, tenant_id: UUID, year: int, quarter: int, accept: bool, actor: str,
) -> dict:
    """ALWAYS writes an acp_shared.audit_log entry first (mirrors
    trust_ramp.py::confirm_ramp_transition()'s "never silently" framing — a REJECTED suggestion
    is exactly the kind of event this log exists to surface, not something to drop). Re-runs
    suggest_trip_reallocation() itself (not passed a cached suggestion) so what gets applied on
    accept is never stale relative to what the tenant most recently saw — a deliberate
    simplicity choice over passing a large plan payload through the confirm request; this is a
    low-frequency (quarterly) action, the extra DB round-trip is not a real cost here."""
    suggestion = await suggest_trip_reallocation(tenant_id, year, quarter, pool)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO acp_shared.audit_log
                (tenant_id, actor, action, resource_type, resource_id, details)
            VALUES ($1, $2, 'trip_reallocation_suggestion', 'quarter_plan', $3, $4::jsonb)
            """,
            str(tenant_id), actor, f"{year}-Q{quarter}",
            json.dumps({
                "accepted": accept, "added": suggestion["added"], "removed": suggestion["removed"],
            }),
        )

    if not accept:
        return {"accepted": False, "suggestion": suggestion}

    plan = QuarterPlan(**suggestion["plan"])
    # AA-448 live-verify finding: acp_shared.quarter_plan_version.source has a CHECK constraint
    # allowing only 'standard'/'override' (migration 092) — "override" is the correct existing
    # value here (a tenant overriding their previous plan based on a feedback suggestion),
    # not a new value this task should invent a migration for.
    version_id = await save_quarter_plan_version(plan, pool, source="override")
    await approve_quarter_plan_version(version_id, f"tenant:{tenant_id}", pool)
    return {"accepted": True, "version_id": str(version_id), "suggestion": suggestion}


# ── AA-859 wave 4: the route path, behind TRIP_REALLOCATION_FLAG (needs migrations 265 + 266) ──────────────
#
# suggest: the planning config on MAIN (D2: shared.tenants / acp_shared.tenant_config, the tenant's own settings,
# no tenant-role grant), then ONE tenant block for every tenant-data read (trips with their pages, the tenant's
# own atoms, seo_context master rows, the approved plan), released before the pure compute.
# confirm: the same reads and compute, then on accept ONE tenant transaction for save + approve (advisory lock per
# tenant / quarter, so two confirms serialize; save and approve are atomic, where the pool path's two
# transactions could leave an orphan pending version after a crash between them). The audit row goes to MAIN after
# the blocks, for accept and reject alike, best-effort (acp_shared.audit_log has no RLS and no tenant-role grant).
# Flag off: the same reads on a main-pool connection, then the pre-wave-4 order exactly (audit first, a lost audit
# row fails the confirm; save and approve through the pool functions).

_QUARTER_LOCK_SQL = "SELECT pg_advisory_xact_lock(859, hashtext($1))"


async def _read_inputs_on(conn, tenant_id: UUID, year: int, quarter: int):
    trips = await fetch_tenant_trips_on(conn, tenant_id)
    atoms_by_trip = await fetch_tenant_atoms_by_trip_on(conn, tenant_id)
    dfs_relevance_by_trip = await fetch_dfs_relevance_by_tour_on(conn, [t.id for t in trips])
    existing_plan = await fetch_approved_quarter_plan_on(conn, tenant_id, year, quarter)
    return trips, atoms_by_trip, dfs_relevance_by_trip, existing_plan


async def _scoped_suggestion(request, tenant_id: UUID, year: int, quarter: int, event: str) -> tuple[dict, bool]:
    from api.core.aa544_tenant_pool import TRIP_REALLOCATION_FLAG, acquire_scoped_conn
    pool = request.app.state.pool
    config = await fetch_tenant_planning_config(tenant_id, pool)   # AA-859 MAIN exception: own config (D2)
    async with acquire_scoped_conn(
        request, str(tenant_id), TRIP_REALLOCATION_FLAG, fail_closed=True,
    ) as (conn, used_tenant_pool):
        logger.info(event, tenant_id=str(tenant_id), year=year, quarter=quarter, used_tenant_pool=used_tenant_pool)
        inputs = await _read_inputs_on(conn, tenant_id, year, quarter)
    return _suggestion(tenant_id, year, quarter, config, *inputs), used_tenant_pool


async def suggest_trip_reallocation_scoped(request, tenant_id: UUID, year: int, quarter: int) -> dict:
    """GET /v1/planning/trip-reallocation/suggest (AA-859 wave 4). Never writes."""
    suggestion, _ = await _scoped_suggestion(request, tenant_id, year, quarter, "aa859_trip_reallocation_suggest")
    return suggestion


async def _write_reallocation_audit(conn_or_pool, tenant_id: UUID, actor: str, year: int, quarter: int,
                                    accept: bool, suggestion: dict, applied=None) -> None:
    details = {"accepted": accept, "added": suggestion["added"], "removed": suggestion["removed"]}
    if applied is not None:
        details["applied"] = applied
    await conn_or_pool.execute(
        """
        INSERT INTO acp_shared.audit_log
            (tenant_id, actor, action, resource_type, resource_id, details)
        VALUES ($1, $2, 'trip_reallocation_suggestion', 'quarter_plan', $3, $4::jsonb)
        """,
        str(tenant_id), actor, f"{year}-Q{quarter}", json.dumps(details),
    )


async def confirm_trip_reallocation_scoped(
    request, tenant_id: UUID, year: int, quarter: int, accept: bool, actor: str,
) -> dict:
    """POST /v1/planning/trip-reallocation/confirm (AA-859 wave 4). Raises VersionConflictError when another
    confirm saved the same version number first (the router answers 409)."""
    from api.core.aa544_tenant_pool import TRIP_REALLOCATION_FLAG, acquire_scoped_conn
    pool = request.app.state.pool
    suggestion, used_tenant_pool = await _scoped_suggestion(
        request, tenant_id, year, quarter, "aa859_trip_reallocation_confirm")

    if not used_tenant_pool:
        # Flag off: the pre-wave-4 order on the main pool (audit first; see confirm_trip_reallocation).
        await _write_reallocation_audit(pool, tenant_id, actor, year, quarter, accept, suggestion)
        if not accept:
            return {"accepted": False, "suggestion": suggestion}
        plan = QuarterPlan(**suggestion["plan"])
        version_id = await save_quarter_plan_version(plan, pool, source="override")
        await approve_quarter_plan_version(version_id, f"tenant:{tenant_id}", pool)
        return {"accepted": True, "version_id": str(version_id), "suggestion": suggestion}

    applied = False
    try:
        if not accept:
            return {"accepted": False, "suggestion": suggestion}
        plan = QuarterPlan(**suggestion["plan"])
        try:
            async with acquire_scoped_conn(
                request, str(tenant_id), TRIP_REALLOCATION_FLAG, fail_closed=True,
            ) as (conn, _):
                await conn.execute(_QUARTER_LOCK_SQL, f"{tenant_id}:quarter_plan:{year}:{quarter}")
                # "override": quarter_plan_version.source allows only 'standard' / 'override' (migration 092)
                version_id = await save_quarter_plan_version_on(conn, plan, source="override")
                await approve_quarter_plan_version_on(conn, version_id, f"tenant:{tenant_id}")
        except asyncpg.UniqueViolationError as exc:
            raise VersionConflictError(
                f"another confirm saved a version of {year}-Q{quarter} at the same time — reload and confirm again"
            ) from exc
        applied = True
        return {"accepted": True, "version_id": str(version_id), "suggestion": suggestion}
    finally:
        # AA-859 MAIN exception: acp_shared.audit_log has no RLS and no tenant-role grant. After the blocks, for
        # accept and reject; best-effort (the plan is saved, a lost audit row must not turn it into a 500).
        try:
            await _write_reallocation_audit(pool, tenant_id, actor, year, quarter, accept, suggestion,
                                            applied=applied)
        except Exception as exc:
            logger.warning("aa859_trip_reallocation_audit_failed", tenant_id=str(tenant_id), error=repr(exc))


__all__ = ["suggest_trip_reallocation", "confirm_trip_reallocation", "suggest_trip_reallocation_scoped",
           "confirm_trip_reallocation_scoped", "VersionConflictError"]
