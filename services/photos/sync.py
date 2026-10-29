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
from urllib.parse import quote

import structlog

from services.photos.drive import DriveClient, walk
from shared.aws_client.bronze import bronze_bucket
from services.photos.match import AMBIGUOUS, WEAK, match_tour, place_label, resolve_destination

logger = structlog.get_logger()

# AA-995: "medium" (960 w) is the phone hero size (a ~850 CSS px hero takes the 1600 w file otherwise). Files stored
# before it existed get it from services/jobs/photo_medium_backfill_job.py (rendered from the stored original).
SIZES = {"large": 1600, "medium": 960, "small": 600}
# GET /content/photos/{id}?size=: the files tried in order. A 960 w request falls back to the 1600 w file until the
# backfill has rendered it; never to the original (up to 14 MB, not a web image).
SIZE_FALLBACK = {"small": ("small",), "medium": ("medium", "large"), "large": ("large",), "original": ("original",)}
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
    return os.environ.get("PHOTO_BUCKET") or bronze_bucket()


# AA-844: the photos CDN (AA-CIS-Infra accounts/aa365/photos_cdn.tf) serves the bucket's photos/
# prefix at its root (CloudFront origin_path /photos), so photos/<slug>/<file> is <base>/<slug>/<file>.
PHOTO_KEY_PREFIX = "photos/"
_CDN_OFF = ("", "off")


def photo_cdn_base() -> Optional[str]:
    """Base URL of the photos CDN, or None when it is switched off (PHOTO_CDN_BASE=off: the public
    route falls back to a presigned S3 URL). A code default like public_base(), so no deployed
    config is needed."""
    base = os.environ.get("PHOTO_CDN_BASE", "https://img.lumiguides.it.com").strip()
    return None if base.lower() in _CDN_OFF else base.rstrip("/")


def cdn_url(key: Optional[str], sha256: Optional[str] = None) -> Optional[str]:
    """CDN URL of one stored photo key, or None (CDN off, or a key outside photos/ the CDN can not
    reach). ``?v=<sha256[:12]>`` of the source bytes busts the edge cache when a Drive edit
    overwrites the same key (the CDN cache key is path + v)."""
    base = photo_cdn_base()
    if not base or not key or not key.startswith(PHOTO_KEY_PREFIX):
        return None
    url = f"{base}/{quote(key[len(PHOTO_KEY_PREFIX):], safe='/')}"
    return f"{url}?v={sha256[:12]}" if sha256 else url


def country_slug(country: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", country.lower()).strip("-")


def s3_key(country: str, file_id: str, size: str, ext: str = "jpg") -> str:
    slug = country_slug(country)
    if size == "original":
        return f"photos/{slug}/{file_id}-orig.{ext}"
    return f"photos/{slug}/{file_id}-{SIZES[size]}.webp"


def original_ext(file_name: str, mime_type: str) -> str:
    ext = (file_name.rsplit(".", 1)[-1] if "." in file_name else "").lower()
    if ext in ("jpg", "jpeg", "png", "webp", "gif", "heic", "tif", "tiff"):
        return ext
    return {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(mime_type, "jpg")


def make_sizes(data: bytes, sizes: Optional[dict[str, int]] = None) -> tuple[dict[str, bytes], int, int]:
    """WebP renditions (``sizes``: name -> width, default SIZES) of one image, EXIF rotation applied. Returns
    (renditions, w, h) — w/h of the ORIGINAL.

    S207: decoding a full 8256x5504 original (45 MP, ~136 MB RGB, plus copies) inside the 1 GB API
    task OOM-killed it. JPEGs are decoded at a reduced scale (`draft`, ≥ the largest rendition)
    so memory stays ~30 MB per image; the original file itself is stored as raw bytes."""
    from PIL import Image, ImageOps
    widths = sizes or SIZES
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        if im.format == "JPEG":
            # draft() only scales down while BOTH sides stay ≥ the request — ask in the image's ratio.
            target = max(widths.values())
            im.draft("RGB", (target, max(1, target * h // w)) if w >= h else (max(1, target * w // h), target))
        im = ImageOps.exif_transpose(im).convert("RGB")
        if (im.size[0] > im.size[1]) != (w > h):   # EXIF rotated: report the displayed orientation
            w, h = h, w
        out: dict[str, bytes] = {}
        for name, width in widths.items():
            copy = im.copy()
            copy.thumbnail((width, width * 4))
            buf = io.BytesIO()
            copy.save(buf, "WEBP", quality=85, method=4)
            out[name] = buf.getvalue()
            copy.close()
        im.close()
    return out, w, h


async def destination_candidates(conn, tour_ids) -> Optional[dict[str, list[tuple[str, str]]]]:
    """tour id -> [(destination id, name)] of that tour's places (services.photos.destinations, AA-671 S257: the
    geo.tour_place rows of the tour's source key + the current graph build's stops). Fails open — a sync must not stop
    because the place pipeline is unreadable — but loudly, and returns None (not {}): the caller then matches no
    destination this run AND must not re-match (a re-match would clear every automatic destination, review S257).

    AA-860 (X1 S15) replaced the read of the frozen tripplanner.* tables with the CIS place pipeline (schema geo);
    S257 keys the read by the photo's own raw tour (its source key) instead of the live tour of the country, so a
    photo matched to a tour variant still finds the places of its source."""
    from services.photos.destinations import tour_candidates
    try:
        return await tour_candidates(conn, tour_ids)
    except Exception as e:
        logger.error("photo_sync_destination_candidates_unavailable", error=str(e)[:200])
        return None


# One photo row. Destination rules (AA-671 S257; destination_match_source, CIS 303):
#   - a rejected row, or a destination set by hand (source 'manual', the assign route), is never changed (a 'manual'
#     source left on an empty destination — the place was deleted, FK ON DELETE SET NULL — protects nothing);
#   - an automatic destination belongs to the tour it was matched against: when the automatic tour changes in this
#     statement (folder renamed, the tour match changed), the destination is this run's answer for the new tour;
#   - $20 (re-match this row: match_destinations=True AND this run read the tour's places) takes this run's answer;
#   - otherwise an automatic destination is kept, and an empty one is filled.
# The status keeps 'matched' for a row with no tour only while its destination is kept.
# Parameters: $1..$18 the row, $19 keep the tour + status (manual / rejected row), $20 re-match, $21/$22 renditions.
UPSERT_SQL = """
    INSERT INTO shared.place_photo (drive_file_id, drive_root_id, folder_path, country, tour_folder,
        file_name, place_label, tour_id, destination_id, status, s3_key_large, s3_key_small, width,
        height, bytes, sha256, drive_modified_at, error, synced_at, s3_key_original, s3_key_medium,
        destination_match_source)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::uuid, $9::uuid,
            CASE WHEN $18::text IS NOT NULL AND $11::text IS NULL THEN 'error' ELSE $10 END,
            $11, $12, $13, $14, $15, $16, $17, $18, now(), $21, $22,
            CASE WHEN $9::uuid IS NULL THEN NULL ELSE 'auto' END)
    ON CONFLICT (drive_file_id) DO UPDATE SET
        folder_path = EXCLUDED.folder_path, tour_folder = EXCLUDED.tour_folder,
        file_name = EXCLUDED.file_name, place_label = EXCLUDED.place_label,
        tour_id = CASE WHEN $19 THEN place_photo.tour_id ELSE EXCLUDED.tour_id END,
        destination_id = CASE WHEN place_photo.status = 'rejected'
                                   OR (place_photo.destination_match_source = 'manual'
                                       AND place_photo.destination_id IS NOT NULL)
                                   THEN place_photo.destination_id
                              WHEN NOT $19 AND place_photo.tour_id IS DISTINCT FROM EXCLUDED.tour_id
                                   THEN EXCLUDED.destination_id
                              WHEN $20 THEN EXCLUDED.destination_id
                              ELSE coalesce(place_photo.destination_id, EXCLUDED.destination_id) END,
        destination_match_source = CASE WHEN place_photo.status = 'rejected'
                                             OR (place_photo.destination_match_source = 'manual'
                                                 AND place_photo.destination_id IS NOT NULL)
                                             THEN place_photo.destination_match_source
                                        WHEN NOT $19 AND place_photo.tour_id IS DISTINCT FROM EXCLUDED.tour_id
                                             THEN EXCLUDED.destination_match_source
                                        WHEN $20 THEN EXCLUDED.destination_match_source
                                        WHEN place_photo.destination_id IS NOT NULL
                                             THEN place_photo.destination_match_source
                                        ELSE EXCLUDED.destination_match_source END,
        status = CASE WHEN $19 THEN place_photo.status
                      WHEN NOT $20 AND EXCLUDED.status = 'unmatched'
                           AND place_photo.destination_id IS NOT NULL
                           AND place_photo.tour_id IS NOT DISTINCT FROM EXCLUDED.tour_id THEN 'matched'
                      WHEN $18::text IS NOT NULL AND coalesce($11, place_photo.s3_key_large) IS NULL
                           THEN 'error'
                      ELSE EXCLUDED.status END,
        s3_key_large = coalesce($11, place_photo.s3_key_large),
        s3_key_small = coalesce($12, place_photo.s3_key_small),
        s3_key_original = coalesce($21, place_photo.s3_key_original),
        s3_key_medium = coalesce($22, place_photo.s3_key_medium),
        width = coalesce($13, place_photo.width), height = coalesce($14, place_photo.height),
        bytes = coalesce($15, place_photo.bytes), sha256 = coalesce($16, place_photo.sha256),
        drive_modified_at = CASE WHEN $18::text IS NULL THEN EXCLUDED.drive_modified_at
                                 ELSE place_photo.drive_modified_at END,
        error = $18, synced_at = now(), updated_at = now()
"""


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
    existing = {r["drive_file_id"]: r for r in await conn.fetch(
        """SELECT drive_file_id, drive_modified_at, s3_key_small, s3_key_original, match_source, status,
                  tour_id
             FROM shared.place_photo WHERE drive_file_id = ANY($1::text[])""", [f.id for _, f in images])}
    # The tour each photo will carry: the folder's match, or the kept one of a manual / rejected row.
    plan = []
    for path, f in images:
        tour_folder = path[-1] if path else None       # the folder holding the image is the tour (Bhutan has a
        auto_tour = match_tour(tour_folder, tours)     # supplier level above it)
        old = existing.get(f.id)
        keep_match = bool(old and (old["match_source"] == "manual" or old["status"] == "rejected"))
        kept_tour = str(old["tour_id"]) if keep_match and old.get("tour_id") else None
        plan.append((path, f, tour_folder, auto_tour, kept_tour if keep_match else auto_tour, keep_match))
    # AA-671 S257: destinations are matched on every sync, against the places of the photo's OWN tour only (CIS place
    # pipeline, services.photos.destinations — never every place in the country), and only an unambiguous answer
    # counts (match.resolve_destination; a WEAK label-inside-a-name answer is counted, never written). Which existing
    # destination survives is UPSERT_SQL's rule: hand-set never changes, automatic follows its tour, match_destinations
    # re-matches the automatic ones. (S207 had switched matching off because
    # shared.destinations still held the places of the tours published before the reset; the candidates now come
    # from the tour's current geo.tour_place rows.)
    # A re-match (match_destinations=True) takes this run's answer even when it is "none", so it only runs for a tour
    # whose places were read: never after a failed read, never for a tour with no places (yet, e.g. re-extracting).
    candidates_read = await destination_candidates(conn, {p[4] for p in plan if p[4]})
    tour_places = candidates_read or {}
    c = {"country": country, "images": len(images), "downloaded": 0, "unchanged": 0, "matched_tour": 0,
         "matched_destination": 0, "destination_ambiguous": 0, "destination_weak": 0, "unmatched": 0, "errors": 0,
         "tour_folders": len({p[-1] for p, _ in images if p}),
         "destination_candidates_unavailable": candidates_read is None}
    bucket = photo_bucket()
    consecutive_errors = 0

    for n, (path, f, tour_folder, tour_id, photo_tour, keep_match) in enumerate(plan, 1):
        label = place_label(f.name)
        found = resolve_destination(label, tour_places.get(photo_tour, [])) if photo_tour else None
        dest_id = found.destination_id if found else None
        rematch = bool(match_destinations and photo_tour and tour_places.get(photo_tour))
        status = "matched" if (tour_id or dest_id) else "unmatched"
        c["matched_tour"] += bool(tour_id)
        c["matched_destination"] += bool(dest_id)
        c["destination_ambiguous"] += bool(found and found.outcome == AMBIGUOUS)
        c["destination_weak"] += bool(found and found.outcome == WEAK)
        c["unmatched"] += status == "unmatched"
        old = existing.get(f.id)
        modified = _ts(f.modified_time)
        # A row without the original (synced before migration 202) is downloaded once more.
        # A row without the 960 w file (synced before migration 301) is NOT: photo_medium_backfill renders it from the
        # stored original, without a Drive download (AA-995).
        fresh = bool(old and old["s3_key_small"] and old.get("s3_key_original")
                     and old["drive_modified_at"] == modified)
        sizes_meta: dict = {}
        error = None
        if fresh:
            c["unchanged"] += 1
        elif not dry_run:
            try:
                if f.size and f.size > MAX_BYTES:
                    raise ValueError(f"file is {f.size} bytes (max {MAX_BYTES})")
                data = await client.download(f.id, f.thumbnail_link)
                sizes, w, h = await asyncio.to_thread(make_sizes, data)
                keys = {name: s3_key(country, f.id, name) for name in SIZES}
                for name, body in sizes.items():
                    await asyncio.to_thread(s3.put_object, Bucket=bucket, Key=keys[name], Body=body,
                                            ContentType="image/webp", CacheControl="public, max-age=31536000")
                orig_key = s3_key(country, f.id, "original", original_ext(f.name, f.mime_type))
                await asyncio.to_thread(s3.put_object, Bucket=bucket, Key=orig_key, Body=data,
                                        ContentType=f.mime_type or "image/jpeg",
                                        CacheControl="public, max-age=31536000")
                sizes_meta = {"s3_key_original": orig_key, "s3_key_medium": keys["medium"],
                              "s3_key_large": keys["large"], "s3_key_small": keys["small"], "width": w,
                              "height": h, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                del data, sizes
                c["downloaded"] += 1
                consecutive_errors = 0
            except Exception as e:  # one bad file must not stop the folder
                error = re.sub(r"key=[^&\s'\"]+", "key=***", str(e))[:500]
                c["errors"] += 1
                consecutive_errors += 1
                logger.warning("photo_sync_file_failed", file_id=f.id, name=f.name, error=error)
        if dry_run:
            continue
        await conn.execute(
            UPSERT_SQL,
            f.id, root_id, " › ".join(path), country, tour_folder, f.name, label, tour_id, dest_id, status,
            sizes_meta.get("s3_key_large"), sizes_meta.get("s3_key_small"), sizes_meta.get("width"),
            sizes_meta.get("height"), sizes_meta.get("bytes"), sizes_meta.get("sha256"),
            modified, error, keep_match, rematch, sizes_meta.get("s3_key_original"),
            sizes_meta.get("s3_key_medium"))
        if progress and (n % 10 == 0 or n == len(images)):
            await progress(step=country, done=n, total=len(images))
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            raise DriveQuotaStop(f"{consecutive_errors} downloads failed in a row in {country} (last: {error}); "
                                 "stopped — the next sync resumes where this one stopped")
    return c


async def fill_destination_covers(conn) -> int:
    """Set shared.destinations.cover_image_url from matched photos. Only empty covers, or covers this
    job set earlier, are written — an image set by anyone else is left alone.

    AA-995: the cover is the destination's position-1 photo in shared.v_destination_photos, whose ``path`` names the
    canonical row of the same file (mig 290: earliest matched, served row with that sha256). A cover this job set
    earlier on a duplicate copy's id is rewritten to the canonical id, so one picture has one URL everywhere."""
    base = public_base()
    prefix = f"{base}/content/photos/"
    res = await conn.execute(
        """
        UPDATE shared.destinations d SET cover_image_url = $1 || p.path || '?size=large'
          FROM shared.v_destination_photos p
         WHERE p.position = 1
           AND d.id = p.destination_id
           AND (d.cover_image_url IS NULL OR d.cover_image_url LIKE $2 || '%')
           AND d.cover_image_url IS DISTINCT FROM $1 || p.path || '?size=large'
        """, base, prefix)
    return int(res.split()[-1])
