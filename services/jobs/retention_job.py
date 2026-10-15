"""AA-825 — retention job kinds (S237 E3; data standards DM-7.2, C5.4). One kind per table:

  retention_llm_call_log            180 d raw, daily rollup shared.llm_call_daily, archive
  retention_tenant_api_usage         90 d raw, monthly rollup shared.tenant_api_usage_monthly, archive
  retention_job                      succeeded 30 d; failed / cancelled / stopped_budget 90 d (archive)
  retention_decision_log            180 d (= Jev cache max age), rows with a human outcome kept, archive
  retention_cost_explorer_snapshot  rows superseded by a newer fetch of the same (account, service, day)

Payload: {"trigger": "schedule" | "manual", "dry_run": bool, "max_rows": int, "batch_size": int,
          "pause_ms": int}

Mode (DM-7.2):
  - manual, dry_run absent or true -> DRY RUN: counts only, no write anywhere (the default).
  - manual, dry_run false          -> live, deletes at most `max_rows` (required: the number
                                      Nghiệp approved from the DRY RUN), oldest first.
  - schedule                       -> DRY RUN until a live run of this kind with the same windows
                                      has succeeded; then live, unless the candidates exceed
                                      2x the last live run's deletions (floor GROWTH_FLOOR_ROWS):
                                      then HELD — nothing written, a bell for the admins.
Every run is capped at RUN_MAX_ROWS deletions; the rest waits for the next run.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import boto3
import structlog

from services.retention import rollups, runner
from services.retention.archive import S3Archiver
from services.retention.policy import ARCHIVE_BUCKET, SPECS, TableSpec
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

RUN_MAX_ROWS = 200_000
GROWTH_FACTOR = 2
GROWTH_FLOOR_ROWS = runner.BATCH_SIZE_MAX
DEFAULT_PAUSE_MS = 200
HELD_EVENT = "platform.retention.held"
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_LAST_LIVE_SQL = """
    SELECT result FROM shared.job
     WHERE kind = $1 AND status = 'succeeded' AND result ->> 'mode' = 'live'
     ORDER BY finished_at DESC NULLS LAST
     LIMIT 1
"""


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


async def last_live(conn, kind: str) -> Optional[dict]:
    row = await conn.fetchrow(_LAST_LIVE_SQL, kind)
    return _json(row["result"]) if row and row["result"] is not None else None


def decide_mode(payload: dict, windows: dict, candidates: int,
                prev_live: Optional[dict]) -> tuple[str, int, str]:
    """-> (mode, limit_rows, reason); mode is 'dry_run', 'live' or 'held'. Pure, unit-tested."""
    trigger = payload.get("trigger", "manual")
    if trigger != "schedule":
        if payload.get("dry_run", True) is not False:
            return "dry_run", 0, "manual dry run"
        max_rows = payload.get("max_rows")
        if not isinstance(max_rows, int) or max_rows < 1:
            raise NonRetryable("a live manual run needs max_rows (the approved count from a DRY RUN)")
        return "live", min(max_rows, RUN_MAX_ROWS), "manual live run, approved max_rows"
    if prev_live is None:
        return "dry_run", 0, "no approved live run of this kind yet (first run is a DRY RUN)"
    if (prev_live.get("windows") or {}) != windows:
        return "dry_run", 0, "windows changed since the last live run (DRY RUN again)"
    allowed = max(GROWTH_FACTOR * int(prev_live.get("deleted_total") or 0), GROWTH_FLOOR_ROWS)
    if candidates > allowed:
        return "held", 0, (f"{candidates} candidates > {GROWTH_FACTOR}x the last live run "
                           f"({prev_live.get('deleted_total') or 0}, floor {GROWTH_FLOOR_ROWS}); "
                           f"needs a manual live run")
    return "live", min(candidates, RUN_MAX_ROWS), "scheduled live run within the growth limit"


async def _notify_held(conn, spec: TableSpec, result: dict) -> bool:
    """One bell per held run. Best-effort: the job result is the record; the bell is a bonus."""
    try:
        await conn.execute(
            """
            INSERT INTO shared.notifications
                (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
            VALUES ($1::uuid, 'system', $2, 'retention', $3, $4::jsonb, ARRAY['admin'])
            """,
            PLATFORM_TENANT_ID, HELD_EVENT, spec.kind,
            json.dumps({"message": f"Retention of {spec.table} held: {result['reason']}",
                        "kind": spec.kind, "candidates": result["candidates_total"]}, default=str))
        return True
    except Exception as e:
        logger.warning("retention_held_notify_failed", kind=spec.kind, error=str(e)[:200])
        return False


def _resume_point(progress: dict, part: str, id_type: str) -> Optional[tuple]:
    cur = ((progress or {}).get("cursors") or {}).get(part)
    if not cur or not cur.get("ts"):
        return None
    return (datetime.fromisoformat(cur["ts"]), int(cur["id"]) if id_type == "bigint" else cur["id"])


async def run_spec(ctx: JobContext, spec: TableSpec, *, archiver=None) -> dict:
    payload = ctx.payload or {}
    windows = spec.windows()
    progress = ctx.job.progress or {}
    pause_s = max(0, int(payload.get("pause_ms", DEFAULT_PAUSE_MS))) / 1000.0
    async with ctx.pool.acquire() as conn:
        refs = await runner.fk_refs(conn, spec.table)
        cuts, parts = {}, []
        for part in spec.parts:
            cuts[part.name] = await runner.cutoff(conn, part.window_days)
            parts.append(await runner.count_part(conn, spec, part, refs, cuts[part.name]))
        candidates = sum(p.candidates for p in parts)
        prev = await last_live(conn, spec.kind)
        mode, limit_rows, reason = decide_mode(payload, windows, candidates, prev)
        result = {
            "kind": spec.kind, "table": spec.table, "mode": mode, "reason": reason,
            "trigger": payload.get("trigger", "manual"), "windows": windows,
            "candidates_total": candidates, "limit_rows": limit_rows,
            "fk_protected_by": [f"{t}.{c}" for t, c in refs],
            "previous_live_deleted": (prev or {}).get("deleted_total"),
            "rollup": spec.rollup, "rollup_rows": 0, "deleted_total": 0, "archived_total": 0,
            "parts": [p.as_dict() for p in parts],
        }
        if spec.rollup:
            # rollup cutoff = the (only) part's cutoff; whole days / months older than it
            roll_cut = cuts[spec.parts[0].name]
            result["rollup_pending"] = await rollups.pending(conn, spec.rollup, roll_cut)
        if mode == "dry_run":
            await ctx.progress(phase="done", mode=mode, candidates=candidates)
            logger.info("retention_dry_run", kind=spec.kind, candidates=candidates, reason=reason)
            return result
        if mode == "held":
            result["notified"] = await _notify_held(conn, spec, result)
            await ctx.progress(phase="done", mode=mode, candidates=candidates)
            logger.error("retention_held", kind=spec.kind, candidates=candidates, reason=reason)
            return result

        # live: rollups first (DM-7.4), then each part's batches, archived where the policy says so
        if spec.rollup:
            await ctx.progress(phase="rollup", mode=mode)
            result["rollup_rows"] = await rollups.write(conn, spec.rollup, cuts[spec.parts[0].name])
        if archiver is None and any(p.archive for p in spec.parts):
            archiver = S3Archiver(boto3.client("s3"), ctx.job_id)
        already = int(progress.get("deleted_total") or 0)   # an earlier attempt of this same job
        remaining = max(limit_rows - already, 0)
        cursors = dict(progress.get("cursors") or {})
        for part, res in zip(spec.parts, parts):
            if remaining <= 0:
                break

            async def _on_batch(r, _part=part.name):
                cursors[_part] = r.cursor
                await ctx.progress(phase="deleting", mode=mode, part=_part, cursors=cursors,
                                   deleted_total=already + sum(x.deleted for x in parts))
            start = res.deleted
            await runner.delete_part(
                conn, spec, part, refs, cuts[part.name], res, limit_rows=remaining,
                batch_size=int(payload.get("batch_size") or runner.BATCH_SIZE_MAX), archiver=archiver,
                cursor=_resume_point(progress, part.name, spec.id_type), pause_s=pause_s,
                on_batch=_on_batch)
            remaining -= res.deleted - start
        result["parts"] = [p.as_dict() for p in parts]
        result["deleted_total"] = already + sum(p.deleted for p in parts)
        result["archived_total"] = sum(p.archived_rows for p in parts)
        if already:
            result["resumed_after"] = already
    await ctx.progress(phase="done", mode=mode, deleted_total=result["deleted_total"])
    logger.info("retention_live_done", kind=spec.kind, deleted=result["deleted_total"],
                archived=result["archived_total"], rollup_rows=result["rollup_rows"])
    return result


def _register(spec: TableSpec):
    @job_kind(spec.kind, concurrency=1, max_attempts=2, expected_seconds=1800, background=True)
    async def run(ctx: JobContext) -> dict:
        result = await run_spec(ctx, spec)
        ctx.set_result(result)
        return result
    return run


for _spec in SPECS.values():
    _register(_spec)

KINDS = tuple(SPECS)


# ── scheduling and admin status ─────────────────────────────────────────────────────────────────

OPEN_WAVE_SQL = "SELECT count(*) FROM shared.rerun_wave WHERE status = 'open'"


async def schedule_all(pool, *, day: str, created_by: str = "scheduler:aa-825") -> dict:
    """Called once a day by the ops scheduler (POST /admin/job-runner/retention/schedule). Enqueues
    one `trigger=schedule` job per retention kind, idempotent per UTC day. Nothing is enqueued while
    a rerun wave is open (C5.4d: retention I/O stays off a wave)."""
    from shared.jobs.registry import enqueue
    open_waves = int(await pool.fetchval(OPEN_WAVE_SQL) or 0)
    if open_waves:
        return {"enqueued": [], "skipped": f"{open_waves} rerun wave(s) open"}
    out = []
    for kind in KINDS:
        job_id, created = await enqueue(pool, kind, {"trigger": "schedule"},
                                        idempotency_key=f"retention:{kind}:{day}", created_by=created_by)
        out.append({"kind": kind, "job_id": job_id, "created": created})
    return {"enqueued": out, "skipped": None}


_STATUS_JOBS_SQL = """
    SELECT DISTINCT ON (kind, bucket) kind, bucket, id, status, created_at, finished_at, result
      FROM (SELECT j.*, CASE WHEN j.status IN ('queued', 'running') THEN 'active'
                             WHEN j.status = 'succeeded' THEN coalesce(j.result ->> 'mode', 'unknown')
                             ELSE 'failed' END AS bucket
              FROM shared.job j
             WHERE j.kind = ANY($1::text[])) x
     ORDER BY kind, bucket, created_at DESC
"""

_RESULT_KEYS = ("mode", "reason", "trigger", "candidates_total", "deleted_total", "archived_total",
                "rollup_rows", "rollup_pending", "limit_rows")


def _job_summary(r) -> dict:
    res = _json(r["result"]) or {}
    return {"job_id": str(r["id"]), "status": r["status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            **{k: res.get(k) for k in _RESULT_KEYS if k in res}}


async def status(pool) -> dict:
    """Per kind: policy, approximate table size, and the latest active / live / dry_run / held /
    failed job. `scheduled_mode` says what tomorrow's scheduled run will do (before counting)."""
    rows = await pool.fetch(_STATUS_JOBS_SQL, list(KINDS))
    by_kind: dict[str, dict] = {k: {} for k in KINDS}
    for r in rows:
        by_kind[r["kind"]][r["bucket"]] = _job_summary(r)
    tables = [s.table for s in SPECS.values()] + ["shared.llm_call_daily", "shared.tenant_api_usage_monthly"]
    est = {r["t"]: int(r["n"]) for r in await pool.fetch(
        "SELECT format('%I.%I', n.nspname, c.relname) AS t, greatest(c.reltuples, 0)::bigint AS n "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE format('%I.%I', n.nspname, c.relname) = ANY($1::text[])", tables)}
    out = []
    for kind, spec in SPECS.items():
        jobs = by_kind[kind]
        live = jobs.get("live")
        live_windows = None
        if live:
            full = await last_live(pool, kind)
            live_windows = (full or {}).get("windows")
        out.append({
            "kind": kind, "table": spec.table, "rows_estimate": est.get(spec.table),
            "rollup": spec.rollup and f"shared.{spec.rollup}",
            "rollup_rows_estimate": est.get(f"shared.{spec.rollup}") if spec.rollup else None,
            "parts": [{"part": p.name, "window_days": p.window_days, "archive": p.archive} for p in spec.parts],
            "scheduled_mode": "live" if live and live_windows == spec.windows() else "dry_run",
            "last": jobs,
        })
    return {"kinds": out, "run_max_rows": RUN_MAX_ROWS, "growth_factor": GROWTH_FACTOR,
            "growth_floor_rows": GROWTH_FLOOR_ROWS, "archive": f"s3://{ARCHIVE_BUCKET}/archive/<table>/yyyy/mm/"}
