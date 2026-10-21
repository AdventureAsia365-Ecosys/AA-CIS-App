"""AA-712 — place pipeline admin API (behind the admin BFF, admin secret).

  POST /admin/places/backfill  {country?, limit ≤ 50, only_stale, dry_run, force}
       dry_run (default true): the tours it would take + expected calls and cost, no call made.
       real run: one place_extract job per tour (background kind, cap 1). 409 while a wave runs
       (s1_rewrite / a3_atomize / recompute queued or running) or a previous backfill batch still
       has queued jobs; 409 when the expected geocoder cost exceeds the per-backfill cap ($50).

AA-847 (content-validity gate, ADR 0007 decision 10, design §5):
  GET  /admin/places/validity?country=&check=&reason=&page=&page_size=
       the deterministic report (services/places/validity.py): counts per check and reason, catalog-wide and per
       country, and one page of findings; the last builds' counts (geo.graph_build.stats.validity) as a trend.
  GET  /admin/places/coverage
       per country: live tours, built (geo.tour_build ok / partial), in the current graph, with a stop on every
       extracted day — the gate for removing the TripPlanner places fallback.
  POST /admin/places/validity/fix {check, country?, source_id?, dry_run = true, undo?, fingerprint?}
       services/places/validity_fix.py; apply writes an audit row with the prior values, undo restores them.

AA-847 lines (owner decision S246, D1: rivers, roads and trails are drawn as lines):
  GET    /admin/places/lines?country=&status=     line places and their geometry state ("Lines" tab); approximate
                                                  routes counted per country apart
  POST   /admin/places/shapes/backfill            {country?, statuses?, destination_ids?, limit ≤ 50, dry_run = true}
                                                  dry run: names + Geoapify credit estimate; real: rows due now + one
                                                  place_shape job. 409 when PLACE_SHAPES_ENABLED is off or the credits
                                                  would pass today's limit
  PUT    /admin/places/{destination_id}/shape     {geojson, note?} manual geometry (exception path), clipped to the
                                                  country; DELETE drops it and hands the row back to the job

Every response that shows coordinates carries the Geoapify / OpenStreetMap attribution (ADR 0007).
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from api.core.staff_authz import staff_policy
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret

ATTRIBUTION = {"geoapify": "Powered by Geoapify", "geoapify_url": "https://www.geoapify.com/",
               "osm": "© OpenStreetMap contributors", "osm_url": "https://www.openstreetmap.org/copyright"}
VALIDITY_CACHE_S = 60.0
_validity_cache: dict = {}

logger = structlog.get_logger()

# AA-882 (GĐ3): staff_policy narrows every route listed in api/core/staff_authz.py POLICY by role.
router = APIRouter(prefix="/admin/places", tags=["admin-places"], dependencies=[Depends(staff_policy)])


class BackfillRequest(BaseModel):
    country: Optional[str] = None
    limit: int = Field(20, ge=1, le=50)
    only_stale: bool = True
    dry_run: bool = True
    force: bool = False


@router.post("/backfill", summary="AA-712 — backfill place_extract for ≤ 50 live tours (dry run by default)")
async def backfill(body: BackfillRequest, request: Request, x_admin_secret: str = Header(None),
                   x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import backfill as bf
    from services.places.enqueue import enqueue_place_extract
    from services.places.geocoder import get_geocoder

    pool = request.app.state.pool
    geocoder = get_geocoder()
    async with pool.acquire() as conn:
        busy = await bf.wave_busy(conn)
        tours = await bf.plan(conn, country=body.country, limit=body.limit, only_stale=body.only_stale)
        est = await bf.estimate(conn, tours, geocoder.cost_per_request, geocoder.calls_external)
    out = {"dry_run": body.dry_run, "country": body.country, "only_stale": body.only_stale,
           "geocoder": geocoder.name, "busy": busy, "estimate": est,
           "tours": [{"tour_id": t["tour_id"], "source_id": t["source_id"], "country": t.get("country"),
                      "name": t.get("aa_name"), "built": t.get("build") is not None} for t in tours]}
    if body.dry_run:
        out["would_refuse"] = bool(busy) or est["geocoder_cost_usd"] > bf.GEOCODER_BACKFILL_CAP_USD
        return out
    if busy:
        raise HTTPException(status_code=409, detail={"error": "a wave or a previous backfill batch is running",
                                                     "busy": busy})
    if est["geocoder_cost_usd"] > bf.GEOCODER_BACKFILL_CAP_USD:
        raise HTTPException(status_code=409, detail={"error": "expected geocoder cost above the backfill cap",
                                                     "cap_usd": bf.GEOCODER_BACKFILL_CAP_USD, "estimate": est})
    if not tours:
        return {**out, "enqueued": 0, "jobs": []}
    batch = uuid.uuid4().hex[:8]
    created_by = f"backfill:admin:{x_admin_user_id or 'unknown'}"
    jobs = []
    for t in tours:
        job_id, created = await enqueue_place_extract(
            pool, tour_id=t["tour_id"], version_id=t.get("version_id"), reason=f"backfill:{batch}",
            created_by=created_by, idempotency_key=f"place_extract:backfill:{batch}:{t['tour_id']}",
            force=body.force)
        jobs.append({"tour_id": t["tour_id"], "job_id": job_id, "created": created})
    logger.info("places_backfill_enqueued", batch=batch, tours=len(jobs), country=body.country,
                expected_cost_usd=est["expected_cost_usd"])
    return {**out, "batch": batch, "enqueued": sum(1 for j in jobs if j["created"]), "jobs": jobs}


# ── AA-847: content-validity report, coverage, fixes ────────────────────────────────────────────────────────────────

TREND_SQL = """
    SELECT build_id::text AS build_id, built_at, stats -> 'validity' AS validity, is_current
      FROM geo.graph_build ORDER BY built_at DESC LIMIT 5
"""

COVERAGE_SQL = """
    WITH live AS (
        SELECT DISTINCT COALESCE(rt.source_group_id, rt.tour_id)::text AS source_id, rt.country
          FROM gold_aa_internal.published_tours pt
          JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
         WHERE pt.tenant_id = $1::uuid
           AND pt.master_status = 'active' AND pt.deleted_at IS NULL
           AND rt.source_status = 'active' AND rt.deleted_at IS NULL
    ), days AS (
        SELECT source_id, count(*) AS n FROM geo.tour_day GROUP BY source_id
    ), stops AS (
        SELECT s.source_id, count(DISTINCT s.day_index) AS n
          FROM geo.tour_stop s JOIN geo.graph_build b ON b.build_id = s.build_id AND b.is_current
         GROUP BY s.source_id
    )
    SELECT coalesce(l.country, '-') AS country,
           count(*) AS live,
           count(*) FILTER (WHERE tb.status IN ('ok', 'partial')) AS built,
           count(*) FILTER (WHERE tb.status = 'partial') AS partial,
           count(*) FILTER (WHERE st.n > 0) AS in_graph,
           count(*) FILTER (WHERE st.n > 0 AND st.n >= d.n) AS stop_every_day,
           coalesce(round(sum(tb.cost_usd), 4), 0) AS build_cost_usd
      FROM live l
      LEFT JOIN geo.tour_build tb ON tb.source_id = l.source_id
      LEFT JOIN days d ON d.source_id = l.source_id
      LEFT JOIN stops st ON st.source_id = l.source_id
     GROUP BY 1 ORDER BY 1
"""


async def _validity(pool, country: Optional[str]):
    import time

    from services.places import validity
    hit = _validity_cache.get(country)
    if hit and time.monotonic() - hit[0] < VALIDITY_CACHE_S:
        return hit[1], hit[2]
    async with pool.acquire() as conn:
        summary, items = await validity.report(conn, country)
    _validity_cache[country] = (time.monotonic(), summary, items)
    return summary, items


@router.get("/validity", summary="AA-847 — content-validity report of the live catalog's places (no model call)")
async def validity_report(request: Request, country: Optional[str] = None, check: Optional[str] = None,
                          reason: Optional[str] = None, page: int = 1, page_size: int = 50,
                          x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import validity
    if check is not None and check not in validity.CHECKS:
        raise HTTPException(status_code=422, detail=f"check must be one of {validity.CHECKS}")
    if reason is not None and not any(reason in r for r in validity.REASONS.values()):
        raise HTTPException(status_code=422, detail="unknown reason")
    page_size = max(1, min(page_size, 200))
    summary, items = await _validity(request.app.state.pool, country or None)
    async with request.app.state.pool.acquire() as conn:
        trend = [{"build_id": r["build_id"], "built_at": r["built_at"].isoformat() if r["built_at"] else None,
                  "is_current": r["is_current"],
                  "validity": (json.loads(r["validity"]) if isinstance(r["validity"], str) else r["validity"])}
                 for r in await conn.fetch(TREND_SQL)]
    return {"country": country, "checks": list(validity.CHECKS), "reasons": validity.REASONS,
            "summary": summary, "trend": trend, "attribution": ATTRIBUTION,
            **validity.page(items, check=check, reason=reason, page_no=page, page_size=page_size)}


@router.get("/coverage", summary="AA-847 — place pipeline coverage per country (live / built / graph / stop per day)")
async def coverage(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places.validity import MASTER_TENANT_ID
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER_TENANT_ID)
            rows = [dict(r) for r in await conn.fetch(COVERAGE_SQL, MASTER_TENANT_ID)]
    for r in rows:
        r["build_cost_usd"] = float(r["build_cost_usd"] or 0)
    totals = {k: sum(r[k] for r in rows) for k in ("live", "built", "partial", "in_graph", "stop_every_day")}
    totals["build_cost_usd"] = round(sum(r["build_cost_usd"] for r in rows), 4)
    return {"countries": rows, "totals": totals}


class ValidityFixRequest(BaseModel):
    check: Optional[str] = None
    country: Optional[str] = None
    source_id: Optional[str] = None
    dry_run: bool = True
    undo: Optional[str] = None
    fingerprint: Optional[str] = None     # the dry run's; apply refuses (409) when the plan changed since


@router.post("/validity/fix", summary="AA-847 — fix content-validity findings (dry run by default; audit + undo)")
async def validity_fix(body: ValidityFixRequest, request: Request, x_admin_secret: str = Header(None),
                       x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import validity_fix as vf
    actor = f"admin:{x_admin_user_id or 'unknown'}"
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if body.undo:
                    try:
                        uuid.UUID(body.undo)
                    except ValueError:
                        raise HTTPException(status_code=422, detail="undo must be an audit row id (uuid)")
                    out = await vf.undo(conn, body.undo, actor=actor, dry_run=body.dry_run)
                else:
                    if body.check not in vf.CHECKS:
                        raise HTTPException(status_code=422, detail=f"check must be one of {vf.CHECKS}")
                    fix = await vf.plan(conn, body.check, country=body.country or None,
                                        source_id=body.source_id or None)
                    if not body.dry_run:
                        vf.check_fingerprint(fix, body.fingerprint)
                    out = {**fix, "dry_run": True} if body.dry_run else {**await vf.apply(conn, fix, actor=actor),
                                                                         "dry_run": False}
    except vf.FixError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    if not body.dry_run:
        _validity_cache.clear()
        logger.info("places_validity_fix", check=out.get("check"), applied=out.get("applied", out.get("restored")),
                    undo=body.undo, actor=actor)
    return out


# ── AA-847 (owner decision S246, D1): line places drawn as lines — "Lines" tab, shapes backfill, manual GeoJSON ──────

LINE_STATUSES = ("missing", "pending", "ok", "approximate", "not_found", "failed", "manual")


def _line_row(r: dict) -> dict:
    out = dict(r)
    for k in ("next_retry_at", "fetched_at"):
        out[k] = r[k].isoformat() if r.get(k) else None
    out["length_km"] = float(r["length_km"]) if r.get("length_km") is not None else None
    out["status"] = r.get("status") or "missing"
    out.pop("no_shape_row", None)
    return out


@router.get("/lines", summary="AA-847 — line places (rivers, roads, trails) and the state of their geometry")
async def lines(request: Request, country: Optional[str] = None, status: Optional[str] = None,
                x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    if status is not None and status not in LINE_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {LINE_STATUSES}")
    from services.jobs.place_shape_job import ENABLED_ENV, enabled
    from services.places import store
    async with request.app.state.pool.acquire() as conn:
        rows = [_line_row(r) for r in await store.lines_report(conn, country or None,
                                                               None if status in (None, "missing") else status)]
    if status == "missing":
        rows = [r for r in rows if r["status"] == "missing"]
    by_country: dict = {}
    for r in rows:
        c = by_country.setdefault(r["country"], {s: 0 for s in LINE_STATUSES})
        c[r["status"]] += 1
    drawn = sum(1 for r in rows if r["status"] in ("ok", "manual", "approximate"))
    return {"country": country, "status": status, "enabled": enabled(), "enabled_env": ENABLED_ENV,
            "totals": {s: sum(c[s] for c in by_country.values()) for s in LINE_STATUSES},
            "drawn": drawn, "lines": len(rows),
            # approximate routes are reported apart (owner question S246: do they satisfy D1?)
            "approximate_by_country": {k: v["approximate"] for k, v in by_country.items() if v["approximate"]},
            "by_country": by_country, "items": rows[:500], "attribution": ATTRIBUTION}


class ShapeBackfillRequest(BaseModel):
    country: Optional[str] = None
    statuses: list[str] = Field(default_factory=lambda: ["missing", "pending", "failed", "not_found"])
    destination_ids: Optional[list[str]] = None
    limit: int = Field(20, ge=1, le=50)
    dry_run: bool = True


@router.post("/shapes/backfill", summary="AA-847 — fetch the geometry of ≤ 50 line places (dry run by default)")
async def shapes_backfill(body: ShapeBackfillRequest, request: Request, x_admin_secret: str = Header(None),
                          x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.jobs.place_shape_job import ENABLED_ENV, enabled
    from services.places import geocoder as geocoder_mod
    from services.places import shape_sources, store
    from services.places.enqueue import SHAPE_KIND, enqueue_debounced
    bad = [s for s in body.statuses if s not in LINE_STATUSES or s == "manual"]
    if bad:
        raise HTTPException(status_code=422, detail=f"statuses: {bad} not allowed (manual shapes are never refetched)")
    ids = None
    if body.destination_ids:
        try:
            ids = [str(uuid.UUID(i)) for i in body.destination_ids]
        except ValueError:
            raise HTTPException(status_code=422, detail="destination_ids must be uuids")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = [_line_row(r) for r in await store.lines_report(conn, body.country or None, None)]
        used = await store.geocoder_requests_today(conn, "geoapify")
    rows = [r for r in rows if r["status"] in body.statuses and (ids is None or r["destination_id"] in ids)]
    rows = rows[:body.limit]
    provider = geocoder_mod.provider_name()
    calls = provider == "geoapify"
    with_place_id = sum(1 for r in rows if r.get("geocoder_place_id"))
    est_credits = (shape_sources.DETAILS_CREDITS + shape_sources.FULL_GEOMETRY_CREDITS) * with_place_id if calls else 0
    limit = int(geocoder_mod._env_float("GEOAPIFY_DAILY_CREDIT_LIMIT", geocoder_mod.DEFAULT_DAILY_CREDITS))
    out = {"dry_run": body.dry_run, "country": body.country, "provider": provider, "enabled": enabled(),
           "estimate": {"names": len(rows), "geoapify_details_names": with_place_id if calls else 0,
                        "geoapify_credits_max": est_credits,
                        "overpass_queries_max": len(rows) if calls else 0,
                        "credits_used_today": used, "daily_credit_limit": limit,
                        "credits_note": "Place Details credits per call are assumed (1 details + 1 full_geometry); "
                                        "the trial reads the real use from the Geoapify dashboard"},
           "items": [{k: r[k] for k in ("destination_id", "name", "country", "status", "tour_days")} for r in rows]}
    refuse = []
    if not enabled():
        refuse.append(f"{ENABLED_ENV} is off")
    if used + est_credits > limit:
        refuse.append("expected credits above today's remaining Geoapify credits")
    out["would_refuse"] = refuse
    if body.dry_run:
        return out
    if refuse:
        raise HTTPException(status_code=409, detail={"error": "; ".join(refuse), "estimate": out["estimate"]})
    if not rows:
        return {**out, "forced": 0, "job": None}
    async with pool.acquire() as conn:
        async with conn.transaction():
            forced = await store.force_shapes_due(conn, [r["destination_id"] for r in rows])
    job, _ = await enqueue_debounced(pool, SHAPE_KIND, reason=f"backfill:admin:{x_admin_user_id or 'unknown'}",
                                     created_by=f"backfill:admin:{x_admin_user_id or 'unknown'}", debounce_s=0)
    logger.info("places_shapes_backfill", names=len(rows), forced=forced, credits_max=est_credits)
    return {**out, "forced": forced, "job": job}


class ManualShapeRequest(BaseModel):
    geojson: dict
    note: Optional[str] = None


@router.put("/{destination_id}/shape", summary="AA-847 — set a line place's geometry by hand (an exception path)")
async def put_manual_shape(destination_id: str, body: ManualShapeRequest, request: Request,
                           x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import guards, shape, store
    try:
        destination_id = str(uuid.UUID(destination_id))
    except ValueError:
        raise HTTPException(status_code=422, detail="destination_id must be a uuid")
    parts = shape.parts_from_geojson(body.geojson)
    if not parts:
        raise HTTPException(status_code=422, detail="geojson must be a LineString / MultiLineString with ≥ 2 points")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        dest = await conn.fetchrow(
            """SELECT id::text AS id, name, country, geo_precision FROM shared.destinations
                WHERE id = $1::uuid AND duplicate_of IS NULL""", destination_id)
        if dest is None:
            raise HTTPException(status_code=404, detail="destination not found")
        if dest["geo_precision"] != "line":
            raise HTTPException(status_code=409, detail="destination is not a line (geo_precision)")
        clipped = shape.clip_to_country(parts, guards.iso2(dest["country"]))
        if not clipped:
            raise HTTPException(status_code=422, detail=f"the line is not inside {dest['country']}")
        length = shape.length_km(clipped)
        meta = {"by": f"admin:{x_admin_user_id or 'unknown'}", "note": (body.note or "")[:300]}
        async with conn.transaction():
            await store.write_manual_shape(conn, destination_id, shape.levels(clipped), shape.bbox(clipped), length,
                                           meta)
    logger.info("places_manual_shape", destination_id=destination_id, length_km=round(length, 1))
    return {"destination_id": destination_id, "status": "manual", "length_km": round(length, 1),
            "vertices": shape.vertex_count(clipped), "attribution": ATTRIBUTION}


@router.delete("/{destination_id}/shape", summary="AA-847 — drop a manual geometry; the job fetches it again")
async def delete_manual_shape(destination_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import store
    try:
        destination_id = str(uuid.UUID(destination_id))
    except ValueError:
        raise HTTPException(status_code=422, detail="destination_id must be a uuid")
    async with request.app.state.pool.acquire() as conn:
        changed = await store.clear_manual_shape(conn, destination_id)
    if not changed:
        raise HTTPException(status_code=404, detail="no manual shape for this destination")
    return {"destination_id": destination_id, "status": "pending"}
