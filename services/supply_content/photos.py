"""AA-835 — copy the rights-cleared Booking photos of one supply entity into the CIS photo store.

Only a photo whose Booking media has ``usage_rights`` and is active is copied. The source location comes from
``booking.v_supply_media`` (bucket + key; never from a URL), and is checked before any S3 call: the bucket must match
the bk-023 naming rule (and SUPPLY_MEDIA_BUCKET when that env pin is set), the key the bk-023 key shape of THIS
media id. The bytes must hash to the sha256 Booking recorded, at most MAX_BYTES. The copies are the two WebP sizes of
the CON photo sync (services/photos/sync.make_sizes) under ``photos/supply/<entity_type>/<media_id>-<w>.webp`` in
the CIS photo bucket, served by the img. CDN (cdn_url, cache-busted with the sha). The CIS task role reads the
Booking bucket through its s3:GetObject grant (Infra AA-835 S2 bucket policy); it never writes there.

A photo that fails (missing object, hash mismatch, bad image) is reported in the result and stays uncopied: a
version that uses it cannot be published (review.publish refuses uncopied photos).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from typing import Any, Optional

import structlog

from services.photos.sync import cdn_url, make_sizes, photo_bucket

logger = structlog.get_logger()

MAX_BYTES = 10 * 1024 * 1024
BOOKING_BUCKET_RE = re.compile(r"^aa-booking-[a-z0-9]+-supply-media-[0-9]{12}$")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SOURCE_KEY_RE = re.compile(rf"^supply-media/(hotel|service)/({_UUID})/({_UUID})\.(jpg|png|webp)$")
SIZES = {"1600": "large", "600": "small"}

PENDING_SQL = """
    SELECT booking_media_id::text AS media_id, sha256
      FROM shared.supply_media
     WHERE entity_id = $1::uuid AND usage_rights AND active AND copied_sha256 IS DISTINCT FROM sha256
"""
SOURCE_SQL = """
    SELECT m.media_id::text AS media_id, m.entity_type, m.entity_id::text AS entity_id, m.usage_rights,
           m.s3_bucket, m.s3_key, m.sha256, m.content_type
      FROM booking.v_supply_media m
     WHERE m.media_id = ANY($1::uuid[])
"""
COPIED_SQL = """
    UPDATE shared.supply_media
       SET cis_key_1600 = $2, cis_key_600 = $3, url_1600 = $4, url_600 = $5, copied_sha256 = $6, copied_at = now(),
           updated_at = now()
     WHERE booking_media_id = $1::uuid AND sha256 = $6
"""


class PhotoRefused(Exception):
    pass


def allowed_bucket(name: Optional[str]) -> bool:
    if not name or not BOOKING_BUCKET_RE.fullmatch(name):
        return False
    pinned = (os.environ.get("SUPPLY_MEDIA_BUCKET") or "").strip()
    return not pinned or name == pinned


def cis_key(entity_type: str, media_id: str, width: str) -> str:
    return f"photos/supply/{entity_type}/{media_id}-{width}.webp"


def check_source(row: Any, *, source_id: str, sha256: str) -> None:
    """Raise PhotoRefused unless the Booking row may be copied for this entity."""
    if not row["usage_rights"]:
        raise PhotoRefused("usage_rights is off in Booking")
    if row["entity_id"] != source_id:
        raise PhotoRefused("the media belongs to another Booking entity")
    if row["sha256"] != sha256:
        raise PhotoRefused("sha256 changed since the feed was read")
    if not allowed_bucket(row["s3_bucket"]):
        raise PhotoRefused("source bucket is not the Booking supply-media bucket")
    m = SOURCE_KEY_RE.fullmatch(row["s3_key"] or "")
    if not m or m.group(1) != row["entity_type"] or m.group(2) != row["entity_id"] or m.group(3) != row["media_id"]:
        raise PhotoRefused("source key does not match the media row")


def _get(s3, bucket: str, key: str) -> bytes:
    obj = s3.get_object(Bucket=bucket, Key=key)
    if int(obj.get("ContentLength") or 0) > MAX_BYTES:
        raise PhotoRefused(f"object larger than {MAX_BYTES} bytes")
    data = obj["Body"].read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise PhotoRefused(f"object larger than {MAX_BYTES} bytes")
    return data


def _s3():
    import boto3
    from botocore.config import Config

    return boto3.client("s3", config=Config(connect_timeout=3, read_timeout=20,
                                            retries={"max_attempts": 2, "mode": "standard"}))


async def copy_rights_cleared(pool, entity_id: str, source_id: str, *, s3=None) -> dict:
    """Copy every rights-cleared, active photo of the entity whose current bytes are not copied yet.
    Returns {"copied": n, "failed": [{"media_id", "error"}]}; never raises for one photo."""
    pending = await pool.fetch(PENDING_SQL, entity_id)
    if not pending:
        return {"copied": 0, "failed": []}
    sources = {r["media_id"]: r for r in await pool.fetch(SOURCE_SQL, [p["media_id"] for p in pending])}
    s3 = s3 or await asyncio.to_thread(_s3)
    bucket = photo_bucket()
    copied, failed = 0, []
    for p in pending:
        media_id, sha = p["media_id"], p["sha256"]
        try:
            row = sources.get(media_id)
            if row is None:
                raise PhotoRefused("the media is no longer active in Booking")
            check_source(row, source_id=source_id, sha256=sha)
            data = await asyncio.to_thread(_get, s3, row["s3_bucket"], row["s3_key"])
            if hashlib.sha256(data).hexdigest() != sha:
                raise PhotoRefused("bytes do not match the sha256 Booking recorded")
            sizes, _, _ = await asyncio.to_thread(make_sizes, data)
            keys = {w: cis_key(row["entity_type"], media_id, w) for w in SIZES}
            for w, name in SIZES.items():
                await asyncio.to_thread(s3.put_object, Bucket=bucket, Key=keys[w], Body=sizes[name],
                                        ContentType="image/webp", CacheControl="public, max-age=31536000")
            await pool.execute(COPIED_SQL, media_id, keys["1600"], keys["600"], cdn_url(keys["1600"], sha),
                               cdn_url(keys["600"], sha), sha)
            copied += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning("supply_photo_copy_failed", media_id=media_id, error=error)
            failed.append({"media_id": media_id, "error": error})
    return {"copied": copied, "failed": failed}
