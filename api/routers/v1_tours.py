import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from pydantic import BaseModel as _BM
from api.routers.auth import verify_jwt
from api.routers.admin import PLAN_LIMITS
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/tours", tags=["B2B Tours"])
# AA-489 — separate router, plain /v1 prefix (not /v1/tours): the pre-AA-428 dead frontend
# code and this issue's own text both call this `GET /v1/quota`, not `/v1/tours/quota`. Same
# split-router-per-file pattern v1_planning.py already uses for its own slate_router.
quota_router = APIRouter(prefix="/v1", tags=["Quota"])
security = HTTPBearer()


def get_tenant(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        return verify_jwt(credentials.credentials)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_pool(request: Request):
    return request.app.state.pool


# AA-579: bare `GET /v1/tours` (list_tours()) removed — tàn dư kiến trúc S8/S9 (21/04/2026,
# commit a5e207d), viết cho 1 model RLS-per-tenant-row trên published_tours chưa từng thành hiện
# thực (bị đè bởi Pool/Rewrite model 2 tuần sau, 05/05/2026, cùng file). `pt.tenant_id = $1` không
# bao giờ khớp vì published_tours 100% thuộc sentinel aa_internal — route luôn trả rỗng cho mọi
# tenant thật, độc lập RLS. CloudWatch 14 ngày (/ecs/aa-cis-dev) xác nhận 0 traffic tenant thật
# (chỉ 401 từ (internal)/catalog's proxy auth mismatch — bug khác, xem AA-579). Tính năng thật đã
# được /v1/tours/pool + /v1/tours/my-versions phủ đúng kiến trúc tenant_tour_versions hiện tại.
# Xem AA-579 (Linear) cho đầy đủ bằng chứng trước khi khôi phục route này.


# ── P3-S4: Shared Pool Browse ─────────────────────────────────────────────────

@router.get("/pool")
async def browse_pool(
    request: Request,
    tenant=Depends(get_tenant),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    country: Optional[str] = Query(None),
    min_quality: Optional[float] = Query(None, ge=0, le=10),
    search: Optional[str] = Query(None),
    duration_min: Optional[int] = Query(None, ge=1),
    duration_max: Optional[int] = Query(None),
    sort: str = Query("newest"),
):
    """Browse AA shared pool (published_tours from aa_internal).
    All active tenants can read the pool — RLS bypassed via aa_internal filter.
    """
    tenant_id = tenant["sub"]
    # INTENTIONAL: cross-tenant read via admin pool — see AA-544. All 3 queries below scan
    # gold_aa_internal.published_tours filtered to the aa_internal sentinel tenant (the shared
    # pool itself), not the caller's own tenant — this is AA-544's narrow, deliberate admin-pool
    # exception (Round 2 decision (a)), not RLS accidentally not applying. All active tenants
    # read the pool this way, unconditionally.
    #
    # Nothing in this function is eligible to move to the aa_app_user tenant pool: the
    # `already_rewritten` EXISTS subquery in the `rows` query below does reference the caller's
    # OWN tenant_id (gold_aa_internal.tenant_tour_versions), but as a correlated sub-select
    # inside the same cross-tenant SELECT — not a separable per-tenant query. Splitting it into
    # its own tenant-pool round trip would add a second DB call + in-Python merge for zero
    # behavior change; not worth it unless this function's shape changes for an unrelated reason.
    pool = request.app.state.pool
    offset = (page - 1) * page_size

    # AA-441 (bug #6, AA-438-00-SUMMARY #8): was missing the master_status/deleted_at gate that
    # acp_contract.v_trip_registry already applies (migrations 078/083/090) — a tour an admin
    # trashes/deactivates in Master Content stayed fully visible and rewritable to every tenant
    # here. Same condition v_trip_registry uses: master_status='active' AND deleted_at IS NULL.
    conditions = [
        "pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid",
        "pt.master_status = 'active'",
        "pt.deleted_at IS NULL",
    ]
    params: list = []

    if country:
        params.append(country)
        conditions.append(f"LOWER(rt.country) = LOWER(${len(params)})")
    if min_quality is not None:
        params.append(min_quality)
        conditions.append(f"pt.quality_score >= ${len(params)}")
    if search:
        params.append(f"%{search}%")
        conditions.append(f"pt.aa_name ILIKE ${len(params)}")
    if duration_min is not None:
        params.append(duration_min)
        conditions.append(
            f"CAST(NULLIF(REGEXP_REPLACE(COALESCE(rt.duration,''),'[^0-9]','','g'),'') AS INTEGER) >= ${len(params)}"
        )
    if duration_max is not None:
        params.append(duration_max)
        conditions.append(
            f"CAST(NULLIF(REGEXP_REPLACE(COALESCE(rt.duration,''),'[^0-9]','','g'),'') AS INTEGER) <= ${len(params)}"
        )

    order_by = "pt.published_at DESC" if sort == "newest" else "pt.quality_score DESC, pt.published_at DESC"
    where = "WHERE " + " AND ".join(conditions)

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"""
            SELECT COUNT(*)
            FROM gold_aa_internal.published_tours pt
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            {where}
        """, *params)

        # tenant_id added as last param for already_rewritten subquery
        params_paged = params + [page_size, offset, tenant_id]
        tid_idx = len(params) + 3  # position of tenant_id in params_paged
        rows = await conn.fetch(f"""
            SELECT pt.id, pt.tour_id, pt.aa_name, pt.aa_subtitle, pt.aa_summary,
                   pt.aa_highlights, pt.aa_itineraries, pt.aa_description,
                   pt.seo_title, pt.seo_meta, pt.seo_keywords_used,
                   pt.quality_score, pt.published_at,
                   rt.country, rt.duration, rt.price_raw,
                   EXISTS(
                       SELECT 1 FROM gold_aa_internal.tenant_tour_versions ttv
                       WHERE ttv.published_tour_id = pt.id
                         AND ttv.tenant_id = ${tid_idx}::uuid
                   ) AS already_rewritten
            FROM gold_aa_internal.published_tours pt
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            {where}
            ORDER BY {order_by}
            LIMIT ${len(params)+1} OFFSET ${len(params)+2}
        """, *params_paged)

        # Countries for filter dropdown — same master_status/deleted_at gate as the main listing
        # above, so a trashed/deactivated tour's country doesn't show as a filterable option.
        countries = await conn.fetch("""
            SELECT DISTINCT rt.country
            FROM gold_aa_internal.published_tours pt
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND pt.master_status = 'active'
              AND pt.deleted_at IS NULL
              AND rt.country IS NOT NULL
            ORDER BY rt.country
        """)

    return {
        "data": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size,
                       "total": total, "pages": -(-total // page_size)},
        "countries": [r["country"] for r in countries],
        "tenant_id": tenant_id,
    }


# ── AA-489: rewrite quota (shared helpers + GET /v1/quota) ────────────────────
# STEP0 found nothing enforces a tenant's monthly rewrite count: PLAN_LIMITS.tours_per_month
# (admin.py) was defined but only ever displayed, never checked; raw_tours.rewrite_count
# (migration 033) is dead, 0 callers; rate_limit_middleware throttles requests/minute across
# ALL /v1/*, not a monthly count. Deliberately NOT reusing acp_quota_ledger — that's ACPv1
# S2/S3/S4 quota, dead since 13/07/2026. New table: shared.tenant_rewrite_usage (migration
# 142), one row per (tenant_id, year_month).
#
# Business decisions — confirmed by Nghiệp 25/09/2026 (AA-640), replacing AA-489's defaults:
#   * Limit = shared.membership_plans.tours_quota_monthly for the tenant's plan — the SAME number
#     the portal shows and billing (v_tenant_monthly_usage overage) uses. PLAN_LIMITS in admin.py
#     used to carry a second, different set (growth 500 vs 200 shown) — it now only holds RPM.
#   * Reset = calendar month, not rolling 30d.
#   * NO hard block while the product is in trial: a rewrite past the quota is allowed and billed
#     as overage (overage_rate_usd_per_tour, already computed by v_tenant_monthly_usage). Logged as
#     `rewrite_over_quota` so it stays visible.


def _current_year_month_and_reset() -> tuple:
    """(year_month 'YYYY-MM' string, first-of-next-month date) in UTC."""
    import datetime as _dt_quota
    now_utc = _dt_quota.datetime.now(_dt_quota.timezone.utc)
    next_month = (now_utc.replace(day=1) + _dt_quota.timedelta(days=32)).replace(day=1)
    return now_utc.strftime("%Y-%m"), next_month


async def _get_tenant_plan_limit(conn, tenant_id: str) -> tuple:
    """(plan_tier str, tours_quota_monthly int) — plan read live from shared.tenants, not the
    JWT's own plan_tier claim (same rationale AA-432 established for rate_limit_rpm/is_active: a
    plan change shouldn't take up to 24h, the JWT's TTL, to take effect). The quota comes from
    shared.membership_plans (AA-640); a plan with no row falls back to the starter row."""
    row = await conn.fetchrow("""
        SELECT t.plan_tier::text AS plan,
               COALESCE(mp.tours_quota_monthly,
                        (SELECT tours_quota_monthly FROM shared.membership_plans
                         WHERE plan_name = 'starter')) AS quota
        FROM shared.tenants t
        LEFT JOIN shared.membership_plans mp ON mp.plan_name = t.plan_tier::text
        WHERE t.tenant_id = $1::uuid
    """, tenant_id)
    if not row:
        return "starter", 0
    return str(row["plan"]), int(row["quota"] or 0)


async def _check_and_consume_rewrite_quota(conn, tenant_id: str) -> None:
    """Atomically increments this month's rewrite count. AA-640: past the plan quota the rewrite
    is still allowed (billed as overage) — logged, never blocked, while the product is in trial."""
    plan, limit = await _get_tenant_plan_limit(conn, tenant_id)
    year_month, _next_month = _current_year_month_and_reset()
    used = await conn.fetchval("""
        INSERT INTO shared.tenant_rewrite_usage (tenant_id, year_month, rewrite_count)
        VALUES ($1::uuid, $2, 1)
        ON CONFLICT (tenant_id, year_month)
        DO UPDATE SET rewrite_count = shared.tenant_rewrite_usage.rewrite_count + 1,
                      updated_at = NOW()
        RETURNING rewrite_count
    """, tenant_id, year_month)
    if used > limit:
        logger.info("rewrite_over_quota", tenant_id=tenant_id, plan=plan, used=used, quota=limit,
                    year_month=year_month)


# Real replacement for the endpoint CatalogTab.tsx used to call before AA-428 deleted that
# dead code (STEP0 there found this route never existed). Read-only — does NOT increment
# shared.tenant_rewrite_usage (only _check_and_consume_rewrite_quota, called from
# trigger_rewrite() below, does that, on an actual attempt). `rewrites_remaining` kept as the
# field name the old removed UI already expected, so a future UI rebuild (the issue's own "nếu
# muốn") can reuse this shape as-is.
@quota_router.get("/quota")
async def get_quota(request: Request, tenant=Depends(get_tenant)):
    tenant_id = tenant["sub"]
    pool = request.app.state.pool
    year_month, next_month = _current_year_month_and_reset()

    async with pool.acquire() as conn:
        plan, limit = await _get_tenant_plan_limit(conn, tenant_id)
        used = await conn.fetchval("""
            SELECT rewrite_count FROM shared.tenant_rewrite_usage
            WHERE tenant_id = $1::uuid AND year_month = $2
        """, tenant_id, year_month) or 0

    return {
        "plan_tier":           plan,
        "tours_per_month":     limit,
        "rewrites_used":       used,
        "rewrites_remaining":  max(limit - used, 0),
        "resets_at":           next_month.strftime("%Y-%m-%d"),
    }


# ── AA-496: GET /v1/billing — tenant-scoped self-service billing view ─────────
# STEP0 found: `/portal/*`'s layout.tsx + DashboardTab.tsx both `fetch("/api/admin/billing")`
# unconditionally on every page load. That proxy (`/api/admin/[...path]/route.ts`) calls
# `requireAdmin()` first — a real, independent admin-JWT check — before ever attaching
# X-Admin-Secret. A tenant portal session is never an admin session, so this 401s at the
# Next.js proxy layer on literally every real tenant, on every page, always — confirmed by
# reading both call sites and the backend `GET /admin/billing` (admin_pipeline.py): it takes
# an arbitrary `?tenant_id=` query param with NO scoping to "the caller's own tenant" (by
# design — it's an admin looking-glass over ANY tenant, defaulting to aa_internal), so it
# could never safely be exposed to a tenant session even with a header change alone.
# This is possibility #1 from the issue text ("gọi sai chỗ"), not a missing-feature gap:
# `/v1/quota` (AA-489, just above) already proves the tenant-scoped-via-JWT pattern this
# needed. Real fix: a tenant-scoped sibling endpoint, JWT-derived tenant_id (never trusts a
# query param), reusing the exact same v_tenant_monthly_usage view + shape the admin view
# already returns (so BillingTab.tsx/DashboardTab.tsx need zero shape changes) — frontend
# repointed from /api/admin/billing to /api/tenant/v1/billing (see those 2 files' own diffs).
@quota_router.get("/billing")
async def get_my_billing(request: Request, tenant=Depends(get_tenant)):
    tenant_id = tenant["sub"]
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                v.tenant_name, v.plan_tier, v.billing_month,
                v.tours_quota_monthly, v.api_calls_quota_monthly,
                v.price_usd_monthly,
                v.tours_rewritten, v.api_calls_used,
                v.quota_tours_pct, v.quota_calls_pct,
                v.tours_overage, v.overage_usd, v.llm_cost_usd,
                v.overage_rate_usd_per_tour
            FROM shared.v_tenant_monthly_usage v
            WHERE v.tenant_id = $1::uuid
        """, tenant_id)

        activity = await conn.fetch("""
            SELECT ttv.id, ttv.created_at, ttv.status, ttv.edit_source,
                   pt.aa_name, rt.country
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE ttv.tenant_id = $1::uuid
            ORDER BY ttv.created_at DESC LIMIT 5
        """, tenant_id)

        # AA-636: the portal hardcoded "300 RPM", "20,000 API calls" and a Growth-is-current plan
        # table for every tenant. Serve the tenant's real rate limit and the sellable plans
        # (shared.membership_plans, the same table v_tenant_monthly_usage reads) instead.
        rate_limit_rpm = await conn.fetchval(
            "SELECT rate_limit_rpm FROM shared.tenants WHERE tenant_id = $1::uuid", tenant_id
        )
        plan_rows = await conn.fetch("""
            SELECT plan_name, tours_quota_monthly, api_calls_quota_monthly, price_usd_monthly
            FROM shared.membership_plans
            WHERE is_active AND plan_name <> 'internal'
            ORDER BY (price_usd_monthly IS NULL OR price_usd_monthly <= 0), price_usd_monthly, tours_quota_monthly
        """)
        # AA-638 — per-day API calls + tour rewrites for the current month (Dashboard sparkline).
        daily_rows = await conn.fetch("""
            WITH days AS (
                SELECT generate_series(date_trunc('month', now()), date_trunc('day', now()),
                                       interval '1 day') AS day
            ),
            api AS (
                SELECT date_trunc('day', called_at) AS day, COUNT(*) AS n
                FROM shared.tenant_api_usage
                WHERE tenant_id = $1::uuid AND called_at >= date_trunc('month', now())
                GROUP BY 1
            ),
            rw AS (
                SELECT date_trunc('day', created_at) AS day, COUNT(*) AS n
                FROM gold_aa_internal.tenant_tour_versions
                WHERE tenant_id = $1::uuid AND created_at >= date_trunc('month', now())
                GROUP BY 1
            )
            SELECT days.day::date AS day, COALESCE(api.n, 0) AS api_calls, COALESCE(rw.n, 0) AS rewrites
            FROM days
            LEFT JOIN api ON api.day = days.day
            LEFT JOIN rw ON rw.day = days.day
            ORDER BY days.day
        """, tenant_id)
    daily = [
        {"day": str(d["day"]), "api_calls": int(d["api_calls"]), "rewrites": int(d["rewrites"])}
        for d in daily_rows
    ]
    plans = [
        {
            "plan_name": p["plan_name"],
            "tours_quota_monthly": p["tours_quota_monthly"],
            "api_calls_quota_monthly": p["api_calls_quota_monthly"],
            # enterprise is stored as 0.00 = negotiated, not free -> served as None ("Custom")
            "price_usd_monthly": (
                float(p["price_usd_monthly"]) if p["price_usd_monthly"] and p["price_usd_monthly"] > 0 else None
            ),
            "rate_limit_rpm": PLAN_LIMITS.get(p["plan_name"], {}).get("rpm"),
        }
        for p in plan_rows
    ]

    if not row:
        return {
            "plan_tier": "starter", "tours_quota_monthly": 50,
            "api_calls_quota_monthly": 5000, "price_usd_monthly": 299.0,
            "tours_rewritten": 0, "api_calls_used": 0,
            "quota_tours_pct": 0.0, "quota_calls_pct": 0.0,
            "tours_overage": 0, "overage_usd": 0.0,
            "llm_cost_usd": 0.0, "overage_rate_usd_per_tour": 4.0,
            "rate_limit_rpm": rate_limit_rpm, "plans": plans, "daily": daily,
            "activity": [],
        }

    return {
        **{k: (float(v) if hasattr(v, '__float__') and not isinstance(v, int) else v)
           for k, v in dict(row).items() if k != "billing_month"},
        "billing_month": str(row["billing_month"])[:7] if row["billing_month"] else None,
        "rate_limit_rpm": rate_limit_rpm,
        "plans": plans,
        "daily": daily,
        "activity": [
            {
                "id": str(a["id"]),
                "created_at": a["created_at"].isoformat(),
                "status": a["status"],
                "edit_source": a["edit_source"],
                "tour_name": a["aa_name"],
                "country": a["country"],
            }
            for a in activity
        ],
    }


# ── P3-S4: Trigger Rewrite ────────────────────────────────────────────────────


class RewriteRequest(_BM):
    rewrite_language: str = "en-US"
    seo_mode: str = "standard"
    custom_notes: Optional[str] = None


@router.post("/pool/{published_tour_id}/rewrite")
async def trigger_rewrite(
    published_tour_id: str,
    body: RewriteRequest,
    request: Request,
    tenant=Depends(get_tenant),
):
    """Trigger tenant rewrite of a published tour.
    Creates tenant_tour_versions record (status=pending) and calls pipeline.
    """
    tenant_id = tenant["sub"]
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        # AA-489/AA-640 — count this rewrite against the monthly plan quota. Over-quota rewrites
        # are allowed and billed as overage (trial phase) — see the helper's comment block.
        await _check_and_consume_rewrite_quota(conn, tenant_id)

        # Check published tour exists
        pt = await conn.fetchrow("""
            SELECT pt.id, pt.tour_id, pt.aa_name, pt.aa_subtitle,
                   pt.aa_summary, pt.aa_description, pt.aa_highlights,
                   pt.aa_itineraries, pt.seo_title, pt.seo_meta,
                   pt.seo_keywords_used,
                   rt.country, rt.duration
            FROM gold_aa_internal.published_tours pt
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE pt.id = $1::uuid
        """, published_tour_id)
        if not pt:
            raise HTTPException(status_code=404, detail="Tour not found in pool")

        # Get next version number
        next_ver = await conn.fetchval("""
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM gold_aa_internal.tenant_tour_versions
            WHERE tenant_id = $1::uuid AND published_tour_id = $2::uuid
        """, tenant_id, published_tour_id)

        # Create pending version record
        import json as _json
        version_id = await conn.fetchval("""
            INSERT INTO gold_aa_internal.tenant_tour_versions
                (tenant_id, published_tour_id, version_number,
                 rewritten_content, status, edit_source,
                 rewrite_language, seo_mode)
            VALUES ($1::uuid, $2::uuid, $3,
                    $4::jsonb, 'pending', 'ai_generated',
                    $5, $6)
            RETURNING id
        """,
            tenant_id, published_tour_id, next_ver,  # noqa: E128
            _json.dumps({  # noqa: E128
                "name": pt["aa_name"], "summary": pt["aa_summary"],
                "status": "generating",
            }),
            body.rewrite_language, body.seo_mode)

        # AA-559 — semantic tenant-activity log, same connection as the pending-version INSERT
        # above (T1 Browse Pool "rewrite" action).
        await write_audit_log(
            conn, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}",
            action=TenantAuditAction.TOUR_REWRITE_TRIGGERED, resource_type="tenant_tour_version",
            resource_id=str(version_id),
            details={
                "published_tour_id": published_tour_id, "version_number": next_ver,
                "tour_name": pt["aa_name"], "rewrite_language": body.rewrite_language,
            },
        )

    # AA-652 — the rewrite (SEO lookup, _rewrite_tour, T3 QA gate, save, live progress) runs as
    # a durable `t2_rewrite` job (services/jobs/t2_rewrite_job.py). Before, it was an in-process
    # asyncio task: a deploy/restart killed it and the version stayed 'pending' ("Writing…")
    # forever. The idempotency key ties one job to one version.
    from services.jobs.t2_rewrite_job import KIND as _T2_KIND
    from shared.jobs.registry import enqueue as _enqueue_job
    job_id, _ = await _enqueue_job(
        pool, _T2_KIND,
        {"version_id": str(version_id), "tenant_id": str(tenant_id),
         "published_tour_id": str(published_tour_id), "rewrite_language": body.rewrite_language},
        idempotency_key=f"{_T2_KIND}:{version_id}", created_by=f"tenant:{tenant_id}",
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE gold_aa_internal.tenant_tour_versions SET job_id = $2::uuid WHERE id = $1::uuid",
            version_id, job_id)

    return {
        "version_id": str(version_id),
        "published_tour_id": published_tour_id,
        "version_number": next_ver,
        "status": "pending",
        "job_id": job_id,
        "message": "Rewrite started — check My Catalog in ~30 seconds for results",
    }


# ── P3-S4: My Versions (Tenant Catalog) ──────────────────────────────────────

@router.get("/my-versions")
async def list_my_versions(
    request: Request,
    tenant=Depends(get_tenant),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
):
    """List tenant's own rewritten versions."""
    tenant_id = tenant["sub"]
    pool = request.app.state.pool
    offset = (page - 1) * page_size

    conditions = ["ttv.tenant_id = $1::uuid"]
    params: list = [tenant_id]

    if status:
        params.append(status)
        conditions.append(f"ttv.status = ${len(params)}")
    if country:
        params.append(country)
        conditions.append(f"LOWER(rt.country) = LOWER(${len(params)})")

    where = "WHERE " + " AND ".join(conditions)

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"""
            SELECT COUNT(*)
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            {where}
        """, *params)

        params_paged = params + [page_size, offset]
        rows = await conn.fetch(f"""
            SELECT ttv.id, ttv.version_number, ttv.status, ttv.quality_score,
                   ttv.edit_source, ttv.rewrite_language, ttv.created_at,
                   ttv.rewritten_content, ttv.qa_auto_passed,
                   pt.id AS published_tour_id, pt.tour_id, pt.aa_name, pt.quality_score AS aa_quality,
                   rt.country, rt.duration,
                   -- AA-652 — the rewrite job's status (queued/running/succeeded/failed/...),
                   -- NULL for manual edits and pre-AA-652 rows. No job error text (tenant-safe).
                   j.status AS job_status
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            LEFT JOIN shared.job j ON j.id = ttv.job_id
            {where}
            ORDER BY ttv.created_at DESC
            LIMIT ${len(params)+1} OFFSET ${len(params)+2}
        """, *params_paged)

    return {
        "data": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size,
                       "total": total, "pages": -(-total // page_size)},
    }


@router.post("/versions/{version_id}/retry")
async def retry_rewrite(version_id: str, request: Request, tenant=Depends(get_tenant)):
    """AA-652 — re-run a rewrite whose job failed (the portal's Retry button). Re-queues the same
    job with fresh attempts; no second quota charge (the rewrite was already counted)."""
    from shared.jobs import queue as job_queue

    tenant_id = tenant["sub"]
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, status, job_id FROM gold_aa_internal.tenant_tour_versions
            WHERE id = $1::uuid AND tenant_id = $2::uuid
        """, version_id, tenant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")
    if row["status"] != "failed" or row["job_id"] is None:
        raise HTTPException(status_code=409, detail="Only a failed AI rewrite can be retried")
    if not await job_queue.retry(pool, str(row["job_id"])):
        raise HTTPException(status_code=409, detail="This rewrite is already running")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE gold_aa_internal.tenant_tour_versions SET status = 'pending' "
            "WHERE id = $1::uuid AND status = 'failed'", version_id)
        await write_audit_log(
            conn, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}",
            action=TenantAuditAction.TOUR_REWRITE_TRIGGERED, resource_type="tenant_tour_version",
            resource_id=str(version_id), details={"retry": True, "job_id": str(row["job_id"])},
        )
    return {"version_id": version_id, "status": "pending", "job_id": str(row["job_id"])}


@router.get("/{tour_id}/full")
async def get_tour_full(
    tour_id: str,
    request: Request,
    x_admin_secret: Optional[str] = Header(None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
):
    """Full before/after data for catalog review panel."""
    import os
    ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
    is_admin = bool(ADMIN_SECRET and x_admin_secret == ADMIN_SECRET)
    if is_admin:
        tenant_id = "00000000-0000-0000-0000-000000000001"
    elif credentials:
        try:
            payload = verify_jwt(credentials.credentials)
            tenant_id = payload["sub"]
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
    else:
        raise HTTPException(status_code=401, detail="Not authenticated")

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        if is_admin:
            # Admin review path reads the shared aa_internal reference pool directly
            # (published_tours.tenant_id sentinel) — unchanged, not a per-tenant lookup.
            pt = await conn.fetchrow("""
                SELECT * FROM gold_aa_internal.published_tours
                WHERE id = $1::uuid AND tenant_id = $2::uuid
            """, tour_id, tenant_id)
        else:
            # Real tenant JWT: scope through tenant_tour_versions (AA-582 fix), same
            # pattern as v1_marketplace.py — published_tours.tenant_id is always the
            # aa_internal sentinel and was never a valid per-tenant filter.
            pt = await conn.fetchrow("""
                SELECT pt.* FROM gold_aa_internal.published_tours pt
                JOIN gold_aa_internal.tenant_tour_versions ttv
                    ON ttv.published_tour_id = pt.id
                WHERE pt.id = $1::uuid AND ttv.tenant_id = $2::uuid
                LIMIT 1
            """, tour_id, tenant_id)
        if not pt:
            raise HTTPException(status_code=404, detail="Tour not found")

        raw = await conn.fetchrow("""
            SELECT * FROM silver_aa_internal.raw_tours
            WHERE tour_id = $1::uuid
        """, pt["tour_id"])

        gen = await conn.fetchrow("""
            SELECT * FROM silver_aa_internal.generated_content
            WHERE tour_id = $1::uuid
            ORDER BY version_num DESC LIMIT 1
        """, pt["tour_id"])

        qs = await conn.fetchrow("""
            SELECT * FROM silver_aa_internal.quality_scores
            WHERE generated_content_id = $1::uuid
            ORDER BY evaluated_at DESC LIMIT 1
        """, gen["id"] if gen else None
        ) if gen else None

        sc = await conn.fetchrow("""
            SELECT top_keywords, keyword_search, keyword_ideas, fetched_at
            FROM silver_aa_internal.seo_context
            WHERE tour_id = $1::uuid
            ORDER BY fetched_at DESC LIMIT 1
        """, pt["tour_id"])

    def safe(row):
        from uuid import UUID
        from decimal import Decimal
        if not row: return {}
        d = dict(row)
        for k, v in d.items():
            if isinstance(v, UUID):
                d[k] = str(v)
            elif isinstance(v, Decimal):
                d[k] = float(v)
            elif hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        return d

    return {
        "published": safe(pt),
        "raw": safe(raw),
        "generated": safe(gen),
        "quality": safe(qs),
        "seo": safe(sc),
    }


class TourEditRequest(_BM):
    field: str
    value: str
    approved_by: Optional[str] = "content_team"


@router.get("/{tour_id}")
async def get_tour(
    tour_id: str,
    request: Request,
    tenant=Depends(get_tenant),
):
    tenant_id = tenant["sub"]
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        # AA-582 fix: scope through tenant_tour_versions (v1_marketplace.py pattern) —
        # published_tours.tenant_id is always the aa_internal sentinel, never per-tenant.
        row = await conn.fetchrow("""
            SELECT pt.* FROM gold_aa_internal.published_tours pt
            JOIN gold_aa_internal.tenant_tour_versions ttv
                ON ttv.published_tour_id = pt.id
            WHERE pt.id = $1::uuid AND ttv.tenant_id = $2::uuid
            LIMIT 1
        """, tour_id, tenant_id)

    if not row:
        raise HTTPException(status_code=404, detail="Tour not found")

    return dict(row)


# ── P3-S4: Version Detail + Approve/Reject/Edit ───────────────────────────────

@router.get("/versions/{version_id}")
async def get_version(
    version_id: str,
    request: Request,
    tenant=Depends(get_tenant),
):
    """Get version detail with AA original for before/after diff."""
    tenant_id = tenant["sub"]
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT ttv.*, pt.tour_id, pt.aa_name, pt.aa_subtitle, pt.aa_summary,
                   pt.aa_description, pt.aa_highlights, pt.aa_itineraries,
                   pt.seo_title AS aa_seo_title, pt.seo_meta AS aa_seo_meta,
                   pt.quality_score AS aa_quality_score,
                   rt.country, rt.duration, rt.price_raw,
                   rt.inclusions, rt.exclusions,
                   j.status AS job_status  -- AA-652
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            LEFT JOIN shared.job j ON j.id = ttv.job_id
            WHERE ttv.id = $1::uuid AND ttv.tenant_id = $2::uuid
        """, version_id, tenant_id)

        if not row:
            raise HTTPException(status_code=404, detail="Version not found")

        # Version history
        history = await conn.fetch("""
            SELECT id, version_number, status, edit_source, quality_score, created_at
            FROM gold_aa_internal.tenant_tour_versions
            WHERE tenant_id = $1::uuid AND published_tour_id = $2::uuid
            ORDER BY version_number ASC
        """, tenant_id, row["published_tour_id"])

    return {
        **dict(row),
        "version_history": [dict(h) for h in history],
    }


# AA-566 Phần B.2 — tenant-facing DOCX export, one version at a time (matches Admin Master
# Content's own single-version DOCX convention, admin_pipeline.py::export_tour_version_docx()
# — same python-docx pattern, same off-white-background/heading-color helpers, deliberately NOT
# a shared function since the two read completely different tables (silver_aa_internal.
# generated_content there vs gold_aa_internal.tenant_tour_versions here) and the tenant version
# has no SEO/score/judge fields to render at all (AA-566 Phần B.5 — those are being dropped from
# the tenant-facing drawer for the same reason: internal jargon, not tenant content).
@router.get("/versions/{version_id}/export-docx")
async def export_version_docx(
    version_id: str,
    request: Request,
    tenant=Depends(get_tenant),
):
    import json as _json
    tenant_id = tenant["sub"]
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT ttv.version_number, ttv.rewritten_content, ttv.rewrite_language,
                   pt.tour_id, pt.aa_name, pt.aa_subtitle, pt.aa_summary, pt.aa_highlights,
                   pt.aa_itineraries,
                   rt.country, rt.duration, rt.inclusions, rt.exclusions
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE ttv.id = $1::uuid AND ttv.tenant_id = $2::uuid
        """, version_id, tenant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")

    # Same rewritten_content-overrides-aa_* merge CatalogTab.tsx's own loadDetail() does —
    # a tenant's manual edit lives in rewritten_content, not the flat aa_* columns.
    rc = {}
    if row["rewritten_content"]:
        try:
            raw_rc = row["rewritten_content"]
            rc = _json.loads(raw_rc) if isinstance(raw_rc, str) else dict(raw_rc)
        except Exception:
            rc = {}
    name = rc.get("name") or row["aa_name"] or "(untitled)"
    subtitle = rc.get("subtitle") or row["aa_subtitle"] or ""
    summary = rc.get("summary") or row["aa_summary"] or ""
    highlights = rc.get("highlights")
    if not isinstance(highlights, list):
        try:
            highlights = _json.loads(row["aa_highlights"]) if row["aa_highlights"] else []
        except Exception:
            highlights = []
    itineraries = rc.get("itineraries") or row["aa_itineraries"] or ""

    import io
    from docx import Document
    from docx.shared import Pt, RGBColor
    from fastapi.responses import StreamingResponse

    ORANGE = RGBColor(0xDB, 0x96, 0x28)
    BLACKBLUE = RGBColor(0x1F, 0x29, 0x33)
    GRAY = RGBColor(0x33, 0x36, 0x3D)

    doc = Document()

    def _section(text):
        p = doc.add_paragraph()
        bar = p.add_run("▌ ")
        bar.bold = True
        bar.font.color.rgb = ORANGE
        r = p.add_run(text)
        r.bold = True
        r.font.size = Pt(13)
        r.font.color.rgb = BLACKBLUE

    def _kv(label, value):
        p = doc.add_paragraph()
        lr = p.add_run(f"{label}: ")
        lr.bold = True
        lr.font.size = Pt(10.5)
        lr.font.color.rgb = BLACKBLUE
        vr = p.add_run("" if value is None else str(value))
        vr.font.size = Pt(10.5)
        vr.font.color.rgb = GRAY

    def _body(text):
        p = doc.add_paragraph()
        for i, line in enumerate(str(text or "").split("\n")):
            if i > 0:
                p.add_run().add_break()
            r = p.add_run(line)
            r.font.size = Pt(10.5)
            r.font.color.rgb = GRAY

    title = doc.add_paragraph()
    tr = title.add_run(name)
    tr.bold = True
    tr.font.size = Pt(20)
    tr.font.color.rgb = BLACKBLUE
    if subtitle:
        sub = doc.add_paragraph()
        sr = sub.add_run(subtitle)
        sr.italic = True
        sr.font.size = Pt(12)
        sr.font.color.rgb = ORANGE
    meta = doc.add_paragraph()
    mr = meta.add_run(f"Version {row['version_number']}   ·   {row['rewrite_language']}")
    mr.font.size = Pt(10)
    mr.font.color.rgb = ORANGE

    _section("Trip Details")
    _kv("Country", row["country"] or "—")
    _kv("Duration", row["duration"] or "—")

    _section("Summary")
    _body(summary)

    if highlights:
        _section("Highlights")
        for h in highlights:
            doc.add_paragraph(str(h), style="List Bullet")

    if itineraries:
        _section("Itinerary")
        _body(itineraries)

    if row["inclusions"]:
        _section("Inclusions")
        _body(row["inclusions"])

    if row["exclusions"]:
        _section("Exclusions")
        _body(row["exclusions"])

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    short = str(row["tour_id"]).split("-")[0]
    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename=tour_{short}_v{row['version_number']}.docx"},
    )


class VersionActionRequest(_BM):
    action: str  # approve | reject | edit
    edited_content: Optional[dict] = None
    edited_by: Optional[str] = None


@router.patch("/versions/{version_id}")
async def update_version(
    version_id: str,
    body: VersionActionRequest,
    request: Request,
    tenant=Depends(get_tenant),
):
    """Approve, reject, or inline-edit a tenant tour version."""
    import json as _json
    tenant_id = tenant["sub"]
    pool = request.app.state.pool

    if body.action not in ("approve", "reject", "edit"):
        raise HTTPException(status_code=400, detail="action must be approve|reject|edit")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, published_tour_id, version_number, rewritten_content
            FROM gold_aa_internal.tenant_tour_versions
            WHERE id = $1::uuid AND tenant_id = $2::uuid
        """, version_id, tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="Version not found")

        if body.action == "edit" and body.edited_content:
            # Inline edit → create new version
            next_ver = await conn.fetchval("""
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM gold_aa_internal.tenant_tour_versions
                WHERE tenant_id = $1::uuid AND published_tour_id = $2::uuid
            """, tenant_id, row["published_tour_id"])

            new_id = await conn.fetchval("""
                INSERT INTO gold_aa_internal.tenant_tour_versions
                    (tenant_id, published_tour_id, version_number,
                     parent_version_id, rewritten_content, status,
                     edit_source, edited_at, edited_by_user)
                VALUES ($1::uuid, $2::uuid, $3,
                        $4::uuid, $5::jsonb, 'pending',
                        'tenant_edit', NOW(), $6)
                RETURNING id
            """,
            tenant_id, row["published_tour_id"], next_ver,  # noqa: E128
            version_id,  # noqa: E128 E122
            _json.dumps(body.edited_content), body.edited_by or "tenant")  # noqa: E128 E122
            return {
                "status": "edited",
                "new_version_id": str(new_id),
                "version_number": next_ver,
            }

        else:
            new_status = "approved" if body.action == "approve" else "rejected"
            await conn.execute("""
                UPDATE gold_aa_internal.tenant_tour_versions
                SET status = $1, edited_at = NOW()
                WHERE id = $2::uuid AND tenant_id = $3::uuid
            """, new_status, version_id, tenant_id)
            return {"status": new_status, "version_id": version_id}
# P3 complete Tue May  5 11:42:07 +07 2026


# ── P4: Full review endpoint — before/after + inline edit + approve ───────────
@router.patch("/{tour_id}/approve")
async def approve_tour_edit(
    tour_id: str,
    body: TourEditRequest,
    request: Request,
    tenant=Depends(get_tenant),
):
    """Inline edit + save a field on published_tour."""
    tenant_id = tenant["sub"]
    pool = request.app.state.pool

    ALLOWED = {
        "aa_name", "aa_subtitle", "aa_summary", "aa_description",
        "aa_highlights", "aa_itineraries", "mobile_card_text",
        "seo_title", "seo_meta",
    }
    if body.field not in ALLOWED:
        raise HTTPException(status_code=400, detail=f"Field '{body.field}' not editable")

    async with pool.acquire() as conn:
        await conn.execute(f"""
            UPDATE gold_aa_internal.published_tours
            SET {body.field} = $1, approved_by = $2
            WHERE id = $3::uuid AND tenant_id = $4::uuid
        """, body.value, body.approved_by, tour_id, tenant_id)

    return {"ok": True, "field": body.field}
