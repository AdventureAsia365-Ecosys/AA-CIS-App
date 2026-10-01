"""AA-708 — `photo_sync` job kind: copy the CON board's marketing photos from Google Drive to S3 and
match them to tours and destinations (services/photos/sync.py). No paid model calls.

Payload: {"folders": [{"country", "folder_id"}] (default: sync.CON_FOLDERS), "limit": int|None,
          "dry_run": bool, "match_destinations": bool (default False — see sync.sync_folder)}
"""
from __future__ import annotations

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
    summary: dict = {"folders": [], "dry_run": dry_run, "match_destinations": match_dest}
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
              ("images", "downloaded", "unchanged", "matched_tour", "matched_destination", "unmatched", "errors")}
    summary["totals"] = totals
    ctx.set_result(summary)
    await ctx.progress(phase="done", **totals)
    return summary
