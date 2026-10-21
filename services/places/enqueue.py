"""AA-712 — enqueue the place-pipeline jobs (ADR 0003 layer A: triggers only enqueue).

  place_extract     per tour; from the end of A3 atomize (key place_extract:<version_id>:<atomize
                    job>), set-active / restore (reuses a queued job of the same tour) and the admin
                    backfill (key per batch).
  place_geocode     platform, debounced: ONE queued job absorbs every enqueue; its start slides to
                    now + debounce, capped at created_at + MAX_WAIT_S (the recompute pattern, AA-743).
  tour_graph_build  platform, debounced the same way.
  place_shape       AA-847, platform, debounced the same way: geometry of line places (rivers, roads, trails),
                    enqueued by place_geocode when it stored a new line, and by the admin shapes backfill.

Every trigger is best-effort: callers wrap these in try/except — a place job must never fail a
publish, an atomize or an admin action.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from shared.jobs.registry import enqueue

logger = structlog.get_logger()

# S236 (Nghiệp, paid-run rule): automatic triggers (end of A3 atomize, place-card relink) only enqueue
# place_extract when this env var is on. Default off until the 3-tour trial and the first country
# pass; the admin backfill endpoint is not gated (it is the controlled way to run batches).
AUTO_ENV = "PLACE_PIPELINE_AUTO"
_ON_VALUES = {"1", "true", "on", "yes"}


def auto_enabled() -> bool:
    return os.environ.get(AUTO_ENV, "").strip().lower() in _ON_VALUES


EXTRACT_KIND = "place_extract"
GEOCODE_KIND = "place_geocode"
GRAPH_KIND = "tour_graph_build"
SHAPE_KIND = "place_shape"
DEBOUNCE_S = 180      # = recompute WAVE_DEBOUNCE_S
MAX_WAIT_S = 1200     # = recompute MAX_WAIT_S

# The oldest QUEUED job of a platform kind absorbs this enqueue (FOR UPDATE SKIP LOCKED + status
# re-check: a job being claimed right now is left alone and a new one is queued instead).
_COALESCE_SQL = """
    UPDATE shared.job j SET
        payload = j.payload || jsonb_build_object('coalesced', coalesce((j.payload->>'coalesced')::int, 0) + 1),
        run_after = GREATEST(j.run_after,
                             LEAST(j.created_at + make_interval(secs => $3), now() + make_interval(secs => $2)))
    WHERE j.id = (SELECT id FROM shared.job WHERE kind = $1 AND status = 'queued'
                  ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
      AND j.status = 'queued'
    RETURNING j.id
"""


async def enqueue_debounced(db, kind: str, *, reason: str, created_by: str = "system",
                            debounce_s: int = DEBOUNCE_S) -> tuple[str, bool]:
    """(job_id, created) — created False when a queued job absorbed it."""
    reused = await db.fetchval(_COALESCE_SQL, kind, debounce_s, MAX_WAIT_S)
    if reused is not None:
        return str(reused), False
    run_after = datetime.now(timezone.utc) + timedelta(seconds=debounce_s) if debounce_s else None
    return await enqueue(db, kind, {"reason": reason}, created_by=created_by, run_after=run_after)


async def enqueue_place_extract(db, *, tour_id: str, version_id: Optional[str], reason: str,
                                created_by: str, idempotency_key: Optional[str] = None,
                                force: bool = False) -> tuple[str, bool]:
    """One tour. Without a key, a QUEUED place_extract of the same tour is reused (a burst of
    set-active / restore clicks queues one job)."""
    if idempotency_key is None:
        existing = await db.fetchval(
            "SELECT id FROM shared.job WHERE kind = $1 AND status = 'queued' AND payload->>'tour_id' = $2 "
            "ORDER BY created_at LIMIT 1", EXTRACT_KIND, str(tour_id))
        if existing is not None:
            return str(existing), False
    payload = {"tour_id": str(tour_id), "version_id": str(version_id) if version_id else None,
               "reason": reason, "force": bool(force)}
    return await enqueue(db, EXTRACT_KIND, payload, idempotency_key=idempotency_key, created_by=created_by)


async def after_atomize(db, *, tour_id: str, version_id: str, atomize_job_id: Optional[str]) -> dict:
    """End of A3 atomize (publish, re-upload, admin re-atomize). Keyed on the content version and
    the atomize job, so a retried atomize job does not queue twice, while an admin re-atomize of
    the same version (new atomize job) does rebuild — its atoms may have changed."""
    if not auto_enabled():
        logger.info("place_extract_auto_off", tour_id=str(tour_id), version_id=str(version_id))
        return {"place_extract_job": None, "place_extract_skipped": f"{AUTO_ENV} off"}
    key = f"{EXTRACT_KIND}:{version_id}" + (f":{atomize_job_id}" if atomize_job_id else "")
    job_id, created = await enqueue_place_extract(
        db, tour_id=tour_id, version_id=version_id, reason="a3_atomize", created_by="a3_atomize",
        idempotency_key=key)
    return {"place_extract_job": job_id, "place_extract_job_reused": not created}


async def after_liveness_change(db, *, tour_id: str, new_status: str, created_by: str) -> dict:
    """Master status change / trash / restore / set-active. Views filter liveness themselves, so a
    tour leaving needs only a graph rebuild; a tour (re-)entering also gets its places extracted
    (free when its hashes are unchanged)."""
    out: dict = {}
    if new_status == "active":
        job_id, _ = await enqueue_place_extract(
            db, tour_id=tour_id, version_id=None, reason=f"status:{new_status}", created_by=created_by)
        out["place_extract_job"] = job_id
    job_id, _ = await enqueue_debounced(db, GRAPH_KIND, reason=f"status:{new_status}:{tour_id}",
                                        created_by=created_by)
    out["graph_job"] = job_id
    return out


# ── AA-847 Problem 2: the automatic duplicate-place merge ───────────────────────────────────────────────────────────
# place_dedupe  platform, debounced the same way: enqueued by place_geocode when it wrote or moved a destination, by
#               place_shape when a shape is drawn (the lines lane calls enqueue_dedupe), by a merge undo and by the
#               admin run endpoint. Default payload mode "apply" (writes only for allow-listed countries in mode apply).
DEDUPE_KIND = "place_dedupe"


async def enqueue_dedupe(db, *, reason: str, created_by: str = "system") -> tuple[str, bool]:
    return await enqueue_debounced(db, DEDUPE_KIND, reason=reason, created_by=created_by)
