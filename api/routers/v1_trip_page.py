"""AA-482 — [BUILD] Landing page engine (ADR-2026-030, D5: AA HOSTS the white-label tour page).

STEP0 (this task, 04/09/2026) confirmed live:
  - acp_deliver.tenant_tour_pages EXISTS (migration 078) and has 87 rows -- NOT the "0 rows"
    the issue's own description (citing AA-478, 06/08) expected. But every one of those 87
    rows points to the SAME placeholder URL (https://aa-cis.lumiguides.it.com/, the admin
    frontend's own root), all with the identical bulk-insert published_at timestamp -- a
    one-time manual/script seed, not a real per-tour page. 0 real landing pages exist today.
    (AA-859 wave 4, S247: the 22/10/2026 schema dump shows ~0 rows; since CIS 265 the table has RLS, this
    router writes it on the main pool as cis_app through tenant_tour_pages_cis_app_all.)
  - Confirmed (grep, re-verified against current code): still 0 real code callers write to
    this table -- AA-478's "0 writers" finding still holds, only the "0 rows" half was stale.
  - T10's gate F6 (services/acp_produce/gates.py, services/acp_produce/pipeline.py) already
    reads `url_alive` from this exact table for real (not mocked) -- fail-closed when no row
    exists. So a real row here has real, immediate downstream effect for any N7-class caller.

This module is the FIRST real slice: a public (no-auth) page-data endpoint the new
frontend/app/trip/[tourId]/page.tsx route renders, plus the write path that was missing
entirely -- an admin-triggered publish endpoint that computes a REAL url (this route's own
URL, not a placeholder) and a lightweight recheck endpoint for the D5 liveness-check ask.

Architecture decisions made here (real, not deferred -- see docs/implementation-notes/AA-482.md
for the full reasoning, this was a genuinely open design question per the issue's own STEP0
list, AskUserQuestion was unavailable in this session so a considered default was picked and
flagged rather than blocking indefinitely):
  - Hosting: a Next.js route in the EXISTING AA-CIS-App frontend (Vercel, already live, atomic
    deploy + instant rollback per this repo's own CLAUDE.md), NOT new S3+CloudFront infra --
    reuses the existing deploy pipeline, no new domain/cert/Terraform needed for a v1.
  - Domain: existing aa-cis.lumiguides.it.com, path-based (/trip/{tour_id}) -- no new domain
    provisioning for a v1.
  - Content source: gold_aa_internal.published_tours' aa_* fields directly (aa_name/
    aa_subtitle/aa_summary/aa_itineraries/aa_highlights/seo_title/seo_meta) -- exactly the
    field set ADR-2026-030's own v_trip_registry view already selects for this purpose. NOT
    T9's landing_page-channel AIDA copy (content_piece) -- that's generated per weekly slot/
    campaign, not 1:1 with a canonical tour, and reusing it would need new "pick the canonical
    piece" logic with no clear v1 benefit.
  - Scope: this v1 serves aa_internal's admin-canonical published tours (the shared catalog) --
    matches v_trip_registry's own real, current JOIN shape exactly. Real per-TENANT rewritten
    versions (gold_aa_internal.tenant_tour_versions) are NOT wired into this page yet -- a
    separate, real follow-up (v_trip_registry's own ttp JOIN is only ON tour_id, not
    tenant_id+tour_id, so it can't already disambiguate multiple tenants' pages for the same
    tour_id today either -- a pre-existing gap in the view itself, not introduced or fixed
    here, flagged in the implementation notes).
"""
import datetime as _dt
import json
import uuid

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from api.core.staff_authz import staff_policy

from api.routers.admin import verify_admin_secret

logger = structlog.get_logger()

router = APIRouter(prefix="/v1/trip", tags=["Trip Page (public)"])
# AA-882 (GĐ3): staff_policy narrows every route listed in api/core/staff_authz.py POLICY by role.
admin_router = APIRouter(prefix="/admin/trip-pages", tags=["Trip Page (admin)"],
                         dependencies=[Depends(staff_policy)])

_AA_INTERNAL_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _jsonb(val):
    """asyncpg does not auto-decode jsonb columns by default in this pool's config -- same
    local helper idiom admin_pipeline.py/admin_settings.py already use for aa_highlights.
    Live-verify (04/09/2026) caught this for real: without it, /v1/trip/{id} returned
    highlights as a JSON-encoded STRING, not a real array -- the frontend's
    `trip.highlights.map(...)` would have thrown at runtime on every real page."""
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (TypeError, ValueError):
            return val
    return val


async def _fetch_page_data(conn, tour_id: str):
    """Real query -- same field set + eligibility rule as ADR-2026-030's own v_trip_registry
    (pt.master_status='active' AND pt.deleted_at IS NULL), scoped to a single tour_id."""
    return await conn.fetchrow("""
        SELECT
            rt.tour_id::text, rt.country, rt.duration,
            pt.aa_name, pt.aa_subtitle, pt.aa_summary, pt.aa_itineraries,
            pt.aa_highlights, pt.seo_title, pt.seo_meta
        FROM silver_aa_internal.raw_tours rt
        JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id
        WHERE rt.tour_id = $1::uuid
          AND pt.master_status = 'active'
          AND pt.deleted_at IS NULL
          AND (rt.source_status IS NULL OR rt.source_status::text != 'trashed')
          AND rt.deleted_at IS NULL
    """, tour_id)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


@router.get("/{tour_id}")
async def get_trip_page(tour_id: str, request: Request):
    """Public, no-auth -- the actual data the /trip/{tour_id} frontend page renders. Real D5
    liveness self-heal: every real render (a row genuinely exists and is servable) stamps
    url_alive=true/last_checked_at=now() on ITS OWN tenant_tour_pages row (aa_internal); a
    tour_id that no longer qualifies flips that row's url_alive=false the same way, then 404s
    -- no separate HTTP-ping cron needed since we host the page ourselves and can check the
    underlying eligibility directly, cheaper and more reliable than an HTTP loopback would be.
    """
    # A tour id that is not a UUID can never be a page: 404 before any query (it was a 500 from asyncpg,
    # Sentry AA-CIS-API-E, and the self-heal UPDATE must not run for it).
    if not _is_uuid(tour_id):
        raise HTTPException(status_code=404, detail="Trip page not found")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await _fetch_page_data(conn, tour_id)
        now = _dt.datetime.now(_dt.timezone.utc)
        if row:
            await conn.execute("""
                UPDATE acp_deliver.tenant_tour_pages
                SET url_alive = true, last_checked_at = $3
                WHERE tenant_id = $1 AND tour_id = $2::uuid
            """, _AA_INTERNAL_TENANT_ID, tour_id, now)
        else:
            await conn.execute("""
                UPDATE acp_deliver.tenant_tour_pages
                SET url_alive = false, last_checked_at = $3
                WHERE tenant_id = $1 AND tour_id = $2::uuid
            """, _AA_INTERNAL_TENANT_ID, tour_id, now)

    if not row:
        raise HTTPException(status_code=404, detail="Trip page not found")

    return {
        "tour_id":       row["tour_id"],
        "country":       row["country"],
        "duration":      row["duration"],
        "name":          row["aa_name"],
        "subtitle":      row["aa_subtitle"],
        "summary":       row["aa_summary"],
        "itineraries":   row["aa_itineraries"],
        "highlights":    _jsonb(row["aa_highlights"]) or [],
        "seo_title":     row["seo_title"],
        "seo_meta":      row["seo_meta"],
    }


@admin_router.post("/{tour_id}/publish")
async def publish_trip_page(
    tour_id: str, request: Request, x_admin_secret: str = Header(None),
):
    """Admin-triggered publish -- the write path this table has never had (STEP0: 0 code
    callers, re-verified). Computes the REAL page URL (this app's own route, not a
    placeholder) and upserts a real acp_deliver.tenant_tour_pages row. Scoped to aa_internal
    for this v1 (see module docstring) -- tenant param intentionally not yet exposed."""
    verify_admin_secret(x_admin_secret)
    if not _is_uuid(tour_id):
        raise HTTPException(status_code=404, detail="Tour not found")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await _fetch_page_data(conn, tour_id)
        if not row:
            raise HTTPException(
                status_code=422,
                detail="Tour is not eligible for a trip page (not found, not active, or "
                       "deleted) -- cannot publish a page for content that doesn't qualify.",
            )
        real_url = f"https://aa-cis.lumiguides.it.com/trip/{tour_id}"
        now = _dt.datetime.now(_dt.timezone.utc)
        await conn.execute("""
            INSERT INTO acp_deliver.tenant_tour_pages
                (tenant_id, tour_id, url, published_at, url_alive, last_checked_at)
            VALUES ($1, $2::uuid, $3, $4, true, $4)
            ON CONFLICT (tenant_id, tour_id) DO UPDATE
                SET url = EXCLUDED.url, published_at = EXCLUDED.published_at,
                    url_alive = true, last_checked_at = EXCLUDED.last_checked_at
        """, _AA_INTERNAL_TENANT_ID, tour_id, real_url, now)

    logger.info("trip_page_published", tour_id=tour_id, url=real_url)
    return {"status": "published", "tour_id": tour_id, "url": real_url}


@admin_router.post("/recheck")
async def recheck_trip_pages(request: Request, x_admin_secret: str = Header(None)):
    """D5 liveness check, batch form -- re-verifies every existing tenant_tour_pages row's
    underlying tour still qualifies, flips url_alive accordingly. Direct DB check (we host
    the pages ourselves), not an HTTP round-trip against our own domain."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    now = _dt.datetime.now(_dt.timezone.utc)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tenant_id, tour_id::text FROM acp_deliver.tenant_tour_pages"
        )
        flipped_dead = 0
        checked = 0
        for r in rows:
            data = await _fetch_page_data(conn, r["tour_id"])
            alive = data is not None
            if not alive:
                flipped_dead += 1
            await conn.execute("""
                UPDATE acp_deliver.tenant_tour_pages
                SET url_alive = $3, last_checked_at = $4
                WHERE tenant_id = $1 AND tour_id = $2::uuid
            """, r["tenant_id"], r["tour_id"], alive, now)
            checked += 1

    logger.info("trip_pages_rechecked", checked=checked, flipped_dead=flipped_dead)
    return {"checked": checked, "flipped_dead": flipped_dead}
