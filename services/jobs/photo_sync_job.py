"""AA-708 — `photo_sync` job kind: copy the CON board's marketing photos from Google Drive to S3 and
match them to tours and destinations (services/photos/sync.py). No paid model calls.

Payload: {"folders": [{"country", "folder_id"}] (default: sync.CON_FOLDERS), "limit": int|None,
          "dry_run": bool, "match_destinations": bool (default False: a photo without a destination gets one
          when its label names exactly one place of its tour; True also re-matches the automatic destinations of the
          tours whose places were read — see sync.sync_folder, AA-671 S257; a hand-set destination never changes),
          "trigger": "schedule" | "manual" (default "manual"; copied into the result)}

AA-844: a daily schedule (AA-CIS-Infra ops-probe Lambda, 21:00 UTC = 04:00 ICT) calls
POST /admin/photos/sync/schedule -> enqueue_scheduled(): one incremental job per CON country,
idempotent per UTC day. /admin/photos/summary reports the latest scheduled day (last_scheduled).
"""
from __future__ import annotations

from datetime import timezone
from typing import Optional

import asyncpg
import boto3
import structlog

from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "photo_sync"


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=1800)
async def run(ctx: JobContext) -> dict:
    from services.photos.drive import DriveClient, load_credentials
    from services.photos.sync import CON_FOLDERS, fill_destination_covers, sync_folder
    from shared.secrets import get_database_url

    folders = ctx.payload.get("folders") or CON_FOLDERS
    if not all(f.get("country") and f.get("folder_id") for f in folders):
        raise NonRetryable("each folder needs country and folder_id")
    limit = ctx.payload.get("limit")
    dry_run = bool(ctx.payload.get("dry_run"))
    match_dest = bool(ctx.payload.get("match_destinations"))
    try:
        creds = load_credentials()
    except Exception as e:
        raise NonRetryable(f"Google Drive credentials not available: {e}")

    client = DriveClient(creds)
    s3 = boto3.client("s3")
    conn = await asyncpg.connect(get_database_url())
    summary: dict = {"folders": [], "dry_run": dry_run, "match_destinations": match_dest,
                     "trigger": ctx.payload.get("trigger") or "manual"}
    try:
        for f in folders:
            async def _progress(**p):
                await ctx.progress(phase="syncing", **p)
            res = await sync_folder(conn, client, s3, country=f["country"], root_id=f["folder_id"],
                                    limit=limit, dry_run=dry_run, match_destinations=match_dest,
                                    progress=_progress)
            summary["folders"].append(res)
            logger.info("photo_sync_folder_done", **res)
        summary["covers_set"] = await fill_destination_covers(conn) if match_dest and not dry_run else 0
    finally:
        await conn.close()
        await client.aclose()
    totals = {k: sum(r[k] for r in summary["folders"]) for k in
              ("images", "downloaded", "unchanged", "matched_tour", "matched_destination", "destination_ambiguous",
               "destination_weak", "unmatched", "errors")}
    summary["totals"] = totals
    ctx.set_result(summary)
    await ctx.progress(phase="done", **totals)
    return summary


# ── AA-844: the daily schedule ──────────────────────────────────────────────────────────────────

SCHEDULE_CREATED_BY = "scheduler:aa-844"
SCHEDULE_KEY_PREFIX = "photo_sync:schedule:"

_ACTIVE_SQL = f"""
    SELECT id::text AS id, status FROM shared.job
     WHERE kind = '{KIND}' AND status IN ('queued', 'running')
     ORDER BY created_at DESC LIMIT 1
"""


async def enqueue_scheduled(pool, *, day: str, created_by: str = SCHEDULE_CREATED_BY) -> dict:
    """The daily run: one incremental photo_sync job per CON folder (country), so one country's
    Drive quota stop or failure does not hide the others and the summary reads per country.

    One transaction under an advisory lock: skipped while any photo_sync job (a manual run, or the
    previous schedule still going) is queued or running; otherwise every country is enqueued with
    idempotency_key photo_sync:schedule:<slug>:<day>, so a Scheduler retry or a second call the same
    UTC day creates nothing new (created=False). Not skipped during a rerun wave: no LLM work, Drive
    and S3 I/O only (retention's wave check deliberately not copied)."""
    from services.photos.sync import CON_FOLDERS, country_slug
    from shared.jobs.registry import enqueue

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{KIND}:schedule")
            active = await conn.fetchrow(_ACTIVE_SQL)
            if active:
                return {"enqueued": [],
                        "skipped": f"{KIND} job {active['id']} already {active['status']}"}
            out = []
            for f in CON_FOLDERS:
                job_id, created = await enqueue(
                    conn, KIND, {"folders": [f], "trigger": "schedule", "match_destinations": False},
                    idempotency_key=f"{SCHEDULE_KEY_PREFIX}{country_slug(f['country'])}:{day}",
                    created_by=created_by)
                out.append({"country": f["country"], "job_id": job_id, "created": created})
    return {"enqueued": out, "skipped": None}


# Latest scheduled job per country over the last 8 days. A failed job has no result (set_result is
# never reached), so its errors are NULL, which last_scheduled() does not count as ok.
LAST_SCHEDULED_SQL = f"""
    SELECT DISTINCT ON (payload -> 'folders' -> 0 ->> 'country')
           payload -> 'folders' -> 0 ->> 'country' AS country, id::text AS id, status, created_at,
           finished_at, (result -> 'totals' ->> 'errors')::int AS errors
      FROM shared.job
     WHERE kind = '{KIND}' AND created_by = $1 AND created_at > now() - interval '8 days'
     ORDER BY payload -> 'folders' -> 0 ->> 'country', created_at DESC
"""


def scheduled_status(rows, countries: list[str]) -> Optional[dict]:
    """{day, countries: [{country, status, errors, job_id, finished_at}], all_ok} for the latest UTC
    day the schedule ran, or None when it has not run in the last 8 days. A country whose latest
    scheduled job is older than that day is listed with status None (not run that day). all_ok only
    when every CON country has a job that day that succeeded with 0 errors."""
    rows = [dict(r) for r in rows]
    if not rows:
        return None

    def _day(r):
        return r["created_at"].astimezone(timezone.utc).date()

    day = max(_day(r) for r in rows)
    by_country = {r["country"]: r for r in rows if _day(r) == day}
    out = []
    for c in countries:
        r = by_country.get(c)
        out.append({"country": c, "status": r["status"] if r else None,
                    "errors": r["errors"] if r else None, "job_id": r["id"] if r else None,
                    "finished_at": r["finished_at"].isoformat() if r and r["finished_at"] else None})
    all_ok = bool(out) and all(d["status"] == "succeeded" and d["errors"] == 0 for d in out)
    return {"day": day.isoformat(), "countries": out, "all_ok": all_ok}


async def last_scheduled(conn) -> Optional[dict]:
    from services.photos.sync import CON_FOLDERS

    rows = await conn.fetch(LAST_SCHEDULED_SQL, SCHEDULE_CREATED_BY)
    return scheduled_status(rows, [f["country"] for f in CON_FOLDERS])
