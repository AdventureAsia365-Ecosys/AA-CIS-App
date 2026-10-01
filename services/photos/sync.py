"""AA-708 — sync one CON photo folder (one country) from Google Drive into S3 + shared.place_photo.

Incremental: a file whose Drive modifiedTime is unchanged and already has its S3 sizes is not
downloaded again (its match is still refreshed). A manual assignment or a rejection made on the
admin Photos page is never overwritten.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
from datetime import datetime
from typing import Awaitable, Callable, Optional

import structlog

from services.photos.drive import DriveClient, walk
from services.photos.match import match_destination, match_tour, place_label

logger = structlog.get_logger()

SIZES = {"large": 1600, "small": 600}
MAX_CONSECUTIVE_ERRORS = 10   # Drive quota reached: stop the job, the next sync resumes


class DriveQuotaStop(RuntimeError):
    pass
MAX_BYTES = 40 * 1024 * 1024

# Jira CON board, PHOTOS column (01/10/2026). Laos (CON-31) has no folder link yet.
CON_FOLDERS = [
    {"country": "South Korea", "folder_id": "1eGe9jMRK45UYPDzv7MTyJgT-zgHqJOCK", "source": "CON-18"},
    {"country": "Mongolia", "folder_id": "1iZ7MMyFAOUGF29x2bEYaiL8ECo6u37RK", "source": "CON-19"},
    {"country": "China", "folder_id": "1yuz_t8Hgwk4283jMlhwWP_kSjDTROpX1", "source": "CON-24"},
    {"country": "Taiwan", "folder_id": "1NHsu_IEaqwsY5Vp_PkLx_48HtDqMHsnJ", "source": "CON-28"},
    {"country": "Philippines", "folder_id": "1KWXPa3AJElzkSHa3JbX2W6MNWCPkBovz", "source": "CON-30"},
    {"country": "Bhutan", "folder_id": "1SnPWO2-tDVXmVDwOJM2P0t8xRWw3_mC9", "source": "CON-32"},
    {"country": "Sri Lanka", "folder_id": "1qm8YGNj2Tz2Hddl1LZRcZgR8y9kdOZlI", "source": "CON-36"},
]


def public_base() -> str:
    return os.environ.get("PUBLIC_API_BASE", "https://api-cis.lumiguides.it.com").rstrip("/")


def photo_bucket() -> str:
    return os.environ.get("PHOTO_BUCKET") or os.environ.get("BRONZE_BUCKET", "aa-cis-bronze-005097885195")


def s3_key(country: str, file_id: str, size: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", country.lower()).strip("-")
    return f"photos/{slug}/{file_id}-{SIZES[size]}.webp"


def make_sizes(data: bytes) -> tuple[dict[str, bytes], int, int]:
    """WebP renditions (1600w, 600w) of one image, EXIF rotation applied. Returns (sizes, w, h)."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        out: dict[str, bytes] = {}
        for name, width in SIZES.items():
            copy = im.copy()
            copy.thumbnail((width, width * 4))
            buf = io.BytesIO()
            copy.save(buf, "WEBP", quality=82, method=4)
            out[name] = buf.getvalue()
    return out, w, h


async def itinerary_places(conn, country: str) -> dict[str, list[tuple[str, str]]]:
    """tour_id → [(destination id, name)] of the places on that tour's itinerary, from the
    TripPlanner extraction. Empty when the extraction tables are missing or not yet rebuilt."""
    try:
        rows = await conn.fetch(
            """SELECT DISTINCT x.source_tour_id AS tour_id, d.id, d.name
                 FROM (SELECT source_tour_id, destination_id FROM tripplanner.itinerary_components
                       UNION SELECT source_tour_id, destination_id FROM tripplanner.tour_stop) x
                 JOIN shared.destinations d ON d.id = x.destination_id
                WHERE d.country = $1""", country)
    except Exception as e:  # TripPlanner schema absent (local/test DB)
        logger.warning("photo_sync_itinerary_places_unavailable", error=str(e)[:200])
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        out.setdefault(str(r["tour_id"]), []).append((str(r["id"]), r["name"]))
    return out


def _ts(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


async def sync_folder(conn, client: DriveClient, s3, *, country: str, root_id: str,
                      limit: Optional[int] = None, dry_run: bool = False,
                      match_destinations: bool = False,
                      progress: Optional[Callable[..., Awaitable[None]]] = None) -> dict:
    images = await walk(client, root_id)
    if limit:
        images = images[:limit]
    # Folder names are often the rewritten (aa_name) title, so the latest written name counts too.
    tours = [(str(r["tour_id"]), r["src_name"], r["aa_name"]) for r in await conn.fetch(
        """SELECT rt.tour_id, rt.src_name,
                  (SELECT gc.aa_name FROM silver_aa_internal.generated_content gc
                    WHERE gc.tour_id = rt.tour_id ORDER BY gc.created_at DESC LIMIT 1) AS aa_name
             FROM silver_aa_internal.raw_tours rt
            WHERE rt.country = $1 AND rt.source_status = 'active' AND rt.deleted_at IS NULL""", country)]
    # Destinations are matched only on request: on 01/10/2026 shared.destinations still held the
    # places extracted from the tours published BEFORE the S207 reset. Match again (no re-download)
    # once the rerun tours are rewritten, atomized and re-extracted (Nghiệp, S207).
    # A photo is compared only with the places on ITS tour's itinerary (TripPlanner extraction:
    # tripplanner.itinerary_components / tour_stop) — never with every place in the country.
    tour_places = await itinerary_places(conn, country) if match_destinations else {}
    existing = {r["drive_file_id"]: r for r in await conn.fetch(
        """SELECT drive_file_id, drive_modified_at, s3_key_small, match_source, status
             FROM shared.place_photo WHERE drive_file_id = ANY($1::text[])""", [f.id for _, f in images])}
    c = {"country": country, "images": len(images), "downloaded": 0, "unchanged": 0, "matched_tour": 0,
         "matched_destination": 0, "unmatched": 0, "errors": 0, "tour_folders": len({p[-1] for p, _ in images if p})}
    bucket = photo_bucket()
    consecutive_errors = 0

    for n, (path, f) in enumerate(images, 1):
        # The folder holding the image is the tour (Bhutan has a supplier level above it).
        tour_folder = path[-1] if path else None
        label = place_label(f.name)
        tour_id = match_tour(tour_folder, tours)
        dest_id = match_destination(label, tour_places.get(tour_id, [])) if tour_id else None
        status = "matched" if (tour_id or dest_id) else "unmatched"
        c["matched_tour"] += bool(tour_id)
        c["matched_destination"] += bool(dest_id)
        c["unmatched"] += status == "unmatched"
        old = existing.get(f.id)
        modified = _ts(f.modified_time)
        fresh = bool(old and old["s3_key_small"] and old["drive_modified_at"] == modified)
        sizes_meta: dict = {}
        error = None
        if fresh:
            c["unchanged"] += 1
        elif not dry_run:
            try:
                if f.size and f.size > MAX_BYTES:
                    raise ValueError(f"file is {f.size} bytes (max {MAX_BYTES})")
                data = await client.download(f.id)
                sizes, w, h = await asyncio.to_thread(make_sizes, data)
                keys = {name: s3_key(country, f.id, name) for name in SIZES}
                for name, body in sizes.items():
                    await asyncio.to_thread(s3.put_object, Bucket=bucket, Key=keys[name], Body=body,
                                            ContentType="image/webp", CacheControl="public, max-age=31536000")
                sizes_meta = {"s3_key_large": keys["large"], "s3_key_small": keys["small"], "width": w,
                              "height": h, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                c["downloaded"] += 1
                consecutive_errors = 0
            except Exception as e:  # one bad file must not stop the folder
                error = re.sub(r"key=[^&\s'\"]+", "key=***", str(e))[:500]
                c["errors"] += 1
                consecutive_errors += 1
                logger.warning("photo_sync_file_failed", file_id=f.id, name=f.name, error=error)
        if dry_run:
            continue
        keep_match = bool(old and (old["match_source"] == "manual" or old["status"] == "rejected"))
        await conn.execute(
            """
            INSERT INTO shared.place_photo (drive_file_id, drive_root_id, folder_path, country, tour_folder,
                file_name, place_label, tour_id, destination_id, status, s3_key_large, s3_key_small, width,
                height, bytes, sha256, drive_modified_at, error, synced_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::uuid, $9::uuid,
                    CASE WHEN $18::text IS NOT NULL AND $11::text IS NULL THEN 'error' ELSE $10 END,
                    $11, $12, $13, $14, $15, $16, $17, $18, now())
            ON CONFLICT (drive_file_id) DO UPDATE SET
                folder_path = EXCLUDED.folder_path, tour_folder = EXCLUDED.tour_folder,
                file_name = EXCLUDED.file_name, place_label = EXCLUDED.place_label,
                tour_id = CASE WHEN $19 THEN place_photo.tour_id ELSE EXCLUDED.tour_id END,
                destination_id = CASE WHEN $19 OR NOT $20 THEN place_photo.destination_id
                                      ELSE EXCLUDED.destination_id END,
                status = CASE WHEN $19 THEN place_photo.status
                              WHEN NOT $20 AND EXCLUDED.status = 'unmatched'
                                   AND place_photo.destination_id IS NOT NULL THEN 'matched'
                              WHEN $18::text IS NOT NULL AND coalesce($11, place_photo.s3_key_large) IS NULL
                                   THEN 'error'
                              ELSE EXCLUDED.status END,
                s3_key_large = coalesce($11, place_photo.s3_key_large),
                s3_key_small = coalesce($12, place_photo.s3_key_small),
                width = coalesce($13, place_photo.width), height = coalesce($14, place_photo.height),
                bytes = coalesce($15, place_photo.bytes), sha256 = coalesce($16, place_photo.sha256),
                drive_modified_at = CASE WHEN $18::text IS NULL THEN EXCLUDED.drive_modified_at
                                         ELSE place_photo.drive_modified_at END,
                error = $18, synced_at = now(), updated_at = now()
            """,
            f.id, root_id, " › ".join(path), country, tour_folder, f.name, label, tour_id, dest_id, status,
            sizes_meta.get("s3_key_large"), sizes_meta.get("s3_key_small"), sizes_meta.get("width"),
            sizes_meta.get("height"), sizes_meta.get("bytes"), sizes_meta.get("sha256"),
            modified, error, keep_match, match_destinations)
        if progress and (n % 10 == 0 or n == len(images)):
            await progress(step=country, done=n, total=len(images))
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            raise DriveQuotaStop(f"{consecutive_errors} downloads failed in a row in {country} (last: {error}); "
                                 "stopped — the next sync resumes where this one stopped")
    return c


async def fill_destination_covers(conn) -> int:
    """Set shared.destinations.cover_image_url from matched photos. Only empty covers, or covers this
    job set earlier, are written — an image set by anyone else is left alone."""
    prefix = f"{public_base()}/content/photos/"
    res = await conn.execute(
        """
        UPDATE shared.destinations d SET cover_image_url = $1 || p.id::text || '?size=large'
          FROM (SELECT DISTINCT ON (destination_id) id, destination_id FROM shared.place_photo
                 WHERE status = 'matched' AND destination_id IS NOT NULL AND s3_key_large IS NOT NULL
                 ORDER BY destination_id, (match_source = 'manual') DESC, file_name) p
         WHERE d.id = p.destination_id
           AND (d.cover_image_url IS NULL OR d.cover_image_url LIKE $1 || '%')
           AND d.cover_image_url IS DISTINCT FROM $1 || p.id::text || '?size=large'
        """, prefix)
    return int(res.split()[-1])
