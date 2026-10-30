"""AA-712 — place pipeline admin API (behind the admin BFF, admin secret).

  POST /admin/places/backfill  {country?, limit ≤ 50, tour_ids? (≤ 100), only_stale, dry_run, force}
       dry_run (default true): the tours it would take + expected calls and cost, no call made.
       tour_ids (AA-847): exactly these tours (live tour id or source id; e.g. the coverage gate's missing list)
       instead of country / limit (422 with country); ids not live, or fresh under only_stale, come back in
       `skipped`. The estimate prices each model stage at its live route (shared.llm_model_catalog).
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
AA-847 Problem 2 (automatic duplicate merge, migration 257; owner decision S246: no manual merges as the normal path):
  GET  /admin/places/merges?country=&active=&page=&page_size=     the merge log (geo.place_merge), newest first
  POST /admin/places/merges/{merge_id}/undo {dry_run = true, reason?}
       clear duplicate_of, restore the pointers that still hold the keeper, `different` pin, relink + graph build +
       place_dedupe queued; 409 on a dependent later merge, a name-key collision or an admin merge (validity fix undo)
  GET  /admin/places/merges/labels   undone merges as labelled keep pairs (tests/unit/fixtures/aa847_dedupe_pairs.json)
  GET  /admin/places/dedupe          the allow-list (geo.place_dedupe_country) + the last place_dedupe run per country
  POST /admin/places/dedupe/countries {country, mode: off | dry_run | apply, diff_hash?}
       dry_run: the country gets the paid model band, report only; apply: needs the diff_hash of the latest dry run of
       the current rule version — that diff is what the job applies (changes outside it count against the breaker)
  POST /admin/places/dedupe/run {mode = dry_run | apply, country?}   queue one place_dedupe run

AA-997 (S254 geocode audit, fix plan step 1: coordinates come from the geocoder only):
  POST /admin/places/relocate {mode = dry_run | apply | hide | unhide, located_by = ["llm"] (| "mapbox"), country?,
                               limit ≤ 400, hide_unconfirmed = false, strategy = v1 | v2, max_requests?,
                               accept_rule = v1 | aa997.2 | aa997.3,
                               recheck = [] (llm_point | off_route | confirmed | geoapify (dry run only)),
                               reasons? + confirm = false (hide)}
       queue one place_relocate run (services/places/relocate.py). dry_run: no call, no write — counts (non-place
       names, points off the tours' route, Hong Kong / Macao stored as China) and the Geoapify credit / day estimate
       in the job result (GET /admin/job-runner/jobs/{id}). apply: the geocoder-only path per row, ≤ limit rows a run,
       re-enqueued until done; 409 when the latest dry run of this rule version and selection is missing.
       AA-997 v2: recheck re-runs rows an earlier rule version stamped (confirmed / geoapify need accept_rule
       aa997.2 or aa997.3: the strict acceptance; geoapify is dry-run only); strategy v2 = query variants + hint bias;
       max_requests caps the chain's Geoapify requests (counted from geo.geocode_call of the chain's jobs).
       hide (report; confirm=true writes) / unhide: the readers' hide-unconfirmed policy — rows still flagged
       llm_point / off_route / unconfirmed_hard_type / weak_confirm (+ non_place:* only when "non_place" is in
       reasons) become suspect (hidden everywhere); unhide reverts the rows still flagged for the reason they were
       hidden and keeps the record (unhidden_at). No paid call.
       AA-997 S260 hide_flagged {outcomes = [llm_point, off_route] (| weak_confirm), stamp_versions?, stamp_accept?,
       country?, confirm = false}: the same hide scoped by the relocate stamp — only rows a relocate run already
       processed and left on the model's point (still flagged, still on that point); the report (confirm=false)
       counts them per country / outcome / reason / stamp version with a sample; confirm=true needs the latest
       report of the same selection (409 otherwise) and refuses on the job when more rows match than that report
       (max_rows); one audit row per batch. unhide {by?, destination_ids?}: the policy stamps to revert (default
       all), only these rows (≤ 500; the single-row restore of the review list), audited.

Every response that shows coordinates carries the Geoapify / OpenStreetMap attribution (ADR 0007).
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from api.core.staff_authz import staff_policy
from pydantic import BaseModel, Field, field_validator, model_validator

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
    tour_ids: Optional[list[str]] = Field(None, min_length=1, max_length=100)
    only_stale: bool = True
    dry_run: bool = True
    force: bool = False

    @field_validator("tour_ids")
    @classmethod
    def _uuid_ids(cls, v):
        if v is None:
            return v
        from services.places.backfill import normalize_tour_ids
        try:
            return normalize_tour_ids(v)
        except (ValueError, TypeError):
            raise ValueError("tour_ids must be tour or source ids (uuid)")

    @model_validator(mode="after")
    def _one_selector(self):
        if self.tour_ids and self.country:
            raise ValueError("give tour_ids or country, not both")
        return self


@router.post("/backfill", summary="Backfill place_extract for ≤ 50 live tours or ≤ 100 listed tours "
                                  "(dry run by default)")
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
        skipped = None
        if body.tour_ids:
            tours, skipped = await bf.plan_ids(conn, body.tour_ids, only_stale=body.only_stale)
        else:
            tours = await bf.plan(conn, country=body.country, limit=body.limit, only_stale=body.only_stale)
        est = await bf.estimate(conn, tours, geocoder.cost_per_request, geocoder.calls_external)
    out = {"dry_run": body.dry_run, "country": body.country, "only_stale": body.only_stale,
           "geocoder": geocoder.name, "busy": busy, "estimate": est,
           **({"requested": len(body.tour_ids), "skipped": skipped} if body.tour_ids else {}),
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
                listed=len(body.tour_ids) if body.tour_ids else None, expected_cost_usd=est["expected_cost_usd"])
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


@router.get("/validity", summary="Content-validity report of the live catalog's places (no model call)")
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


@router.get("/coverage", summary="Place pipeline coverage per country (live / built / graph / stop per day)")
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


@router.post("/validity/fix", summary="Fix content-validity findings (dry run by default; audit + undo)")
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


@router.get("/lines", summary="Line places (rivers, roads, trails) and the state of their geometry")
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


@router.post("/shapes/backfill", summary="Fetch the geometry of ≤ 50 line places (dry run by default)")
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


@router.put("/{destination_id}/shape", summary="Set a line place's geometry by hand (an exception path)")
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


@router.delete("/{destination_id}/shape", summary="Drop a manual geometry; the job fetches it again")
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
# ── AA-847 Problem 2: automatic duplicate merge ─────────────────────────────────────────────────────────────────────

_LAST_DEDUPE_SQL = """
    SELECT id::text AS id, status, created_at, finished_at, payload, result
      FROM shared.job WHERE kind = 'place_dedupe' AND status = 'succeeded'
     ORDER BY finished_at DESC NULLS LAST LIMIT 1
"""
_OPEN_RELOCATE_APPLY_SQL = """
    SELECT id::text AS id FROM shared.job
     WHERE kind = $1 AND status IN ('queued', 'running') AND payload ->> 'mode' = 'apply'
       -- AA-997 v2: a chain queued under another rule version is not reused (it refuses itself when it runs)
       AND (payload ->> 'rule_version') IS NOT DISTINCT FROM $2
     ORDER BY created_at LIMIT 1
"""


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


_COUNTRY_FIELDS = ("status", "listed", "usable", "merges", "llm_needed", "diff_size", "diff_hash", "outside",
                   "breaker_limit", "applied", "band_pairs", "band_asked", "band_cached", "band_unasked",
                   "embedding_promoted")
# The job stores these three as full lists (the plan); the admin view shows a count plus a capped detail list.
_COUNTED_FIELDS = ("possible", "conflicts", "defects")
_DETAIL_CAP = 50


def _country_view(v: dict) -> dict:
    """One country of the last place_dedupe result, as the Merges tab reads it: scalars only, plus capped lists.

    possible / conflicts / defects are lists in the job result but counts in this view (the tab rendered the raw
    list, and a non-empty list of objects crashed it: S252, China's first conflict)."""
    out = {k: v.get(k) for k in _COUNTRY_FIELDS}
    for k in _COUNTED_FIELDS:
        x = v.get(k)
        out[k] = len(x) if isinstance(x, list) else (x if isinstance(x, int) else 0)
        if isinstance(x, list):
            out[f"{k}_detail"] = x[:_DETAIL_CAP]
    out["merges_planned"] = (v.get("merges_planned") or [])[:_DETAIL_CAP]
    out["undos"] = (v.get("undos") or [])[:_DETAIL_CAP]
    return out


@router.get("/merges", summary="The duplicate-place merge log (job and admin merges, with undo state)")
async def list_merges(request: Request, country: Optional[str] = None, active: Optional[bool] = None,
                      page: int = 1, page_size: int = 50, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import merges
    async with request.app.state.pool.acquire() as conn:
        return await merges.list_merges(conn, country=country or None, active=active, page=page,
                                        page_size=page_size)


@router.get("/merges/labels", summary="Undone merges as labelled keep pairs for the CI pair set")
async def merge_labels(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import merges
    async with request.app.state.pool.acquire() as conn:
        return {"keep_pairs": await merges.labelled_keeps(conn),
                "how": "add each as a 'keep' pair to tests/unit/fixtures/aa847_dedupe_pairs.json"}


class UndoMergeRequest(BaseModel):
    dry_run: bool = True
    reason: Optional[str] = Field(None, max_length=500)


@router.post("/merges/{merge_id}/undo", summary="Undo one duplicate-place merge (dry run by default)")
async def undo_merge(merge_id: str, body: UndoMergeRequest, request: Request, x_admin_secret: str = Header(None),
                     x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import merges, store
    from services.places.enqueue import GRAPH_KIND, enqueue_debounced, enqueue_dedupe
    try:
        uuid.UUID(merge_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="merge_id must be a uuid")
    actor = f"admin:{x_admin_user_id or 'unknown'}"
    try:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                out = await merges.undo_merge(conn, merge_id, actor=actor, reason=body.reason or "admin undo",
                                              dry_run=body.dry_run)
                if not body.dry_run:
                    out["relinked"] = await store.relink(conn)
                    out["graph_job"], _ = await enqueue_debounced(conn, GRAPH_KIND, reason="place_merge_undo",
                                                                  created_by=actor)
                    out["dedupe_job"], _ = await enqueue_dedupe(conn, reason="place_merge_undo", created_by=actor)
    except merges.MergeError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    if not body.dry_run:
        _validity_cache.clear()
        logger.info("place_merge_undone", merge_id=merge_id, actor=actor, left=out.get("left"))
    return out


@router.get("/dedupe", summary="Allow-list and the last place_dedupe run per country")
async def dedupe_status(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import dedupe as dd
    from services.places import merges
    async with request.app.state.pool.acquire() as conn:
        allow = await merges.allow_list(conn)
        last = await conn.fetchrow(_LAST_DEDUPE_SQL)
    run = None
    if last is not None:
        res = _json(last["result"]) or {}
        countries = {c: _country_view(v) for c, v in (res.get("countries") or {}).items()}
        run = {"job_id": last["id"], "finished_at": last["finished_at"].isoformat() if last["finished_at"] else None,
               "mode": res.get("mode"), "rule_version": res.get("rule_version"),
               "model_calls": res.get("model_calls"), "model_cost_usd": res.get("model_cost_usd"),
               "countries": countries}
    for v in allow.values():
        v["updated_at"] = v["updated_at"].isoformat() if v.get("updated_at") else None
        v["approved_diff_size"] = len(v.pop("approved_diff") or [])
    return {"rule_version": dd.RULE_VERSION, "allow_list": allow, "last_run": run}


class DedupeCountryRequest(BaseModel):
    country: str = Field(..., min_length=1, max_length=80)
    mode: str = Field(..., pattern="^(off|dry_run|apply)$")
    diff_hash: Optional[str] = None


@router.post("/dedupe/countries", summary="Put a country on / off the place_dedupe allow-list, approve a "
                                          "dry run")
async def dedupe_country(body: DedupeCountryRequest, request: Request, x_admin_secret: str = Header(None),
                         x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.acp_shared.audit_log import write_audit_log
    from services.places import dedupe as dd
    from services.places import merges
    actor = f"admin:{x_admin_user_id or 'unknown'}"
    key = merges.country_key(body.country)
    diff = None
    try:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                if body.mode == "apply":
                    last = await conn.fetchrow(_LAST_DEDUPE_SQL)
                    res = _json(last["result"]) if last is not None else {}
                    c = ((res or {}).get("countries") or {}).get(key) or {}
                    if not body.diff_hash or c.get("diff_hash") != body.diff_hash:
                        raise merges.MergeError(409, {"error": "approve the diff of the latest dry run",
                                                      "latest": c.get("diff_hash"), "sent": body.diff_hash})
                    if (res or {}).get("rule_version") != dd.RULE_VERSION:
                        raise merges.MergeError(409, "the latest run used another rule version; run it again")
                    if c.get("diff_size") != len(c.get("diff") or []):
                        raise merges.MergeError(409, "the stored diff is incomplete; run the dry run again")
                    diff = c.get("diff") or []
                out = await merges.set_country(conn, key, None if body.mode == "off" else body.mode, actor=actor,
                                               diff=diff)
                await write_audit_log(conn, tenant_id=None, actor=actor, action="places.dedupe.country",
                                      resource_type="places_dedupe", resource_id=key,
                                      details={**out, "diff_size": len(diff or [])})
    except merges.MergeError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    logger.info("place_dedupe_country", actor=actor, **out)
    return out


class DedupeRunRequest(BaseModel):
    mode: str = Field("dry_run", pattern="^(dry_run|apply)$")
    country: Optional[str] = Field(None, max_length=80)


@router.post("/dedupe/run", summary="Queue one place_dedupe run (dry_run by default)")
async def dedupe_run(body: DedupeRunRequest, request: Request, x_admin_secret: str = Header(None),
                     x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places.enqueue import DEDUPE_KIND
    from shared.jobs.registry import enqueue
    actor = f"admin:{x_admin_user_id or 'unknown'}"
    async with request.app.state.pool.acquire() as conn:
        job_id, created = await enqueue(conn, DEDUPE_KIND, {"mode": body.mode, "country": body.country,
                                                            "reason": "admin"}, created_by=actor)
    return {"job_id": job_id, "created": created, "mode": body.mode, "country": body.country}


class RelocateRequest(BaseModel):
    mode: str = Field("dry_run", pattern="^(dry_run|apply|hide|unhide|hide_flagged)$")
    located_by: list[str] = Field(default_factory=lambda: ["llm"], min_length=1, max_length=2)
    country: Optional[str] = Field(None, max_length=80)
    limit: int = Field(400, ge=1, le=400)
    hide_unconfirmed: bool = False
    # AA-997 v2
    recheck: list[str] = Field(default_factory=list, max_length=4)
    strategy: str = Field("v1", pattern="^(v1|v2)$")
    accept_rule: str = Field("v1", pattern="^(v1|aa997\\.[23])$")
    max_requests: Optional[int] = Field(None, ge=1, le=100_000)
    reasons: Optional[list[str]] = Field(None, max_length=8)      # hide
    confirm: bool = False                                         # hide / hide_flagged: false = report only
    # AA-997 S260 hide_flagged / unhide
    outcomes: Optional[list[str]] = Field(None, max_length=3)
    stamp_versions: list[str] = Field(default_factory=list, max_length=4)
    stamp_accept: Optional[str] = Field(None, pattern="^(v1|aa997\\.[23])$")
    by: Optional[list[str]] = Field(None, max_length=4)            # unhide: the policy stamps to revert
    destination_ids: Optional[list[uuid.UUID]] = Field(None, max_length=500)   # unhide: only these rows

    @field_validator("outcomes")
    @classmethod
    def _outcomes(cls, v):
        from services.places.relocate import HIDE_FLAGGED_OUTCOMES
        if v is None:
            return v
        bad = [x for x in v if x not in HIDE_FLAGGED_OUTCOMES]
        if bad:
            raise ValueError(f"outcomes must be in {list(HIDE_FLAGGED_OUTCOMES)}")
        return sorted(set(v))

    @field_validator("stamp_versions")
    @classmethod
    def _stamp_versions(cls, v):
        bad = [x for x in v if not x.startswith("aa997.") or len(x) > 16]
        if bad:
            raise ValueError("stamp_versions are relocate rule versions such as 'aa997.1' or 'aa997.2'")
        return sorted(set(v))

    @field_validator("by")
    @classmethod
    def _by(cls, v):
        from services.places.relocate import POLICY_STAMPS
        if v is None:
            return v
        bad = [x for x in v if x not in POLICY_STAMPS]
        if bad:
            raise ValueError(f"by must be in {list(POLICY_STAMPS)}")
        return sorted(set(v))

    @field_validator("recheck")
    @classmethod
    def _recheck(cls, v):
        from services.places.relocate import RECHECK_ALLOWED
        bad = [x for x in v if x not in RECHECK_ALLOWED]
        if bad:
            raise ValueError(f"recheck must be in {list(RECHECK_ALLOWED)}")
        return sorted(set(v))

    @model_validator(mode="after")
    def _strict_rechecks(self):
        from services.places.relocate import RECHECK_DRY_RUN_ONLY, RECHECK_STRICT_ONLY, is_strict
        need = [x for x in self.recheck if x in RECHECK_STRICT_ONLY]
        if need and not is_strict(self.accept_rule):
            raise ValueError(f"recheck {need} re-judges stored answers with the strict rule: "
                             "send accept_rule 'aa997.2' or 'aa997.3'")
        held = [x for x in self.recheck if x in RECHECK_DRY_RUN_ONLY]
        if held and self.mode == "apply":
            raise ValueError(f"recheck {held} is dry-run only until a labelled sample clears it")
        return self

    @field_validator("located_by")
    @classmethod
    def _sources(cls, v):
        from services.places.relocate import LOCATED_BY_ALLOWED
        bad = [x for x in v if x not in LOCATED_BY_ALLOWED]
        if bad:
            raise ValueError(f"located_by must be in {list(LOCATED_BY_ALLOWED)}")
        return sorted(set(v))


_LAST_HIDE_FLAGGED_REPORT_SQL = """
    SELECT id::text AS id, result FROM shared.job
     WHERE kind = 'place_relocate' AND status = 'succeeded' AND payload ->> 'mode' = 'hide_flagged'
       AND coalesce((payload ->> 'confirm')::boolean, false) = false
     ORDER BY finished_at DESC NULLS LAST LIMIT 1
"""
_LAST_RELOCATE_DRY_RUN_SQL = """
    SELECT id::text AS id, result FROM shared.job
     WHERE kind = 'place_relocate' AND status = 'succeeded' AND payload ->> 'mode' = 'dry_run'
     ORDER BY finished_at DESC NULLS LAST LIMIT 1
"""


@router.post("/relocate", summary="Queue one place_relocate run (dry_run by default; apply after a dry run)")
async def relocate_run(body: RelocateRequest, request: Request, x_admin_secret: str = Header(None),
                       x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import relocate
    from services.places.enqueue import RELOCATE_KIND
    from shared.jobs.registry import enqueue
    actor = f"admin:{x_admin_user_id or 'unknown'}"
    payload = {"mode": body.mode, "located_by": body.located_by, "country": body.country, "limit": body.limit,
               "hide_unconfirmed": body.hide_unconfirmed, "reason": "admin", "rule_version": relocate.RULE_VERSION}
    if body.recheck:
        payload["recheck"] = body.recheck
    if body.strategy != "v1":
        payload["strategy"] = body.strategy
    if body.accept_rule != "v1":
        payload["accept_rule"] = body.accept_rule
    if body.max_requests:
        payload["max_requests"] = body.max_requests
        if body.mode == "apply":      # the chain's request count is read from geo.geocode_call of its jobs
            payload["chain_id"] = str(uuid.uuid4())
    if body.mode == "hide":
        payload["reasons"] = relocate.hide_reasons(body.reasons)
        payload["confirm"] = body.confirm
    if body.mode == "hide_flagged":
        payload.update({"outcomes": relocate.hide_flagged_outcomes(body.outcomes),
                        "stamp_versions": body.stamp_versions, "stamp_accept": body.stamp_accept,
                        "confirm": body.confirm})
    if body.mode == "unhide":
        payload["by"] = relocate.policy_stamps(body.by)
        if body.destination_ids:
            payload["destination_ids"] = sorted({str(i) for i in body.destination_ids})
    async with request.app.state.pool.acquire() as conn:
        if body.mode == "hide_flagged" and body.confirm:
            last = await conn.fetchrow(_LAST_HIDE_FLAGGED_REPORT_SQL)
            res = (_json(last["result"]) or {}) if last is not None else {}
            same = (last is not None and res.get("mode") == "hide_flagged"
                    and sorted(res.get("outcomes") or []) == payload["outcomes"]
                    and sorted(res.get("stamp_versions") or []) == payload["stamp_versions"]
                    and (res.get("stamp_accept") or None) == (body.stamp_accept or None)
                    and (res.get("country") or None) == (body.country or None))
            if not same:
                raise HTTPException(status_code=409, detail={
                    "error": "run the hide_flagged report of this selection first (confirm=false; same outcomes, "
                             "stamp_versions, stamp_accept, country)",
                    "last_report": last["id"] if last is not None else None})
            payload["dry_run_job"] = last["id"]
            payload["max_rows"] = int(res.get("rows") or 0)
        if body.mode == "apply":
            last = await conn.fetchrow(_LAST_RELOCATE_DRY_RUN_SQL)
            res = (_json(last["result"]) or {}) if last is not None else {}
            same = (res.get("rule_version") == relocate.RULE_VERSION and res.get("located_by") == body.located_by
                    and (res.get("country") or None) == (body.country or None)
                    and (res.get("recheck") or []) == body.recheck
                    and (res.get("strategy") or "v1") == body.strategy
                    and (res.get("accept_rule") or "v1") == body.accept_rule)
            if not same:
                raise HTTPException(status_code=409, detail={
                    "error": "run the dry run of this selection first (same rule version, located_by, country, "
                             "recheck, strategy, accept_rule)",
                    "rule_version": relocate.RULE_VERSION, "last_dry_run": last["id"] if last is not None else None})
            payload["dry_run_job"] = last["id"]
            busy = await conn.fetchrow(_OPEN_RELOCATE_APPLY_SQL, RELOCATE_KIND, relocate.RULE_VERSION)
            if busy is not None:    # one apply chain at a time: a second POST reuses it instead of a parallel chain
                return {"job_id": busy["id"], "created": False, "reused": True, **payload}
        job_id, created = await enqueue(conn, RELOCATE_KIND, payload, created_by=actor)
    logger.info("place_relocate_queued", actor=actor, job_id=job_id, **{k: v for k, v in payload.items()
                                                                          if k != "located_by"})
    return {"job_id": job_id, "created": created, **payload}
