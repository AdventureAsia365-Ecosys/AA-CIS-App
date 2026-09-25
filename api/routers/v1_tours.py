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

# AA-425: strong refs to the fire-and-forget rewrite (T2 + T3 + T5) background task —
# asyncio only keeps a weak ref to a bare create_task() result, so an unreferenced task can be
# GC'd mid-flight (same class of bug AA-223 already found/fixed for admin_pipeline.py's
# run-tour-async path — see that file's own comment on this pattern). T3's repair loop adds up
# to 2 more LLM round trips and T5 adds another on top of T2's own call, so this task now runs
# meaningfully longer than it did pre-AA-425 — worth closing this gap while touching this code.
_background_tasks: set = set()


def get_tenant(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        return verify_jwt(credentials.credentials)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_pool(request: Request):
    return request.app.state.pool


async def _run_research_only(tenant_id: str, pool) -> None:
    """AA-545 — Segment-matching/ranking/route-detection MOVED to A3
    (`services/export/handler.py::_run_a3_atomize_background()`), platform-wide, per Q2's locked
    decision (see docs/implementation-notes/AA-545.md Decision 3 and the AA-545 Linear issue).
    Only `run_segment_research()` (the search-demand PURCHASE decision — explicitly OUTSIDE
    AA-545's 4-layer scope: Segment/Score/Route/Hub, not this module) stays triggered here,
    per-tenant-rewrite, exactly as before AA-545 — its cost profile (real DataForSEO spend) and
    per-tenant `target_market` scoping were a deliberate, disclosed non-change, not an oversight.
    """
    try:
        from services.acp_contract.segment_research import run_segment_research
        from shared.services.tenant_config_service import TenantConfigService

        async with pool.acquire() as conn:
            cfg = await TenantConfigService(conn).get_seo_config(tenant_id)

        research_result = await run_segment_research(tenant_id, cfg.target_market, pool)
        logger.info("t5_segment_research_done", tenant_id=tenant_id, result=research_result)
    except Exception:
        logger.warning("t5_segment_research_failed", tenant_id=tenant_id, exc_info=True)


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
# Business decisions (conservative defaults, per this chain's own build prompt — not
# separately confirmed live by Nghiệp, flagged for review, not treated as silently final):
#   * Limit = PLAN_LIMITS[plan_tier].tours_per_month as-is, no new number invented.
#   * Reset = calendar month, not rolling 30d.
#   * Hard-block (429) over quota — matches this issue's stated purpose, LLM cost control.


def _current_year_month_and_reset() -> tuple:
    """(year_month 'YYYY-MM' string, first-of-next-month date) in UTC."""
    import datetime as _dt_quota
    now_utc = _dt_quota.datetime.now(_dt_quota.timezone.utc)
    next_month = (now_utc.replace(day=1) + _dt_quota.timedelta(days=32)).replace(day=1)
    return now_utc.strftime("%Y-%m"), next_month


async def _get_tenant_plan_limit(conn, tenant_id: str) -> tuple:
    """(plan_tier str, tours_per_month int) — read live from shared.tenants, not the JWT's
    own plan_tier claim (same rationale AA-432 established for rate_limit_rpm/is_active: a
    plan change shouldn't take up to 24h, the JWT's TTL, to take effect)."""
    plan = await conn.fetchval(
        "SELECT plan_tier FROM shared.tenants WHERE tenant_id = $1::uuid", tenant_id
    )
    limit = PLAN_LIMITS.get(str(plan), PLAN_LIMITS["starter"])["tours_per_month"]
    return str(plan), limit


async def _check_and_consume_rewrite_quota(conn, tenant_id: str) -> None:
    """Atomically increments this month's rewrite count and raises 429 if it now exceeds the
    tenant's plan limit. Increment-then-check (same order rate_limit_middleware's
    redis.incr()-then-compare already uses) — a request that pushes the count past the limit
    still counts, consistent with "quota consumed by requesting"."""
    _, limit = await _get_tenant_plan_limit(conn, tenant_id)
    year_month, next_month = _current_year_month_and_reset()
    used = await conn.fetchval("""
        INSERT INTO shared.tenant_rewrite_usage (tenant_id, year_month, rewrite_count)
        VALUES ($1::uuid, $2, 1)
        ON CONFLICT (tenant_id, year_month)
        DO UPDATE SET rewrite_count = shared.tenant_rewrite_usage.rewrite_count + 1,
                      updated_at = NOW()
        RETURNING rewrite_count
    """, tenant_id, year_month)
    if used > limit:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly rewrite quota exceeded ({used - 1}/{limit} used this month, "
                   f"resets {next_month.strftime('%Y-%m-%d')})",
        )


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
            ORDER BY price_usd_monthly NULLS LAST, tours_quota_monthly
        """)
    plans = [
        {
            "plan_name": p["plan_name"],
            "tours_quota_monthly": p["tours_quota_monthly"],
            "api_calls_quota_monthly": p["api_calls_quota_monthly"],
            "price_usd_monthly": float(p["price_usd_monthly"]) if p["price_usd_monthly"] is not None else None,
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
            "rate_limit_rpm": rate_limit_rpm, "plans": plans,
            "activity": [],
        }

    return {
        **{k: (float(v) if hasattr(v, '__float__') and not isinstance(v, int) else v)
           for k, v in dict(row).items() if k != "billing_month"},
        "billing_month": str(row["billing_month"])[:7] if row["billing_month"] else None,
        "rate_limit_rpm": rate_limit_rpm,
        "plans": plans,
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
        # AA-489 — real monthly rewrite quota, enforced here for the first time. See the
        # helper's own docstring/comment block above (_check_and_consume_rewrite_quota) for
        # the full rationale; raises 429 before any tour lookup or LLM work if this request
        # would push the tenant over their plan's tours_per_month for the current month.
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

    # P3-S9 fix: actually call LLM rewrite
    import asyncio as _asyncio
    import sys as _sys
    _sys.path.insert(0, '/app')
    from api.routers.v1_pipeline import _rewrite_tour as _do_rewrite

    # Build tour dict from published tour
    tour_dict = {
        "name":        pt["aa_name"],
        "subtitle":    pt.get("aa_subtitle") or "",
        "summary":     pt.get("aa_summary") or "",
        "description": pt.get("aa_description") or "",
        "highlights":  pt.get("aa_highlights") or "",
        "itineraries": pt.get("aa_itineraries") or "",
        "seo_title":   pt.get("seo_title") or "",    # fixed: was "aa_seo_title"
        "seo_meta":    pt.get("seo_meta") or "",     # fixed: was "aa_seo_meta"
        "country":     pt.get("country") or "",       # now from raw_tours JOIN
        "duration":    pt.get("duration") or "",      # now from raw_tours JOIN
    }

    # Fetch brand rules for this tenant
    # AA-425 fix: `br_row = await pool.acquire().__aenter__()` used to sit right before the
    # `async with pool.acquire() as _conn2` below -- it acquired a SECOND connection from the
    # pool and called __aenter__() directly without ever pairing it with __aexit__(), leaking
    # one pooled connection on every single tenant rewrite (br_row itself was never even used
    # afterward -- dead code). Pool is min_size=2/max_size=10 (api/main.py) -- found live during
    # AA-425 verification: after ~8 real rewrite calls in one session the pool was exhausted
    # enough that THIS query started timing out, silently falling into the except below
    # (brand_rules = {} minus system_prompt/style_guide/forbidden_words) -- the LLM then had no
    # brand voice guidance at all and reliably drifted into generic marketing adjectives that
    # happen to be on graph.py's own forbidden-word list, escalating every rewrite to
    # review_queue. Removed the leaked acquire; only the real query below remains.
    brand_rules = {}
    try:
        async with pool.acquire() as _conn2:
            # AA-612a: T2 (tenant rewrite) shares build_graph with admin S1, whose judge
            # brand-fit gate (judge_node.has_brand_signals) keys off core_idea /
            # customer_mindset / voice_examples. This SELECT used to omit those brand-diff
            # columns, so brand_rules never carried them → the graph state's brand_* fields were
            # always empty → judge_skipped(no_brand_profile) even for a tenant WITH a real brand
            # (the exact case that SHOULD be judged on brand-fit). Fetch the same brand-diff
            # columns admin_pipeline._BRAND_RULE_COLS does so a real tenant brand is judged.
            _br = await _conn2.fetchrow("""
                SELECT system_prompt, style_guide, forbidden_words,
                       core_idea, customer_segment, customer_mindset, voice_examples, good_examples
                FROM shared.tenant_brand_rules
                WHERE tenant_id = $1::uuid AND is_active = true
                ORDER BY version DESC LIMIT 1
            """, tenant_id)
        if _br:
            # AA-425 fix (found via hash-diff forensic comparison against a direct-call harness
            # using the exact same tenant/tour): asyncpg has no jsonb codec registered on this
            # app's connections (same gap AA-314 already found/fixed elsewhere, api/routers/
            # v1_tours.py's own src_highlights handling) -- forbidden_words arrives as a raw
            # JSON-encoded STRING, not a parsed list. `list(_br["forbidden_words"] or [])` was
            # calling list() on that STRING, splitting it into individual characters
            # (['[', '"', 'l', 'u', 'x', ...]) instead of parsing it -- every tenant rewrite's
            # system prompt carried a garbled "FORBIDDEN WORDS: [, ", l, u, x, ..." instruction
            # instead of the tenant's real word list. Confirmed via SHA-256 hash comparison: the
            # user_prompt and tour_dict hashes matched byte-for-byte between this endpoint and a
            # direct _rewrite_tour() call using identical inputs, but brand_rules.forbidden_words
            # diverged at exactly this line.
            import json as _json2
            _fw_raw = _br["forbidden_words"]
            if isinstance(_fw_raw, str):
                _fw_raw = _json2.loads(_fw_raw) if _fw_raw else []
            # AA-612a: voice_examples has the same asyncpg no-jsonb-codec gotcha as
            # forbidden_words above — it arrives as a JSON-encoded string, so parse it before
            # list() (mirrors admin_pipeline._execute_run_tour's own handling).
            _voice_raw = _br["voice_examples"]
            if isinstance(_voice_raw, str):
                _voice_raw = _json2.loads(_voice_raw) if _voice_raw else []
            brand_rules = {
                "system_prompt":    _br["system_prompt"] or "",
                "style_guide":      _br["style_guide"] or "",
                "forbidden_words":  list(_fw_raw or []),
                "rewrite_language": body.rewrite_language,
                # AA-612a: brand-diff fields — the keys _rewrite_tour maps into the graph's
                # brand_* state (v1_pipeline.py), which the judge brand-fit gate reads.
                "core_idea":        _br["core_idea"] or "",
                "customer_segment": _br["customer_segment"] or "",
                "customer_mindset": _br["customer_mindset"] or "",
                "voice_examples":   list(_voice_raw or []),
                "good_examples":    _br["good_examples"] or "",
            }
    except Exception:
        brand_rules = {"rewrite_language": body.rewrite_language}

    # Run LLM rewrite in background (don't block response)
    async def _do_rewrite_and_save():
        # AA-445-02 — DFS mở rộng T2 (docs/claude_audit/AA-445-01-dfs-distinctiveness-step0-audit
        # .md Q2a): T1/T2 never called process_seo(), so `seo` reached the shared
        # _rewrite_tour()/validate_node graph as {} — structurally, not by a tier flag. Mirrors
        # admin_pipeline.py's A1 pattern (lines ~458-488): reuse an existing seo_context row for
        # this tour_id if one exists (free — AA-439-05 confirmed most tenant-rewritten tours were
        # originally A1-generated and already have one), else call process_seo() on a miss
        # (~$0.18/tour, AA-439-05 §7). Best-effort — a SEO fetch failure must not block the
        # rewrite itself, same as A1's own try/except around this block.
        seo_data: dict = {}
        try:
            async with pool.acquire() as _conn_seo:
                _existing = await _conn_seo.fetchrow("""
                    SELECT top_keywords, keyword_ideas, people_also_ask
                    FROM silver_aa_internal.seo_context
                    WHERE tour_id = $1::uuid
                    ORDER BY fetched_at DESC LIMIT 1
                """, pt["tour_id"])
            if _existing:
                import json as _json_seo
                _tk = _existing["top_keywords"]
                seo_data = {
                    "top_keywords": (_json_seo.loads(_tk) if isinstance(_tk, str) else _tk) or [],
                }
                seo_data["keywords"] = {"top_keywords": seo_data["top_keywords"]}
            else:
                from services.seo_intelligence.handler import process_seo
                from services.seo_intelligence.seed_builder import build_seed
                seed = build_seed(tour_dict.get("country"), None, tour_dict.get("name")) or tour_dict.get("name", "")
                if seed:
                    _seo_result = await process_seo(
                        tour_id=pt["tour_id"], destination=seed, seed=seed,
                        tenant_id=tenant_id, seo_mode="dataforseo",
                    )
                    seo_data = _seo_result.get("data", {})
                    if "keywords" in seo_data and "top_keywords" not in seo_data:
                        seo_data["top_keywords"] = seo_data["keywords"].get("top_keywords", [])
                    elif "top_keywords" in seo_data and "keywords" not in seo_data:
                        seo_data["keywords"] = {"top_keywords": seo_data["top_keywords"]}
                    seo_data.setdefault("top_keywords", [])
        except Exception as _seo_err:
            import structlog as _sl_seo
            _sl_seo.get_logger().warning("t2_seo_step_failed", tour_id=pt["tour_id"], error=str(_seo_err))

        try:
            result = await _do_rewrite(
                tour_dict, idx=0, total=1,
                brand_rules=brand_rules,
                seo=seo_data,
                is_tenant_rewrite=True,  # skips name-match check in validate_node
                tenant_id=tenant_id,             # AA-620: log this tenant in llm_call_log
                generate_stage="t2_generate",    # AA-620: tenant writer stage (admin-tunable)
            )
            if result.get("status") == "success" and result.get("generated"):
                # AA-425 T3 — QA gate (grounding + structural), self-repair up to
                # TENANT_QA_MAX_REPAIRS rounds. source_texts is T2's OWN input (the
                # pre-rewrite published_tours content) — the grounding baseline a
                # tenant rewrite must not introduce new numbers/measurements beyond.
                from services.acp_produce.tenant_pipeline import run_t3_qa_gate
                source_texts = [str(v) for v in tour_dict.values() if v]
                qa = await run_t3_qa_gate(
                    tour_dict, source_texts, result, brand_rules,
                    seo_data=seo_data,  # AA-445-02 — repair-round rewrites also carry seo_data
                    tenant_id=tenant_id,  # AA-620 — repair-round logs t2_generate + this tenant
                )
                result = qa["result"]  # possibly a later repair round's output

                import json as _j3
                gen = result["generated"]
                rewritten = {
                    "name":        gen.get("name", tour_dict["name"]),
                    "subtitle":    gen.get("subtitle", ""),
                    "summary":     gen.get("summary", ""),
                    "highlights":  gen.get("highlights", []),
                    "itineraries": gen.get("itineraries", tour_dict.get("itineraries", "")),
                    "seo_title":   gen.get("seo_title", ""),
                    "seo_meta":    gen.get("seo_meta", ""),
                    "trip_type":   gen.get("trip_type", ""),
                    "status":      "done",
                }
                rewrite_score = float(result.get("quality_score") or 0)
                # AA-436: status is now purely score-based, same formula for a real T3 pass
                # and an auto-pass — T3 no longer forces needs_review on its own (see
                # qa_auto_passed below for the separate signal that a QA-gate failure
                # happened). ADR-2026-038 §0.1 (amend §10.3): escalate-and-stop broke the
                # single-job T2->T3->T5 chain and made the tenant fix wording themselves —
                # both rejected 22/08.
                if rewrite_score >= 7.0:
                    new_status = "ai_generated"   # ready for tenant to review
                elif rewrite_score > 0:
                    new_status = "needs_review"   # LLM finished but low quality
                else:
                    new_status = "needs_review"   # hitl / score=0 — needs human
                # Never write 0.0 — fall back to source published_tours quality_score
                async with pool.acquire() as _conn_qs:
                    source_score = await _conn_qs.fetchval(
                        "SELECT quality_score FROM gold_aa_internal.published_tours WHERE id = $1::uuid",
                        published_tour_id
                    )
                final_score = rewrite_score if rewrite_score else float(source_score or 0)
                # qa_status keeps its migration-107 meaning unchanged (the real QA verdict,
                # 'escalated' still means the gate did NOT actually clear) — qa_auto_passed
                # (migration 109) is the new, separate tenant-facing badge flag: true means
                # this version reached the pool despite qa_status='escalated'.
                qa_status = "passed" if qa["passed"] else "escalated"
                qa_auto_passed = not qa["passed"]
                async with pool.acquire() as _conn3:
                    await _conn3.execute("""
                        UPDATE gold_aa_internal.tenant_tour_versions
                        SET rewritten_content = $1::jsonb,
                            status = $2,
                            quality_score = $3,
                            qa_status = $4,
                            qa_repair_count = $5,
                            qa_checked_at = now(),
                            qa_auto_passed = $6
                        WHERE id = $7::uuid
                    """,
                        _j3.dumps(rewritten), new_status, final_score,
                        qa_status, qa["attempts"], qa_auto_passed, version_id)
                import structlog as _sl2
                _sl2.get_logger().info("tenant_rewrite_done",
                    version_id=str(version_id), score=final_score, status=new_status,
                    qa_status=qa_status, qa_attempts=qa["attempts"],
                    qa_auto_passed=qa_auto_passed)

                if not qa["passed"]:
                    # AA-436: T3 no longer escalate-BLOCKS — still write the review_queue
                    # row exactly as AA-425 did (escalate_t3_failure() itself unchanged), so
                    # A4 (AA-437, separate issue) can see it — the tour still reaches the
                    # tenant's pool (T4) either way.
                    from services.acp_produce.tenant_pipeline import escalate_t3_failure
                    await escalate_t3_failure(
                        pool, tenant_id, pt["tour_id"], str(version_id),
                        qa["structural_issues"], qa["grounding_issues"],
                    )

                # AA-526 (04/09/2026 architecture decision) — atomize (T5) no longer runs on
                # tenant-rewritten content at all, here or via the (now-removed) standalone
                # POST /v1/tours/versions/{version_id}/atomize AA-469 Việc 1 introduced. Atoms
                # for this tour already exist (owner_scope='platform') by the time it's even
                # visible in Browse Pool — atomize now runs once, at A3 (services/export/
                # handler.py::process_export(), right after the tour is published), not
                # per-tenant, not on every rewrite. See services/acp_produce/tenant_pipeline.py's
                # own module docstring + docs/implementation-notes/AA-526.md.
                #
                # AA-545 — Segment-matching + ranking + route-detection (AA-509/510/515) ALSO
                # moved to A3 (`services/export/handler.py::_run_a3_atomize_background()`),
                # right after atomize itself, platform-wide — not per-tenant-rewrite anymore
                # (AA-526's own note above, kept for history, is now superseded: Segment/Route/
                # Ranking are the single global set A3 already computes once for everyone; see
                # docs/implementation-notes/AA-545.md). Only `run_segment_research()` (search-
                # demand purchase decision, explicitly out of AA-545's scope) still fires here,
                # per-tenant-rewrite, unchanged.
                import asyncio as _asyncio_ranking
                _ranking_task = _asyncio_ranking.create_task(_run_research_only(tenant_id, pool))
                _background_tasks.add(_ranking_task)
                _ranking_task.add_done_callback(_background_tasks.discard)
        except Exception as _e:
            import structlog as _sl
            _sl.get_logger().error("tenant_rewrite_failed", error=str(_e))
            # Mark as needs_review so polling detects completion even on error
            try:
                async with pool.acquire() as _conn_err:
                    await _conn_err.execute("""
                        UPDATE gold_aa_internal.tenant_tour_versions
                        SET status = 'needs_review'
                        WHERE id = $1::uuid AND status = 'pending'
                    """, version_id)
            except Exception:
                pass

    _rewrite_task = _asyncio.create_task(_do_rewrite_and_save())
    _background_tasks.add(_rewrite_task)
    _rewrite_task.add_done_callback(_background_tasks.discard)

    return {
        "version_id": str(version_id),
        "published_tour_id": published_tour_id,
        "version_number": next_ver,
        "status": "pending",
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
                   rt.country, rt.duration
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            {where}
            ORDER BY ttv.created_at DESC
            LIMIT ${len(params)+1} OFFSET ${len(params)+2}
        """, *params_paged)

    return {
        "data": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size,
                       "total": total, "pages": -(-total // page_size)},
    }


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
                   rt.inclusions, rt.exclusions
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
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
