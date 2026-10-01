"""AA-708 — Photos: AA marketing photos synced from the CON board's Google Drive folders.

Admin (`/admin/photos`, behind the admin BFF):
  - GET  /summary                 — per country: photos, matched to a tour / destination, unmatched,
                                     rejected, errors; destinations with a cover; the last sync job.
  - GET  ""                       — photos, filter by country / status / Drive folder.
  - GET  /folders?country=        — Drive folders with photo / unmatched counts and matched tour.
  - GET  /destinations?country=   — destination names for the manual assign picker.
  - GET  /tours?country=          — active tours for the manual assign picker.
  - POST /sync                    — enqueue a `photo_sync` job (admin secret).
  - POST /{photo_id}/assign       — set tour / destination by hand, or reject (admin secret).

Public (`/content/photos/{photo_id}?size=large|small`): 302 to a short-lived S3 URL. It is the
`cover_image_url` the TripPlanner shows (API Gateway `/content/*` has no auth). CloudFront can
replace it later without changing the stored URLs' meaning.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import boto3
import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from api.routers.admin import verify_admin_secret

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/photos", tags=["admin-photos"])
public_router = APIRouter(prefix="/content/photos", tags=["photos"])

STATUSES = ("matched", "unmatched", "rejected", "error")
_s3 = None


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _uuid(value: Optional[str], field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} is not a UUID")


def _photo(row) -> dict:
    d = dict(row)
    if d.get("error"):   # rows written before the key moved to a header carried `?key=...`
        import re
        d["error"] = re.sub(r"key=[^&\s'\"]+", "key=***", d["error"])
    for k in ("id", "tour_id", "destination_id"):
        d[k] = str(d[k]) if d.get(k) else None
    for k in ("synced_at", "drive_modified_at", "updated_at"):
        if d.get(k):
            d[k] = d[k].isoformat()
    from services.photos.sync import public_base
    base = f"{public_base()}/content/photos/{d['id']}"
    d["url_small"] = f"{base}?size=small" if d.get("has_file") else None
    d["url_large"] = f"{base}?size=large" if d.get("has_file") else None
    return d


@router.get("/summary", summary="AA-708 — photo coverage per country + last sync")
async def summary(request: Request):
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT country, count(*) AS photos,
                   count(*) FILTER (WHERE tour_id IS NOT NULL AND status = 'matched') AS with_tour,
                   count(*) FILTER (WHERE destination_id IS NOT NULL AND status = 'matched') AS with_destination,
                   count(*) FILTER (WHERE status = 'unmatched') AS unmatched,
                   count(*) FILTER (WHERE status = 'rejected') AS rejected,
                   count(*) FILTER (WHERE status = 'error') AS errors,
                   count(DISTINCT tour_folder) AS tour_folders,
                   count(DISTINCT tour_id) FILTER (WHERE status = 'matched') AS tours_covered,
                   count(*) FILTER (WHERE s3_key_small IS NOT NULL) AS with_file,
                   coalesce(sum(bytes) FILTER (WHERE s3_key_small IS NOT NULL), 0) AS source_bytes,
                   count(*) FILTER (WHERE match_source = 'manual') AS manual,
                   max(synced_at) AS last_synced
              FROM shared.place_photo GROUP BY country ORDER BY country""")
        covers = {r["country"]: (r["n"], r["with_cover"]) for r in await conn.fetch(
            """SELECT country, count(*) AS n, count(cover_image_url) AS with_cover
                 FROM shared.destinations GROUP BY country""")}
        tours = {r["country"]: r["n"] for r in await conn.fetch(
            """SELECT country, count(*) AS n FROM silver_aa_internal.raw_tours
                WHERE source_status = 'active' AND deleted_at IS NULL GROUP BY country""")}
        job = await conn.fetchrow(
            """SELECT id, status, created_at, finished_at, result, error FROM shared.job
                WHERE kind = 'photo_sync' ORDER BY created_at DESC LIMIT 1""")
    # S207 redesign: every country with active tours is listed, with or without photos yet.
    by_country = {r["country"]: dict(r) for r in rows}
    empty = {"photos": 0, "with_tour": 0, "with_destination": 0, "unmatched": 0, "rejected": 0, "errors": 0,
             "tour_folders": 0, "tours_covered": 0, "with_file": 0, "source_bytes": 0, "manual": 0,
             "last_synced": None}
    from services.photos.sync import CON_FOLDERS
    countries = []
    for c in sorted(set(by_country) | set(tours)):
        d = by_country.get(c) or {"country": c, **empty}
        d["last_synced"] = d["last_synced"].isoformat() if d["last_synced"] else None
        d["source_bytes"] = int(d["source_bytes"] or 0)
        d["destinations"], d["destinations_with_cover"] = covers.get(c, (0, 0))
        d["active_tours"] = tours.get(c, 0)
        d["has_drive_folder"] = any(f["country"] == c for f in CON_FOLDERS)
        countries.append(d)
    keys = ("photos", "with_file", "with_tour", "unmatched", "rejected", "errors", "tours_covered",
            "active_tours", "source_bytes", "destinations", "destinations_with_cover", "manual", "tour_folders")
    totals = {k: sum(int(d[k] or 0) for d in countries) for k in keys}
    last_job = None
    if job:
        last_job = {"id": str(job["id"]), "status": job["status"], "error": job["error"],
                    "created_at": job["created_at"].isoformat() if job["created_at"] else None,
                    "finished_at": job["finished_at"].isoformat() if job["finished_at"] else None,
                    "result": job["result"]}
    return {"countries": countries, "totals": totals, "last_job": last_job}


@router.get("", summary="AA-708 — list synced photos")
async def list_photos(request: Request, country: Optional[str] = None, status: Optional[str] = None,
                      folder_path: Optional[str] = None, limit: int = Query(120, ge=1, le=500),
                      offset: int = Query(0, ge=0)):
    if status and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {STATUSES}")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.country, p.folder_path, p.tour_folder, p.file_name, p.place_label, p.tour_id,
                   rt.src_name AS tour_name, p.destination_id, d.name AS destination_name, p.match_source,
                   p.status, p.width, p.height, p.bytes, p.error, p.synced_at, p.drive_file_id,
                   (p.s3_key_small IS NOT NULL) AS has_file, count(*) OVER () AS total
              FROM shared.place_photo p
              LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = p.tour_id
              LEFT JOIN shared.destinations d ON d.id = p.destination_id
             WHERE ($1::text IS NULL OR p.country = $1) AND ($2::text IS NULL OR p.status = $2)
               AND ($3::text IS NULL OR p.folder_path = $3)
             ORDER BY p.country, p.tour_folder NULLS FIRST, p.file_name
             LIMIT $4 OFFSET $5""", country, status, folder_path, limit, offset)
    total = rows[0]["total"] if rows else 0
    return {"total": total, "photos": [_photo(r) for r in rows]}


def _url(photo_id, size: str = "small") -> str:
    from services.photos.sync import public_base
    return f"{public_base()}/content/photos/{photo_id}?size={size}"


@router.get("/folders", summary="AA-708 — Drive folders with their match and sample photos")
async def folders(request: Request, country: Optional[str] = None):
    rows = await request.app.state.pool.fetch(
        """SELECT p.country, p.folder_path, max(p.tour_folder) AS tour_folder, count(*) AS photos,
                  count(*) FILTER (WHERE p.status = 'unmatched') AS unmatched,
                  count(*) FILTER (WHERE p.status = 'error') AS errors,
                  count(*) FILTER (WHERE p.status = 'matched') AS matched,
                  min(rt.src_name) FILTER (WHERE p.tour_id IS NOT NULL) AS tour_name,
                  min(p.tour_id::text) FILTER (WHERE p.tour_id IS NOT NULL) AS tour_id,
                  count(DISTINCT p.tour_id) AS tours,
                  bool_or(p.match_source = 'manual') AS manual,
                  (array_agg(p.id::text ORDER BY p.file_name) FILTER (WHERE p.s3_key_small IS NOT NULL))[1:4] AS sample
             FROM shared.place_photo p
             LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = p.tour_id
            WHERE ($1::text IS NULL OR p.country = $1)
            GROUP BY p.country, p.folder_path ORDER BY p.country, unmatched DESC, p.folder_path""", country)
    out = []
    for r in rows:
        d = dict(r)
        d["sample"] = [_url(i) for i in (d["sample"] or [])]
        out.append(d)
    return {"folders": out}


@router.get("/tour-coverage", summary="AA-708 — photos per active tour (gaps first)")
async def tour_coverage(request: Request, country: Optional[str] = None, only_missing: bool = False):
    rows = await request.app.state.pool.fetch(
        """SELECT rt.tour_id::text AS tour_id, rt.src_name, rt.country, rt.duration,
                  EXISTS (SELECT 1 FROM gold_aa_internal.published_tours pt WHERE pt.tour_id = rt.tour_id) AS published,
                  count(p.id) AS photos,
                  (array_agg(p.id::text ORDER BY (p.match_source = 'manual') DESC, p.file_name)
                     FILTER (WHERE p.id IS NOT NULL))[1:4] AS sample
             FROM silver_aa_internal.raw_tours rt
             LEFT JOIN shared.place_photo p
                    ON p.tour_id = rt.tour_id AND p.status = 'matched' AND p.s3_key_small IS NOT NULL
            WHERE rt.source_status = 'active' AND rt.deleted_at IS NULL
              AND ($1::text IS NULL OR rt.country = $1)
            GROUP BY rt.tour_id, rt.src_name, rt.country, rt.duration
           HAVING NOT $2 OR count(p.id) = 0
            ORDER BY rt.country, count(p.id), rt.src_name""", country, only_missing)
    out = []
    for r in rows:
        d = dict(r)
        d["sample"] = [_url(i) for i in (d["sample"] or [])]
        out.append(d)
    return {"tours": out, "with_photos": sum(1 for d in out if d["photos"]), "total": len(out)}


@router.get("/destinations", summary="AA-708 — destinations of a country (assign picker)")
async def destinations(request: Request, country: str):
    pool = request.app.state.pool
    rows = await pool.fetch("SELECT id, name FROM shared.destinations WHERE country = $1 ORDER BY name", country)
    return {"destinations": [{"id": str(r["id"]), "name": r["name"]} for r in rows]}


@router.get("/tours", summary="AA-708 — active tours of a country (assign picker)")
async def tours(request: Request, country: str):
    pool = request.app.state.pool
    rows = await pool.fetch(
        """SELECT tour_id, src_name FROM silver_aa_internal.raw_tours
            WHERE country = $1 AND source_status = 'active' AND deleted_at IS NULL ORDER BY src_name""",
        country)
    return {"tours": [{"id": str(r["tour_id"]), "name": r["src_name"]} for r in rows]}


class SyncRequest(BaseModel):
    countries: Optional[list[str]] = None   # subset of the CON folders; None = all
    limit: Optional[int] = None             # images per folder (trial runs)
    dry_run: bool = False
    match_destinations: bool = False        # off until destinations are re-extracted after the rerun


@router.post("/sync", summary="AA-708 — enqueue a photo_sync job")
async def sync(body: SyncRequest, request: Request, x_admin_secret: str = Header(None),
               x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.photos.sync import CON_FOLDERS
    from shared.jobs.registry import enqueue
    folders = [f for f in CON_FOLDERS if not body.countries or f["country"] in body.countries]
    if not folders:
        raise HTTPException(status_code=422, detail="no CON photo folder for those countries")
    job_id, created = await enqueue(
        request.app.state.pool, "photo_sync",
        {"folders": folders, "limit": body.limit, "dry_run": body.dry_run,
         "match_destinations": body.match_destinations},
        created_by=f"admin:{x_admin_user_id or 'unknown'}")
    return {"job_id": job_id, "created": created, "folders": [f["country"] for f in folders]}


class AssignRequest(BaseModel):
    tour_id: Optional[str] = None
    destination_id: Optional[str] = None
    reject: bool = False
    whole_folder: bool = True   # a tour assignment applies to every photo in the same Drive folder


@router.post("/{photo_id}/assign", summary="AA-708 — manual tour/destination assignment or reject")
async def assign(photo_id: str, body: AssignRequest, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pid = _uuid(photo_id, "photo_id")
    tour_id = _uuid(body.tour_id, "tour_id")
    dest_id = _uuid(body.destination_id, "destination_id")
    if not body.reject and not (tour_id or dest_id):
        raise HTTPException(status_code=422, detail="give tour_id and/or destination_id, or reject")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        photo = await conn.fetchrow(
            "SELECT country, folder_path FROM shared.place_photo WHERE id = $1::uuid", pid)
        if not photo:
            raise HTTPException(status_code=404, detail="photo not found")
        if tour_id and not await conn.fetchval(
                "SELECT 1 FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid AND country = $2",
                tour_id, photo["country"]):
            raise HTTPException(status_code=422, detail="tour not found in this photo's country")
        if dest_id and not await conn.fetchval(
                "SELECT 1 FROM shared.destinations WHERE id = $1::uuid AND country = $2", dest_id, photo["country"]):
            raise HTTPException(status_code=422, detail="destination not found in this photo's country")
        status = "rejected" if body.reject else "matched"
        await conn.execute(
            """UPDATE shared.place_photo SET tour_id = $2::uuid, destination_id = $3::uuid, status = $4,
                      match_source = 'manual', updated_at = now() WHERE id = $1::uuid""",
            pid, None if body.reject else tour_id, None if body.reject else dest_id, status)
        folder_count = 1
        if tour_id and not body.reject and body.whole_folder:
            # Same tour for the rest of the folder; their own destination and rejections are kept.
            res = await conn.execute(
                """UPDATE shared.place_photo SET tour_id = $3::uuid, status = 'matched', match_source = 'manual',
                          updated_at = now()
                    WHERE country = $1 AND folder_path = $2 AND status <> 'rejected' AND id <> $4::uuid""",
                photo["country"], photo["folder_path"], tour_id, pid)
            folder_count += int(res.split()[-1])
        covers = 0
        if not body.reject and dest_id:
            from services.photos.sync import fill_destination_covers
            covers = await fill_destination_covers(conn)
    return {"id": pid, "status": status, "tour_id": tour_id, "destination_id": dest_id,
            "photos_updated": folder_count if not body.reject else 1, "covers_set": covers}


@public_router.get("/{photo_id}", summary="AA-708 — photo image (redirect to S3)", include_in_schema=False)
async def photo_image(photo_id: str, request: Request,
                      size: str = Query("large", pattern="^(large|small|original)$")):
    pid = _uuid(photo_id, "photo_id")
    row = await request.app.state.pool.fetchrow(
        """SELECT s3_key_large, s3_key_small, s3_key_original FROM shared.place_photo
            WHERE id = $1::uuid AND status <> 'rejected'""", pid)
    key = row and row[f"s3_key_{size}"]
    if not key:
        raise HTTPException(status_code=404, detail="photo not found")
    from services.photos.sync import photo_bucket
    url = await asyncio.to_thread(_client().generate_presigned_url, "get_object",
                                  Params={"Bucket": photo_bucket(), "Key": key}, ExpiresIn=3600)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "public, max-age=3000"})
