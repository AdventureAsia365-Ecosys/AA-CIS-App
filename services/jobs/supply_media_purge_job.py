"""AA-835 — job kind `supply_media_purge`: take the CIS copies of one Booking supply photo out of the photo bucket
and the CDN once Booking withdrew its usage rights or removed it (DM-7 row "supply_media": a supply_media row and its
CIS photo objects go when Booking removes the media).

Queued by the supply feed consumer (services/supply_content/pull.py) in the same transaction as the media change,
key ``supply_media_purge:<media_id>:<copied_at in epoch microseconds>`` (one job per copy: a photo copied again later
and withdrawn again gets its own job). Payload: ``{"media_id", "copied_sha256", "copied_at_us"}``.

  1. Re-reads the shared.supply_media row. Skips (success) when the copy it names is gone or was replaced, or when
     the media has usage_rights and is active again (rights restored before the job ran: the copy stays).
  2. Deletes both WebP objects (photos/supply/<type>/<media_id>-{1600,600}.webp) from the CIS photo bucket. A delete
     of a key that is already gone succeeds, so a retry is safe. AccessDenied is NonRetryable: the task role needs
     s3:DeleteObject on photos/supply/* (AA-CIS-Infra, AA-835 purge grant).
  3. Clears the copy columns (cis_key_*, url_*, copied_sha256, copied_at) of that same copy in one UPDATE
     (supply_media_copy_check allows all-NULL). catalog.v_supply_content already left the photo out from the moment
     the pull flipped usage_rights / active; this makes the stored state match the bucket.
  4. Invalidates ``/supply/<type>/<media_id>-*`` on the photos CloudFront distribution when PHOTO_CDN_DISTRIBUTION_ID
     is set (CDN TTL is up to a year, min one day, so a deleted object can stay at the edge otherwise). Best effort:
     a failure is in the job result and a warning line, the job still succeeds (the S3 objects and the row are
     already gone, a retry could not find the copy again). Unset: ``invalidation: not_configured``.

Background kind, concurrency 1, 3 attempts. Observable: the Jobs page (result: deleted keys, invalidation id or
error), log lines ``supply_media_purged`` / ``supply_media_purge_invalidation_failed``.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import structlog

from services.supply_content.pull import PURGE_KIND, PURGE_MAX_ATTEMPTS
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = PURGE_KIND
KEY_PREFIX = "photos/supply/"
CDN_KEY_PREFIX = "photos/"          # the CDN serves the bucket's photos/ prefix at its root (services/photos/sync.py)

ROW_SQL = """
    SELECT usage_rights, active, cis_key_1600, cis_key_600, copied_sha256,
           (extract(epoch FROM copied_at) * 1000000)::bigint AS copied_at_us
      FROM shared.supply_media WHERE booking_media_id = $1::uuid
"""
CLEAR_SQL = """
    UPDATE shared.supply_media
       SET cis_key_1600 = NULL, cis_key_600 = NULL, url_1600 = NULL, url_600 = NULL, copied_sha256 = NULL,
           copied_at = NULL, updated_at = now()
     WHERE booking_media_id = $1::uuid AND copied_sha256 = $2
       AND (extract(epoch FROM copied_at) * 1000000)::bigint = $3
"""


def distribution_id() -> Optional[str]:
    value = (os.environ.get("PHOTO_CDN_DISTRIBUTION_ID") or "").strip()
    return value or None


def invalidation_path(key: str) -> str:
    """CDN path covering both sizes and every ?v= variant of one media: /supply/<type>/<media_id>-*."""
    stem = key[len(CDN_KEY_PREFIX):].rsplit("-", 1)[0]
    return f"/{stem}-*"


def _client(service: str):
    import boto3
    from botocore.config import Config

    return boto3.client(service, config=Config(connect_timeout=3, read_timeout=20,
                                               retries={"max_attempts": 2, "mode": "standard"}))


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) if hasattr(exc, "response") else ""


def _delete(s3, bucket: str, keys: list[str]) -> None:
    for key in keys:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:
            if _error_code(exc) in ("AccessDenied", "403"):
                raise NonRetryable(f"s3:DeleteObject denied on {key}: the CIS task role needs the photos/supply/* "
                                   f"delete grant") from exc
            raise


def _invalidate(cf, dist: str, path: str, reference: str) -> str:
    resp = cf.create_invalidation(DistributionId=dist, InvalidationBatch={
        "Paths": {"Quantity": 1, "Items": [path]}, "CallerReference": reference})
    return str(resp["Invalidation"]["Id"])


@job_kind(KIND, concurrency=1, max_attempts=PURGE_MAX_ATTEMPTS, expected_seconds=60, background=True)
async def run(ctx: JobContext, *, s3: Any = None, cf: Any = None) -> dict:
    from services.photos.sync import photo_bucket

    payload = ctx.payload or {}
    media_id, sha, copied_at_us = payload.get("media_id"), payload.get("copied_sha256"), payload.get("copied_at_us")
    if not isinstance(media_id, str) or not isinstance(sha, str) or not isinstance(copied_at_us, int):
        raise NonRetryable("payload needs media_id (str), copied_sha256 (str) and copied_at_us (int)")
    base = {"media_id": media_id}
    row = await ctx.pool.fetchrow(ROW_SQL, media_id)
    if row is None or row["copied_sha256"] != sha or row["copied_at_us"] != copied_at_us:
        return {**base, "skipped": "copy_gone"}
    if row["usage_rights"] and row["active"]:
        return {**base, "skipped": "rights_restored"}
    keys = [k for k in (row["cis_key_1600"], row["cis_key_600"]) if k]
    if any(not k.startswith(KEY_PREFIX) for k in keys):
        raise NonRetryable("a copy key is outside photos/supply/")

    s3 = s3 or await asyncio.to_thread(_client, "s3")
    await asyncio.to_thread(_delete, s3, photo_bucket(), keys)
    await ctx.pool.execute(CLEAR_SQL, media_id, sha, copied_at_us)

    result: dict[str, Any] = {**base, "deleted": keys, "invalidation": "not_configured"}
    dist = distribution_id()
    if dist and keys:
        try:
            cf = cf or await asyncio.to_thread(_client, "cloudfront")
            result["invalidation"] = await asyncio.to_thread(
                _invalidate, cf, dist, invalidation_path(keys[0]), f"{KIND}:{media_id}:{copied_at_us}")
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning("supply_media_purge_invalidation_failed", media_id=media_id, error=error)
            result["invalidation"] = {"error": error}
    logger.info("supply_media_purged", media_id=media_id, keys=len(keys))
    return result
