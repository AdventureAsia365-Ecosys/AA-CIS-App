"""AA-986 L2: what the tour lifecycle service does after its transaction commits (ADR 0015 decision 2, last
paragraph): recompute ranking / routes with the NEW state, follow the place graph, recheck the public trip pages of
the tours whose state changed, and run the on_terminal hooks of the jobs it cancelled. Each step is best-effort: the
state change is already committed and must not fail because a queue insert did; every failure is logged (and visible
in the job tables).

`recompute_after_status_change` is the AA-713 / AA-723 / AA-743 helper that used to live in api/routers/admin.py
(admin.py keeps a thin alias of the same name).
"""
from __future__ import annotations

import structlog

_log = structlog.get_logger()


async def recompute_after_status_change(pool, tour_id: str, new_status: str) -> None:
    """AA-713 / AA-723 — when a master tour leaves (inactive/trashed) or re-enters (active) the
    active set, its atoms must leave/re-enter the Score + Route caches the tenant Slate reads
    (both read atoms through `v_active_tour_atoms`, migration 203 / 271). The recompute runs as a
    durable `recompute` job on the worker (scope=platform). Debounced: a burst of status flips
    reuses one pending job. Best-effort to enqueue — a status change must not fail if the queue
    insert does.

    AA-743 — scoped: a tour leaving the active set has its ranking/route rows superseded right
    away (no platform pass), and the debounced platform job re-lands questions only for the
    Segments this tour touched."""
    try:
        from services.export.handler import remove_tour_from_caches, tour_segment_ids
        from services.jobs.recompute_job import STATUS_DEBOUNCE_S, enqueue_recompute
        async with pool.acquire() as conn:
            if new_status != "active":
                await remove_tour_from_caches(conn, tour_id)
            segment_ids = await tour_segment_ids(conn, tour_id)
        job_id, created = await enqueue_recompute(
            pool, scope="platform", reason=f"master_status={new_status}:{tour_id}",
            created_by="admin:status_change", segment_ids=segment_ids, debounce_s=STATUS_DEBOUNCE_S)
        _log.info("aa713_recompute_enqueued", tour_id=tour_id, new_status=new_status,
                  job_id=job_id, created=created, segments=len(segment_ids))
    except Exception:
        _log.warning("aa713_recompute_enqueue_failed", tour_id=tour_id, exc_info=True)
    # AA-712 — the tour graph follows the live set; a tour (re-)entering also gets its places.
    try:
        from services.places.enqueue import after_liveness_change
        await after_liveness_change(pool, tour_id=str(tour_id), new_status=new_status,
                                    created_by="admin:status_change")
    except Exception:
        _log.warning("aa712_place_enqueue_failed", tour_id=tour_id, exc_info=True)


async def recheck_trip_pages(pool, tour_ids) -> None:
    """ADR 0015 decision 2 (last paragraph): recheck the public trip pages of the tours whose state changed, so
    acp_deliver.tenant_tour_pages.url_alive (the T10 gate F6 reads it) follows at once. Same rule as
    POST /admin/trip-pages/recheck, scoped to these tours. The page itself (GET /v1/trip/{id}) already reads the live
    state on every render; the SEO answer for a retired page (410 / 301) is slice L4."""
    if not tour_ids:
        return
    try:
        from api.routers.v1_trip_page import recheck_trip_pages_for
        async with pool.acquire() as conn:
            await recheck_trip_pages_for(conn, tour_ids)
    except Exception:
        _log.warning("aa986_trip_page_recheck_failed", tours=len(tour_ids), exc_info=True)


async def run_after_commit(pool, outcome, recompute=None) -> None:
    """Post-commit work of one lifecycle outcome. `recompute` defaults to recompute_after_status_change (a caller may
    pass its own, e.g. a router's alias that tests patch)."""
    recompute = recompute or recompute_after_status_change
    for tour_id, new_status in outcome.recompute:
        await recompute(pool, tour_id, new_status)
    await recheck_trip_pages(pool, list(dict.fromkeys(t for t, _ in outcome.recompute)))
    if outcome.cancelled_jobs:
        from shared.jobs.registry import run_terminal_hook
        for job in outcome.cancelled_jobs:
            await run_terminal_hook(pool, job["id"])
