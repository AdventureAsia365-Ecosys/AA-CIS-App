"""AA-995 — job kind `photo_medium_backfill`: render the 960 w WebP ("medium", services/photos/sync.py SIZES) of
every catalog photo stored before migration 301, from the files already in the photo bucket. No Drive download, no
model call: S3 GET + PUT and Pillow work on the worker only.

Payload: {"limit": int (rows this run, default 1500, 1..5000), "country": str | None, "dry_run": bool}.

Bounded and resumable:
  - one run writes at most ``limit`` rows; a row is done the moment its own UPDATE commits, so a cancelled, released
    or failed run loses nothing and the next run starts with what is still missing (s3_key_medium IS NULL);
  - rows are read in (sha256, id) order with a keyset cursor, so a row that fails is passed (listed in the result) and
    retried by the next run, never looped on in this one;
  - the same file synced into several tour folders (one row per Drive file, one sha256) is decoded once: the rendition
    of the previous row is reused while the sha256 is the same (each row still gets its own key, like the sync).
Source: the stored original (JPEGs decoded at a reduced scale, sync.make_sizes), else the 1600 w WebP when the
original is missing or can not be decoded (HEIC, TIFF Pillow can not open).

The photo sync also writes these keys. To not overwrite a fresh rendition with one from older bytes, a run stops
(``stopped: photo_sync_running``, success) while a photo_sync job is running, checked before every batch, and the
UPDATE only lands when the row still has the sha256 the rendition was made from and no medium key yet.

Background kind, concurrency 1, 2 attempts. Observable: the Jobs page (progress, result: written, reused, from_large,
errors, remaining) and /admin/photos/summary (`with_medium`). Enqueue: POST /admin/photos/medium-backfill.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import structlog

from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "photo_medium_backfill"
DEFAULT_LIMIT = 1500
MAX_LIMIT = 5000
BATCH = 50
MAX_CONSECUTIVE_ERRORS = 10   # e.g. the task role lost s3:PutObject: stop instead of failing every row
MAX_LISTED_ERRORS = 20

_MISSING = """
      FROM shared.place_photo
     WHERE s3_key_medium IS NULL AND s3_key_large IS NOT NULL AND status <> 'rejected'
       AND ($1::text IS NULL OR country = $1)
"""
COUNT_SQL = "SELECT count(*)" + _MISSING
BATCH_SQL = ("SELECT id::text AS id, country, drive_file_id, s3_key_original, s3_key_large, sha256" + _MISSING + """
       AND (coalesce(sha256, ''), id) > ($2::text, $3::uuid)
     ORDER BY coalesce(sha256, ''), id
     LIMIT $4""")
SET_SQL = """
    UPDATE shared.place_photo SET s3_key_medium = $2, updated_at = now()
     WHERE id = $1::uuid AND s3_key_medium IS NULL AND sha256 IS NOT DISTINCT FROM $3
"""
SYNC_RUNNING_SQL = "SELECT 1 FROM shared.job WHERE kind = 'photo_sync' AND status = 'running' LIMIT 1"
_FIRST_ID = "00000000-0000-0000-0000-000000000000"


def _limit(payload: dict) -> int:
    raw = payload.get("limit", DEFAULT_LIMIT)
    if raw is None:
        return DEFAULT_LIMIT
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_LIMIT:
        raise NonRetryable(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return raw


def _s3():
    import boto3
    from botocore.config import Config

    return boto3.client("s3", config=Config(connect_timeout=3, read_timeout=60,
                                            retries={"max_attempts": 3, "mode": "standard"}))


def _get(s3, bucket: str, key: str) -> bytes:
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def render(s3, bucket: str, row: dict) -> tuple[bytes, str]:
    """The 960 w WebP of one row and where it came from ("original" | "large"). Raises when neither file renders."""
    from services.photos.sync import MAX_BYTES, SIZES, make_sizes

    widths = {"medium": SIZES["medium"]}
    error: Optional[Exception] = None
    for source in ("original", "large"):
        key = row.get(f"s3_key_{source}")
        if not key:
            continue
        try:
            data = _get(s3, bucket, key)
            if len(data) > MAX_BYTES:
                raise ValueError(f"{source} is {len(data)} bytes (max {MAX_BYTES})")
            out, _, _ = make_sizes(data, widths)
            return out["medium"], source
        except Exception as exc:   # an undecodable original (HEIC, TIFF): try the 1600 w WebP
            error = exc
    raise error or ValueError("no stored file to render from")


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=1800, background=True)
async def run(ctx: JobContext, *, s3: Any = None) -> dict:
    from services.photos.sync import photo_bucket, s3_key

    payload = ctx.payload or {}
    limit = _limit(payload)
    country = payload.get("country") or None
    if country is not None and not isinstance(country, str):
        raise NonRetryable("country must be a string")
    dry_run = bool(payload.get("dry_run"))
    pool = ctx.pool
    missing = int(await pool.fetchval(COUNT_SQL, country))
    res: dict[str, Any] = {"country": country, "dry_run": dry_run, "limit": limit, "missing_before": missing,
                           "written": 0, "reused": 0, "from_original": 0, "from_large": 0, "changed": 0,
                           "failed": 0, "errors": [], "stopped": None}
    if dry_run or not missing:
        res["remaining"] = missing
        ctx.set_result(res)
        return res

    s3 = s3 or await asyncio.to_thread(_s3)
    bucket = photo_bucket()
    cursor = ("", _FIRST_ID)
    last: Optional[tuple[str, bytes]] = None       # (sha256, rendition) of the previous row
    done = consecutive = 0
    while done < limit:
        if await pool.fetchval(SYNC_RUNNING_SQL):
            res["stopped"] = "photo_sync_running"
            break
        rows = await pool.fetch(BATCH_SQL, country, cursor[0], cursor[1], min(BATCH, limit - done))
        if not rows:
            break
        for r in rows:
            row = dict(r)
            cursor = (row["sha256"] or "", row["id"])
            done += 1
            try:
                sha = row["sha256"]
                if sha and last and last[0] == sha:
                    body = last[1]
                    res["reused"] += 1
                else:
                    body, source = await asyncio.to_thread(render, s3, bucket, row)
                    res[f"from_{source}"] += 1
                    last = (sha, body) if sha else None
                key = s3_key(row["country"], row["drive_file_id"], "medium")
                await asyncio.to_thread(s3.put_object, Bucket=bucket, Key=key, Body=body, ContentType="image/webp",
                                        CacheControl="public, max-age=31536000")
                updated = await pool.execute(SET_SQL, row["id"], key, sha)
                if updated.split()[-1] == "1":
                    res["written"] += 1
                else:
                    res["changed"] += 1     # the sync rewrote the row meanwhile (new bytes, its own medium key)
                consecutive = 0
            except Exception as exc:
                consecutive += 1
                res["failed"] += 1
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
                if len(res["errors"]) < MAX_LISTED_ERRORS:
                    res["errors"].append({"photo_id": row["id"], "error": error})
                logger.warning("photo_medium_backfill_row_failed", photo_id=row["id"], error=error)
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    res["stopped"] = "too_many_errors"
                    break
            if done % 10 == 0:
                await ctx.progress(phase="rendering", done=done, written=res["written"], failed=res["failed"],
                                   missing_before=missing)
        if res["stopped"]:
            break
    res["remaining"] = int(await pool.fetchval(COUNT_SQL, country))
    ctx.set_result(res)
    await ctx.progress(phase="done", done=done, written=res["written"], failed=res["failed"],
                       remaining=res["remaining"])
    logger.info("photo_medium_backfill_done", **{k: v for k, v in res.items() if k != "errors"})
    if res["stopped"] == "too_many_errors":
        raise NonRetryable(f"{MAX_CONSECUTIVE_ERRORS} photos failed in a row (last: {res['errors'][-1]['error']}); "
                           "stopped — a new run resumes with the photos still missing")
    return res
