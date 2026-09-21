# api/routers/admin.py
# P2-S5 — Multi-tenant onboarding + billing metrics
import hashlib
import io
import json
import os
import secrets
from datetime import datetime, timezone
from uuid import UUID
from typing import List, Optional
import asyncpg
import boto3
from fastapi import APIRouter, File, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from services.notifications import NotificationService, EventType
from shared.validators.prompt_sanitize import (
    sanitize_text, sanitize_list, MAX_LONG_FIELD_LEN, MAX_SHORT_FIELD_LEN, MAX_SYSTEM_PROMPT_LEN,
)
from services.acp_planning.tenant_config import (
    TenantNotFoundError,
    fetch_tenant_planning_config,
    save_tenant_planning_config,
)

router = APIRouter(prefix="/admin", tags=["admin"])
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

PLAN_LIMITS = {
    "starter":  {"rpm": 60,   "tours_per_month": 100},
    "growth":   {"rpm": 300,  "tours_per_month": 500},
    "business": {"rpm": 1000, "tours_per_month": 2000},
    "internal": {"rpm": 60,   "tours_per_month": 999999},
}

# ── Auth guard ────────────────────────────────────────────────────────────────


def verify_admin_secret(x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin secret not configured")
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")

# ── Models ────────────────────────────────────────────────────────────────────


class CreateTenantRequest(BaseModel):
    name: str
    slug: str
    plan_tier: str = "starter"
    # AA-384: caller-chosen posting cadence, no longer implied by plan_tier (was
    # POSTS_PER_WEEK_BY_PLAN_TIER, removed). Required, no default -- every tenant states its own
    # cadence at creation. 1-14 is the validation range confirmed in the AA-384 build task (a
    # generous ceiling, not a real technical limit).
    posts_per_week: int = Field(..., ge=1, le=14)


class CreateTenantResponse(BaseModel):
    tenant_id: str
    name: str
    slug: str
    plan_tier: str
    posts_per_week: int
    api_key: str
    rate_limit_rpm: int
    is_active: bool
    message: str


class GenerateKeyResponse(BaseModel):
    tenant_id: str
    tenant_name: str
    api_key: str
    message: str

# ── POST /admin/tenants — Create tenant ───────────────────────────────────────


@router.post("/tenants", response_model=CreateTenantResponse, summary="Create new tenant")
async def create_tenant(
    body: CreateTenantRequest,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """AA-473 (ADR-2026-038 §0.2): Gate A removed entirely -- a new tenant is `is_active=true`
    immediately on creation, same as AA-472 already did for Gate B. This function itself never
    touched silver_aa_internal.raw_tours (no code to remove here) -- that "tenant brings its own
    tours" assumption lives only in list_tenants()/get_tenant_details() below, the old ACP v1
    shape N1 deliberately does not extend: a tenant's tour/atom selection is GET /v1/marketplace
    (AA-444, api/routers/v1_marketplace.py), never rows the tenant uploads under its own
    tenant_id."""
    verify_admin_secret(x_admin_secret)

    if body.plan_tier not in PLAN_LIMITS:
        raise HTTPException(status_code=400, detail=f"Invalid plan_tier. Choose: {list(PLAN_LIMITS.keys())}")

    rpm = PLAN_LIMITS[body.plan_tier]["rpm"]
    plaintext = f"cis_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT tenant_id FROM shared.tenants WHERE slug = $1", body.slug
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"Slug '{body.slug}' already exists")

        async with conn.transaction():
            tenant_id = await conn.fetchval("""
                INSERT INTO shared.tenants
                    (name, slug, plan_tier, posts_per_week, api_key_hash, rate_limit_rpm, is_active)
                VALUES ($1, $2, $3::plan_tier_enum, $4, $5, $6, true)
                RETURNING tenant_id
            """, body.name, body.slug, body.plan_tier, body.posts_per_week, key_hash, rpm)

            # Quota ledger — default limits per plan
            plan_limits = PLAN_LIMITS.get(body.plan_tier, PLAN_LIMITS["starter"])
            await conn.execute("""
                INSERT INTO acp_shared.acp_quota_ledger
                    (tenant_id, s2_runs_limit, s3_runs_limit, s4_blogs_limit)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tenant_id) DO NOTHING
            """, tenant_id, 10, 10, 50)

            # Empty brand rules row — populated later via brand-brief upload
            # ON CONFLICT omitted: no unique constraint on (tenant_id, is_active);
            # duplicate guard relies on the slug uniqueness check above.
            has_rules = await conn.fetchval(
                "SELECT 1 FROM shared.tenant_brand_rules WHERE tenant_id = $1 LIMIT 1",
                tenant_id,
            )
            if not has_rules:
                # AA-309 [N1] live-verify fix (08/08/2026): shared.tenant_brand_rules.brand_name is
                # TEXT NOT NULL with no column default (confirmed live, information_schema) -- this
                # bare INSERT has always violated that constraint, discovered only by actually
                # running create_tenant() against the real dev DB (not visible from reading the code
                # or the schema in isolation). Pre-existing bug in AA-63's own code, not introduced
                # by N1 -- fixed here because N1 categorically depends on create_tenant() succeeding.
                # AA-471: brand_name must be the literal 'default' sentinel, not body.name -- every
                # reader (fetch_brand_rubric_text() in services/acp_produce/brand.py,
                # _resolve_brand_rule()'s no-brand_name branch in admin_pipeline.py, AA-198's whole
                # multi-brand convention since migration 044) looks up the tenant's primary brand via
                # `WHERE tenant_id = $1 AND brand_name = 'default'`. Seeding brand_name = body.name
                # (e.g. the tenant's own company name) meant this placeholder row could never be
                # found by any of those readers -- every tenant onboarded through this endpoint fell
                # through to the generic AA_BRAND_IDENTITY_PROMPT fallback even with a row present
                # and system_prompt non-empty. NOTE: prior to AA-383, upload_brand_brief()'s db.py
                # upsert never touched brand_name at all (INSERT omitted the column entirely, would
                # NOT-NULL-violate on a tenant with no seeded row here; UPDATE left it unchanged) --
                # AA-383 made both branches set brand_name = 'default' explicitly, so it's now
                # genuinely safe to say this seeded row is kept in sync by the real upload flow.
                await conn.execute(
                    "INSERT INTO shared.tenant_brand_rules (tenant_id, brand_name) VALUES ($1, $2)",
                    tenant_id, "default",
                )

            # Onboarding audit trail
            await conn.execute("""
                INSERT INTO acp_shared.audit_log
                    (tenant_id, actor, action, resource_type, resource_id, details)
                VALUES ($1, 'admin_api', 'agency.onboard', 'tenant', $2, $3::jsonb)
            """, str(tenant_id), str(tenant_id), json.dumps({
                "name": body.name, "plan_tier": body.plan_tier, "posts_per_week": body.posts_per_week,
            }))

    return CreateTenantResponse(
        tenant_id=str(tenant_id),
        name=body.name,
        slug=body.slug,
        plan_tier=body.plan_tier,
        posts_per_week=body.posts_per_week,
        api_key=plaintext,
        rate_limit_rpm=rpm,
        is_active=True,
        message="Store this API key securely — it will not be shown again. Tenant is active.",
    )

# ── GET /admin/tenants — List all tenants + usage ────────────────────────────


@router.get("/tenants", summary="List all tenants with usage stats")
async def list_tenants(
    request: Request,
    x_admin_secret: str = Header(None),
):
    """AA-473: Gate A removed -- every tenant is `is_active=true` from creation, so this endpoint
    is back to a plain active-tenant list (`pending_tenants`/`gate_a_status` concept removed
    entirely; `is_active=false` now only means manually deactivated/offboarded, tracked
    separately if ever needed).

    AA-472: `seeded`/`seeded_tour_count`/`angle_assigned` are removed from this response --
    acp_shared.tenant_atom_state is no longer part of the N1 onboarding flow (portfolio seeding
    and per-tenant angle assignment were both removed, Hướng B)."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                t.tenant_id, t.name, t.slug, t.plan_tier::text, t.posts_per_week,
                t.country, t.rate_limit_rpm, t.is_active, t.created_at,
                COALESCE(u.api_calls_used, 0)          AS api_calls_used,
                COALESCE(u.quota_tours_pct, 0)         AS quota_tours_pct,
                COALESCE(u.quota_calls_pct, 0)         AS quota_calls_pct,
                COALESCE(u.tours_overage, 0)           AS tours_overage,
                COALESCE(u.overage_usd, 0)             AS overage_usd,
                COALESCE(u.llm_cost_usd, 0)            AS llm_cost_usd,
                COALESCE(u.tours_quota_monthly, 0)     AS tours_quota_monthly,
                COALESCE(u.api_calls_quota_monthly, 0) AS api_calls_quota_monthly,
                COALESCE(u.price_usd_monthly, 0)       AS price_usd_monthly,
                COUNT(rt.tour_id) FILTER (WHERE rt.source_status::text = 'active')     AS source_active,
                COUNT(rt.tour_id) FILTER (WHERE rt.source_status::text = 'superseded') AS source_superseded,
                COUNT(rt.tour_id) FILTER (WHERE rt.source_status::text = 'trashed')    AS source_trashed,
                COUNT(pt.tour_id) FILTER (WHERE pt.master_status::text = 'active')     AS master_active,
                COUNT(pt.tour_id) FILTER (WHERE pt.master_status::text = 'inactive')   AS master_inactive,
                COUNT(pt.tour_id) FILTER (WHERE pt.master_status::text = 'trashed')    AS master_trashed
            FROM shared.tenants t
            LEFT JOIN shared.v_tenant_monthly_usage u
                ON u.tenant_id = t.tenant_id
            LEFT JOIN silver_aa_internal.raw_tours rt
                ON rt.tenant_id = t.tenant_id
            LEFT JOIN gold_aa_internal.published_tours pt
                ON pt.tenant_id = t.tenant_id
            WHERE t.is_active = true
            GROUP BY t.tenant_id, t.name, t.slug, t.plan_tier, t.posts_per_week, t.country,
                     t.rate_limit_rpm, t.is_active, t.created_at,
                     u.api_calls_used, u.quota_tours_pct, u.quota_calls_pct,
                     u.tours_overage, u.overage_usd, u.llm_cost_usd,
                     u.tours_quota_monthly, u.api_calls_quota_monthly, u.price_usd_monthly
            ORDER BY t.created_at
        """)
    return {
        "tenants": [
            {
                "tenant_id":      str(r["tenant_id"]),
                "name":           r["name"],
                "slug":           r["slug"],
                "plan_tier":      str(r["plan_tier"]),
                "posts_per_week": r["posts_per_week"],
                "country":        r["country"],
                "rate_limit_rpm": r["rate_limit_rpm"],
                "is_active":      r["is_active"],
                "created_at":     r["created_at"].isoformat(),
                "plan": {
                    "tours_quota_monthly":     r["tours_quota_monthly"],
                    "api_calls_quota_monthly": r["api_calls_quota_monthly"],
                    "price_usd_monthly":       float(r["price_usd_monthly"]),
                },
                "this_month": {
                    "tours_rewritten":  r["source_active"],
                    "api_calls_used":   r["api_calls_used"],
                    "quota_tours_pct":  float(r["quota_tours_pct"]),
                    "quota_calls_pct":  float(r["quota_calls_pct"]),
                    "tours_overage":    r["tours_overage"],
                    "overage_usd":      float(r["overage_usd"]),
                    "llm_cost_usd":     float(r["llm_cost_usd"]),
                },
                "lifecycle": {
                    "source_active":     r["source_active"],
                    "source_superseded": r["source_superseded"],
                    "source_trashed":    r["source_trashed"],
                    "master_active":     r["master_active"],
                    "master_inactive":   r["master_inactive"],
                    "master_trashed":    r["master_trashed"],
                },
            }
            for r in rows
        ],
        "total": len(rows),
    }

# ── GET /admin/tenants/{id}/usage — Billing metrics ──────────────────────────


@router.get("/tenants/{tenant_id}/usage", summary="Tenant billing metrics")
async def get_tenant_usage(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
    months: int = 3,
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT name, slug, plan_tier, rate_limit_rpm FROM shared.tenants WHERE tenant_id = $1",
            tenant_id,
        )
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        usage = await conn.fetch("""
            SELECT
                DATE_TRUNC('month', month) AS month,
                total_calls, successful_calls,
                rate_limited_calls, avg_response_ms
            FROM shared.v_tenant_monthly_usage
            WHERE tenant_id = $1
              AND month >= NOW() - ($2 || ' months')::interval
            ORDER BY month DESC
        """, tenant_id, str(months))

        tours_published = await conn.fetchval("""
            SELECT COUNT(*) FROM gold_aa_internal.published_tours
            WHERE tenant_id = $1 AND master_status <> 'trashed'
        """, tenant_id)

    plan = str(tenant["plan_tier"])
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["starter"])

    return {
        "tenant_id":   str(tenant_id),
        "name":        tenant["name"],
        "slug":        tenant["slug"],
        "plan_tier":   plan,
        "limits": {
            "rate_limit_rpm":    tenant["rate_limit_rpm"],
            "tours_per_month":   limits["tours_per_month"],
        },
        "tours_published": tours_published,
        "monthly_usage": [
            {
                "month":               r["month"].strftime("%Y-%m"),
                "total_calls":         r["total_calls"],
                "successful_calls":    r["successful_calls"],
                "rate_limited_calls":  r["rate_limited_calls"],
                "avg_response_ms":     float(r["avg_response_ms"]),
            }
            for r in usage
        ],
    }

# ── PATCH /admin/tenants/{id} — Update plan/status ───────────────────────────


@router.patch("/tenants/{tenant_id}", summary="Update tenant plan or status")
async def update_tenant(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
    plan_tier: Optional[str] = None,
    is_active: Optional[bool] = None,
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        if plan_tier:
            if plan_tier not in PLAN_LIMITS:
                raise HTTPException(status_code=400, detail="Invalid plan_tier")
            rpm = PLAN_LIMITS[plan_tier]["rpm"]
            await conn.execute("""
                UPDATE shared.tenants
                SET plan_tier = $2::plan_tier_enum, rate_limit_rpm = $3, updated_at = NOW()
                WHERE tenant_id = $1
            """, tenant_id, plan_tier, rpm)

        if is_active is not None:
            # AA-473: Gate A removed -- activation and deactivation are both unrestricted now,
            # same as deactivation already was (there is no approval gate left to bypass).
            await conn.execute("""
                UPDATE shared.tenants
                SET is_active = $2, updated_at = NOW()
                WHERE tenant_id = $1
            """, tenant_id, is_active)

    return {"status": "updated", "tenant_id": str(tenant_id)}

def _parse_fw(value) -> list:
    """Parse forbidden_words from asyncpg — may be list (pg array) or JSON string (JSONB)."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        import json as _j
        try:
            parsed = _j.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return list(value)


# ── DELETE /admin/tenants/{id} — soft delete ─────────────────────────────────


@router.delete("/tenants/{tenant_id}", summary="Soft-delete tenant (is_active=false)")
async def delete_tenant(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        updated = await conn.fetchval("""
            UPDATE shared.tenants SET is_active=false, updated_at=NOW()
            WHERE tenant_id=$1 RETURNING tenant_id
        """, tenant_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"status": "deleted", "tenant_id": str(tenant_id)}


# ── GET /admin/tenants/{id}/details — 4-tab detail view ─────────────────────


@router.get("/tenants/{tenant_id}/details", summary="Tenant 4-tab detail view")
async def get_tenant_details(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        tenant = await conn.fetchrow("""
            SELECT name, slug, plan_tier::text, rate_limit_rpm, created_at
            FROM shared.tenants WHERE tenant_id = $1
        """, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        is_internal = tenant["plan_tier"] == "internal"

        if is_internal:
            total_rewrites = await conn.fetchval(
                "SELECT COUNT(*) FROM gold_aa_internal.published_tours "
                "WHERE master_status <> 'trashed'"
            )
        else:
            total_rewrites = await conn.fetchval("""
                SELECT COUNT(*) FROM gold_aa_internal.tenant_tour_versions
                WHERE tenant_id = $1
            """, tenant_id)

        total_cost = await conn.fetchval("""
            SELECT COALESCE(SUM(cost_usd), 0)
            FROM shared.pipeline_runs WHERE tenant_id = $1
        """, tenant_id)

        # v_tenant_monthly_usage has one row per tenant per billing_month;
        # ORDER BY DESC so we always get the current/most-recent month.
        # COALESCE guards: new tenants have no quota row, producing NULLs.
        usage = await conn.fetchrow("""
            SELECT
                COALESCE(api_calls_used, 0)            AS api_calls_used,
                COALESCE(quota_calls_pct, 0)           AS quota_calls_pct,
                COALESCE(api_calls_quota_monthly, 0)   AS api_calls_quota_monthly
            FROM shared.v_tenant_monthly_usage WHERE tenant_id = $1
            ORDER BY billing_month DESC LIMIT 1
        """, tenant_id)

        if is_internal:
            # Show published_tours for the internal catalog
            tours = await conn.fetch("""
                SELECT pt.id, pt.tour_id, pt.aa_name, rt.country,
                       pt.quality_score, pt.master_status::text AS master_status,
                       (SELECT gc.version_num FROM silver_aa_internal.generated_content gc
                        WHERE gc.tour_id = pt.tour_id ORDER BY gc.created_at DESC LIMIT 1) AS version_number,
                       -- AA-626: how many of this tour's versions are still stuck pending in the
                       -- review queue. Surfaced so the admin can jump over and dismiss the stale
                       -- failed versions of a tour that already has an approved master version.
                       (SELECT COUNT(*) FROM silver_aa_internal.review_queue rq
                        WHERE rq.tour_id = pt.tour_id AND rq.review_status = 'pending')
                        AS pending_review_count,
                       'published'::text AS status, pt.published_at AS created_at
                FROM gold_aa_internal.published_tours pt
                LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
                ORDER BY pt.published_at DESC LIMIT 200
            """)
        else:
            tours = await conn.fetch("""
                SELECT ttv.id, NULL::uuid AS tour_id, pt.aa_name, rt.country,
                       ttv.quality_score, ttv.version_number, ttv.status,
                       'active'::text AS master_status, ttv.created_at
                FROM gold_aa_internal.tenant_tour_versions ttv
                JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
                LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
                WHERE ttv.tenant_id = $1
                ORDER BY ttv.created_at DESC LIMIT 50
            """, tenant_id)

        # UNION: direct tenant runs + runs containing this tenant's tours (covers B2B tenants
        # whose tours were processed under aa_internal pipeline)
        runs = await conn.fetch("""
            SELECT * FROM (
                SELECT pr.batch_id, pr.started_at, pr.tours_total, pr.tours_passed,
                       pr.llm_model, pr.cost_usd, pr.status
                FROM shared.pipeline_runs pr
                WHERE pr.tenant_id = $1
                UNION
                SELECT pr.batch_id, pr.started_at, pr.tours_total, pr.tours_passed,
                       pr.llm_model, pr.cost_usd, pr.status
                FROM shared.pipeline_runs pr
                JOIN silver_aa_internal.raw_tours rt ON rt.batch_id = pr.batch_id
                JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id
                JOIN gold_aa_internal.tenant_tour_versions ttv ON ttv.published_tour_id = pt.id
                WHERE ttv.tenant_id = $1
            ) _combined
            ORDER BY started_at DESC LIMIT 20
        """, tenant_id)

        brand_rows = await conn.fetch("""
            SELECT
                system_prompt, style_guide, forbidden_words, version, updated_at,
                COALESCE(brand_name, '')         AS brand_name,
                COALESCE(brand_type, '')         AS brand_type,
                COALESCE(core_idea, '')          AS core_idea,
                COALESCE(customer_segment, '')   AS customer_segment,
                COALESCE(customer_mindset, '')   AS customer_mindset,
                COALESCE(voice_examples, '[]'::jsonb) AS voice_examples,
                COALESCE(good_examples, '')      AS good_examples,
                COALESCE(rewrite_language, 'en') AS rewrite_language,
                COALESCE(target_markets, ARRAY[]::text[]) AS target_markets
            FROM shared.tenant_brand_rules
            WHERE tenant_id = $1
            ORDER BY version DESC
        """, tenant_id)

    brand         = brand_rows[0] if brand_rows else None
    api_calls     = int(usage["api_calls_used"])            if usage else 0
    quota_total   = int(usage["api_calls_quota_monthly"])   if usage else 0
    quota_pct     = float(usage["quota_calls_pct"])         if usage else 0.0

    last_updated: str | None = None
    if brand and brand["updated_at"]:
        last_updated = brand["updated_at"].isoformat()

    def _parse_jsonb_list(value) -> list:
        """Parse a JSONB LIST field (asyncpg may return list or JSON string, no jsonb codec
        registered on this connection). `voice_examples` stores `tone_of_voice` as a JSON array
        (admin_pipeline.py's BrandCreateRequest/update_brand_identity, both `json.dumps(list)`) —
        AA-557 J.24 fix: this helper used to parse-as-dict and silently return `{}` for that real
        list value, so "Tone of Voice" always rendered empty here even with real saved data."""
        if not value:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            import json as _j
            try:
                parsed = _j.loads(value)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    return {
        "summary": {
            "total_rewrites":       int(total_rewrites or 0),
            "total_llm_cost_usd":   float(total_cost or 0),
            "api_calls_this_month": api_calls,
            "quota_pct":            quota_pct,
            "plan_name":            str(tenant["plan_tier"]).title(),
            "member_since":         tenant["created_at"].isoformat()[:10],
            "tours_view":           "published" if is_internal else "rewrites",
            "pipeline_note":        None if is_internal else "Showing pipeline runs for tours in your catalog",
        },
        "rewritten_tours": [
            {
                "version_id":     str(r["id"]),
                "tour_id":        str(r["tour_id"]) if r.get("tour_id") else None,
                "tour_name":      r["aa_name"] or "—",
                "country":        r["country"],
                "quality_score":  float(r["quality_score"]) if r["quality_score"] is not None else None,
                "version_number": r["version_number"],
                "status":         r["status"],
                "master_status":  r["master_status"] if r["master_status"] else "active",
                "created_at":     r["created_at"].isoformat(),
                # AA-626: only the internal (published_tours) branch selects this; tenant branch
                # has no such column, so default 0 rather than KeyError.
                "pending_review_count": (
                    int(r["pending_review_count"])
                    if "pending_review_count" in r and r["pending_review_count"] is not None
                    else 0
                ),
            }
            for r in tours
        ],
        "pipeline_runs": [
            {
                "run_id":          str(r["batch_id"]),
                "started_at":      r["started_at"].isoformat(),
                "tours_processed": int(r["tours_total"] or 0),
                "tours_passed":    int(r["tours_passed"] or 0),
                "llm_model":       r["llm_model"],
                "llm_cost_usd":    float(r["cost_usd"] or 0),
                "status":          r["status"],
            }
            for r in runs
        ],
        "api_usage": {
            "total_calls":        api_calls,
            "quota_used":         api_calls,
            "quota_total":        quota_total,
            "rate_limit_per_min": tenant["rate_limit_rpm"],
        },
        "brand_rules": {
            "system_prompt":    brand["system_prompt"]               if brand else None,
            "style_guide":      brand["style_guide"]                 if brand else None,
            "forbidden_words":  _parse_fw(brand["forbidden_words"]) if brand else [],
            "version_count":    len(brand_rows),
            "last_updated":     last_updated,
            "brand_name":       brand["brand_name"]       if brand else "",
            "brand_type":       brand["brand_type"]       if brand else "",
            "core_idea":        brand["core_idea"]        if brand else "",
            "customer_segment": brand["customer_segment"] if brand else "",
            "customer_mindset": brand["customer_mindset"] if brand else "",
            "voice_examples":   _parse_jsonb_list(brand["voice_examples"]) if brand else [],
            "good_examples":    brand["good_examples"]    if brand else "",
            "rewrite_language": brand["rewrite_language"] if brand else "en",
            "target_markets":   list(brand["target_markets"]) if brand else [],
        },
    }


class TenantBrandIdentityUpdate(BaseModel):
    """AA-557 J.24 — same field set as the tenant-facing `POST /admin/brand-identity`
    (admin_pipeline.py's `BrandIdentityUpdate`), duplicated here rather than imported: that
    endpoint resolves its own tenant_id from a tenant JWT-or-AA-internal-fallback
    (`_resolve_brand_tenant_id`) with no path for "admin editing an EXPLICIT other tenant's
    brand" — this endpoint exists specifically to give Admin's Tenant→Brand tab that write path
    onto the exact same `shared.tenant_brand_rules` row (2 UIs, 1 data source, per the issue's
    own requirement), not a second copy of the schema."""
    system_prompt:     Optional[str] = None
    style_guide:        Optional[str] = None
    forbidden_words:    Optional[List[str]] = None
    brand_name:         Optional[str] = None
    brand_type:         Optional[str] = None
    core_idea:          Optional[str] = None
    customer_segment:   Optional[str] = None
    customer_mindset:   Optional[str] = None
    tone_of_voice:      Optional[List[str]] = None
    writing_style:      Optional[str] = None
    good_examples:      Optional[str] = None
    target_markets:     Optional[List[str]] = None


@router.put("/tenants/{tenant_id}/brand-identity")
async def update_tenant_brand_identity(
    tenant_id: UUID,
    body: TenantBrandIdentityUpdate,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Admin-side write for a SPECIFIC tenant's brand identity (AA-557 J.24) — same table, same
    columns, same NOT-scoped-by-brand_name "1 active row per tenant" invariant as
    admin_pipeline.py's tenant-facing `update_brand_identity()` (kept identical deliberately, see
    that function's own comment for why: avoids 2 simultaneously-active rows if brand_name
    changes mid-flow). Prompt-injection sanitized the same way (AA-487's `shared.validators.
    prompt_sanitize`) since these free-text fields feed straight into every future rewrite for
    this tenant, same as the tenant's own self-service path."""
    verify_admin_secret(x_admin_secret)
    tenant_id_s = str(tenant_id)

    system_prompt = sanitize_text(body.system_prompt, MAX_SYSTEM_PROMPT_LEN)
    style_guide = sanitize_text(body.writing_style or body.style_guide, MAX_LONG_FIELD_LEN)
    forbidden_words = sanitize_list(body.forbidden_words or [], MAX_SHORT_FIELD_LEN, max_items=20)
    brand_name = sanitize_text(body.brand_name, MAX_SHORT_FIELD_LEN) or "default"
    brand_type = sanitize_text(body.brand_type, MAX_SHORT_FIELD_LEN)
    core_idea = sanitize_text(body.core_idea, MAX_LONG_FIELD_LEN)
    customer_segment = sanitize_text(body.customer_segment, MAX_LONG_FIELD_LEN)
    customer_mindset = sanitize_text(body.customer_mindset, MAX_LONG_FIELD_LEN)
    tone_of_voice = sanitize_list(body.tone_of_voice or [], MAX_SHORT_FIELD_LEN, max_items=20)
    good_examples = sanitize_text(body.good_examples, MAX_LONG_FIELD_LEN)
    target_markets = sanitize_list(body.target_markets or [], MAX_SHORT_FIELD_LEN, max_items=20)

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tenant_exists = await conn.fetchval(
            "SELECT 1 FROM shared.tenants WHERE tenant_id = $1", tenant_id_s
        )
        if not tenant_exists:
            raise HTTPException(status_code=404, detail="Tenant not found")
        current = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM shared.tenant_brand_rules WHERE tenant_id = $1",
            tenant_id_s,
        )
        await conn.execute(
            "UPDATE shared.tenant_brand_rules SET is_active = false WHERE tenant_id = $1", tenant_id_s
        )
        await conn.execute("""
            INSERT INTO shared.tenant_brand_rules
                (tenant_id, brand_name, brand_type, core_idea, customer_segment,
                 customer_mindset, voice_examples, style_guide, good_examples, system_prompt,
                 forbidden_words, target_markets, version, is_active, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11::jsonb, $12, $13, true, NOW())
        """, tenant_id_s, brand_name, brand_type, core_idea, customer_segment, customer_mindset,
            json.dumps(tone_of_voice), style_guide, good_examples, system_prompt,
            json.dumps(forbidden_words), target_markets, current + 1)
    return {"status": "updated", "version": current + 1}


# ── GET /admin/tenants/{id}/rewrite-activity ─────────────────────────────────


@router.get("/tenants/{tenant_id}/rewrite-activity")
async def get_rewrite_activity(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                ttv.id,
                pt.aa_name AS tour_name,
                rt.country,
                ttv.version_number,
                ttv.status,
                ttv.quality_score,
                ttv.edit_source,
                ttv.created_at
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE ttv.tenant_id = $1
            ORDER BY ttv.created_at DESC
        """, tenant_id)

    return {
        "rewrite_activity": [
            {
                "version_id":     str(r["id"]),
                "tour_name":      r["tour_name"] or "—",
                "country":        r["country"],
                "version_number": r["version_number"],
                "status":         r["status"],
                "quality_score":  float(r["quality_score"]) if r["quality_score"] is not None else None,
                "edit_source":    r["edit_source"],
                "created_at":     r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


# ── POST /admin/tenants/{id}/generate-key ────────────────────────────────────


@router.post("/tenants/{tenant_id}/generate-key", response_model=GenerateKeyResponse)
async def generate_api_key(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id, name FROM shared.tenants WHERE tenant_id = $1", tenant_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Tenant not found")
        plaintext = f"cis_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        await conn.execute("""
            UPDATE shared.tenants
            SET api_key_hash = $1, updated_at = NOW()
            WHERE tenant_id = $2
        """, key_hash, tenant_id)

    return GenerateKeyResponse(
        tenant_id=str(row["tenant_id"]),
        tenant_name=row["name"],
        api_key=plaintext,
        message="Store this key securely — it will not be shown again.",
    )


# ── POST /admin/tenants/{id}/brand-brief ─────────────────────────────────────

_BRAND_BRIEF_BUCKET = "acp-bronze-867490540162"
_BRAND_BRIEF_LAMBDA = "acp-brand-brief-parser"
_MAX_DOCX_BYTES = 5 * 1024 * 1024  # 5 MB


@router.post("/tenants/{tenant_id}/brand-brief", summary="Upload and parse brand brief DOCX")
async def upload_brand_brief(
    tenant_id: str,
    request: Request,
    file: UploadFile = File(...),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)

    content_type = file.content_type or ""
    if "officedocument.wordprocessingml" not in content_type and not file.filename.endswith(".docx"):
        raise HTTPException(status_code=400, detail="File must be a .docx document")

    data = await file.read()
    if len(data) > _MAX_DOCX_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds 5 MB limit")

    iso_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    s3_key = f"brand-briefs/{tenant_id}/{iso_ts}.docx"

    s3 = boto3.client("s3", region_name="us-west-1")
    try:
        s3.upload_fileobj(io.BytesIO(data), _BRAND_BRIEF_BUCKET, s3_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"S3 upload failed: {e}")

    lam = boto3.client("lambda", region_name="us-west-1")
    payload = json.dumps({
        "tenant_id": tenant_id,
        "s3_bucket": _BRAND_BRIEF_BUCKET,
        "s3_key": s3_key,
    }).encode()
    try:
        resp = lam.invoke(
            FunctionName=_BRAND_BRIEF_LAMBDA,
            InvocationType="RequestResponse",
            Payload=payload,
        )
        result = json.loads(resp["Payload"].read())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Lambda invoke failed: {e}")

    if result.get("status") == "error":
        raise HTTPException(status_code=422, detail=result.get("warnings", ["Unknown parse error"]))

    # Persist S3 key for brand brief reuse on next S0 run (M1)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE shared.tenants SET last_brand_brief_s3_key=$2, updated_at=NOW() WHERE tenant_id=$1::uuid",
            tenant_id, s3_key,
        )

    return {**result, "brand_brief_s3_key": s3_key}


# ── POST /admin/tenants/{id}/offboard — GDPR offboard ────────────────────────

class OffboardRequest(BaseModel):
    reason: str


@router.post("/tenants/{tenant_id}/offboard", summary="GDPR offboarding — cancel tenant + revoke key")
async def offboard_tenant(
    tenant_id: str,
    body: OffboardRequest,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """
    GDPR offboarding: cancel tenant, revoke API key, log for data deletion.
    S3 data deletion must be run manually (Lambda or CLI) after 14 days.
    PRD v1.0 §3.2.
    """
    verify_admin_secret(x_admin_secret)
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required for GDPR audit trail")

    try:
        UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid tenant_id UUID")

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT tenant_id, name, cancelled_at FROM shared.tenants WHERE tenant_id=$1::uuid",
            tenant_id,
        )
        if not tenant:
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
        if tenant["cancelled_at"]:
            raise HTTPException(
                status_code=409,
                detail=f"Tenant already offboarded at {tenant['cancelled_at'].isoformat()}",
            )

        async with conn.transaction():
            await conn.execute(
                """
                UPDATE shared.tenants
                SET cancelled_at        = NOW(),
                    cancellation_reason = $2,
                    is_active           = FALSE,
                    api_key_hash        = 'REVOKED_' || tenant_id::text,
                    updated_at          = NOW()
                WHERE tenant_id = $1::uuid
                """,
                tenant_id, body.reason,
            )
            await conn.execute(
                """
                INSERT INTO acp_shared.audit_log
                    (tenant_id, actor, action, resource_type, resource_id, details)
                VALUES ($1, 'admin_api', 'agency.offboard', 'tenant', $1, $2::jsonb)
                """,
                tenant_id, json.dumps({
                    "reason": body.reason,
                    "name": tenant["name"],
                    "note": "S3 data deletion: acp-cis-*/{tenant_id}/ — schedule Lambda after 14d",
                }),
            )

    return {
        "tenant_id": tenant_id,
        "status": "offboarded",
        "api_key": "REVOKED",
        "note": f"S3 prefix acp-cis-bronze-867490540162/{tenant_id}/ must be deleted after 14 days.",
    }


# ── GET/PUT /admin/tenants/{id}/config — AA-323 Gap 3: N4-N6 markets/channels/
# capacity config. capacity_posts_per_week reads/writes shared.tenants.posts_per_week
# (AA-384, existing single source of truth) — markets/channels read/write the new
# acp_shared.tenant_config table (migration 101). One combined form since the issue
# asked for a single place to set all three; no duplicate posts_per_week column.

class TenantConfigRequest(BaseModel):
    markets: list[str] = Field(..., min_length=1)
    channels: list[str] = Field(..., min_length=1)
    posts_per_week: int = Field(..., ge=1, le=14)


# AA-449 — extended from 4 to 8 values (kept "blog" for backward compat even though it has no
# row in T8's Bang-2 channel-style table; see services/acp_planning/models.py's Channel Literal,
# which this set must stay in sync with — same 8 values, same names).
_VALID_CHANNELS = {
    "blog", "facebook", "tiktok", "email", "linkedin", "instagram", "landing_page", "ads",
}


@router.get("/tenants/{tenant_id}/config", summary="AA-323 — N4-N6 markets/channels/capacity for one tenant")
async def get_tenant_config(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    try:
        cfg = await fetch_tenant_planning_config(tenant_id, pool)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
    return {
        "tenant_id": str(tenant_id),
        "markets": cfg.markets,
        "channels": cfg.channels,
        "posts_per_week": cfg.capacity_posts_per_week,
    }


@router.put("/tenants/{tenant_id}/config", summary="AA-323 — set N4-N6 markets/channels/capacity for one tenant")
async def update_tenant_config(
    tenant_id: UUID,
    body: TenantConfigRequest,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    invalid = [c for c in body.channels if c not in _VALID_CHANNELS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid channel(s): {invalid} — must be one of {sorted(_VALID_CHANNELS)}",
        )
    pool = request.app.state.pool
    try:
        await save_tenant_planning_config(
            tenant_id, body.markets, body.channels, body.posts_per_week, pool,
        )
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
    return {
        "tenant_id": str(tenant_id),
        "markets": body.markets,
        "channels": body.channels,
        "posts_per_week": body.posts_per_week,
    }


# ── PATCH /admin/master/{tour_id}/status — Toggle master active/inactive ──────

AA_INTERNAL_TENANT = "00000000-0000-0000-0000-000000000001"


@router.patch("/master/{tour_id}/status", summary="Toggle master tour active/inactive")
async def toggle_master_status(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    body = await request.json()
    status = body.get("master_status")
    if status not in ("active", "inactive"):
        raise HTTPException(400, "master_status must be 'active' or 'inactive'")

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT aa_name FROM gold_aa_internal.published_tours WHERE tour_id=$1::uuid",
            tour_id,
        )
        if not row:
            raise HTTPException(404, "Tour not found")

        event_type = (
            EventType.MASTER_ACTIVATED if status == "active"
            else EventType.MASTER_DEACTIVATED
        )

        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE gold_aa_internal.published_tours
                SET master_status = $1::gold_aa_internal.master_status_enum
                WHERE tour_id = $2::uuid
                """,
                status, tour_id,
            )
            if result == "UPDATE 0":
                raise HTTPException(404, "Tour not found or not updated")

            await NotificationService(conn).emit(
                event_type=event_type,
                entity_type="tour",
                entity_id=tour_id,
                tenant_id=AA_INTERNAL_TENANT,
                payload={"tour_name": row["aa_name"], "new_status": status, "changed_by": "admin"},
                actor_type="admin",
            )

    return {"tour_id": tour_id, "master_status": status}


# ── GET /admin/catalog — staff content review list (AA-580) ────────────────────
# Admin-native replacement for the bare `GET /v1/tours` deleted in AA-579 (Depends(get_tenant)
# Bearer-JWT auth — content/reviewer staff have no tenant JWT, only x-admin-secret via the
# `/catalog` staff proxy, so that route was never reachable with valid auth in the first place).
# Path is `/admin/catalog`, not `/admin/tours` — admin_pipeline.py already owns `GET /admin/tours`
# for a different shape (raw_tours/pipeline_status, used by /admin/s1-rewrite); reusing that path
# would collide. No tenant_id scoping (unlike the deleted route's `pt.tenant_id = $1`, which never
# matched any real tenant JWT anyway) — this is staff reviewing the whole aa_internal catalog, not
# a per-tenant B2B view. Same SELECT fields as the deleted route otherwise, same {data,pagination}
# response shape, so frontend/app/(internal)/catalog/page.tsx needs only a URL change.


@router.get("/catalog", summary="AA-580 — staff content review: published_tours list")
async def list_catalog_tours(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    min_quality: Optional[float] = Query(None, ge=0, le=1),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    offset = (page - 1) * page_size

    conditions = []
    params: list = []

    if min_quality is not None:
        params.append(min_quality)
        conditions.append(f"pt.quality_score >= ${len(params)}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"""
            SELECT COUNT(*)
            FROM gold_aa_internal.published_tours pt
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            {where}
        """, *params)
        params_paged = params + [page_size, offset]
        rows = await conn.fetch(f"""
            SELECT pt.id, pt.tour_id, pt.aa_name, pt.aa_subtitle, pt.aa_summary,
                   pt.seo_title, pt.quality_score, pt.published_at,
                   rt.country
            FROM gold_aa_internal.published_tours pt
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            {where}
            ORDER BY pt.published_at DESC
            LIMIT ${len(params)+1} OFFSET ${len(params)+2}
        """, *params_paged)

    return {
        "data": [dict(r) for r in rows],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": -(-total // page_size) if total else 0,
        },
    }


# ── PATCH /admin/tours/{tour_id}/trash — Soft-delete source tour ───────────────


@router.patch("/tours/{tour_id}/trash", summary="Soft-delete source tour (source_status=trashed)")
async def trash_source_tour(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT src_name, tenant_id FROM silver_aa_internal.raw_tours
               WHERE tour_id=$1::uuid AND source_status != 'trashed'""",
            tour_id,
        )
        if not row:
            raise HTTPException(404, "Tour not found or already trashed")

        async with conn.transaction():
            result = await conn.fetchrow(
                """
                UPDATE silver_aa_internal.raw_tours
                SET source_status = 'trashed'::silver_aa_internal.source_status_enum,
                    deleted_at = NOW(),
                    deleted_by = 'admin'
                WHERE tour_id = $1::uuid AND source_status != 'trashed'
                RETURNING tour_id, source_status::text, deleted_at
                """,
                tour_id,
            )
            if not result:
                raise HTTPException(404, "Tour not found or already trashed")

            await NotificationService(conn).emit(
                event_type=EventType.SOURCE_TRASHED,
                entity_type="tour",
                entity_id=tour_id,
                tenant_id=str(row["tenant_id"]),
                payload={"tour_name": row["src_name"], "changed_by": "admin"},
                actor_type="admin",
            )

    return {
        "tour_id": tour_id,
        "source_status": "trashed",
        "deleted_at": result["deleted_at"].isoformat(),
    }


# ── PATCH /admin/tours/{tour_id}/restore — Restore trashed source tour ─────────


@router.patch("/tours/{tour_id}/restore", summary="Restore trashed source tour (source_status=active)")
async def restore_source_tour(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT src_name, tenant_id FROM silver_aa_internal.raw_tours
               WHERE tour_id=$1::uuid AND source_status='trashed'""",
            tour_id,
        )
        if not row:
            raise HTTPException(404, "Tour not found or not in trashed state")

        async with conn.transaction():
            try:
                result = await conn.fetchrow(
                    """
                    UPDATE silver_aa_internal.raw_tours
                    SET source_status = 'active'::silver_aa_internal.source_status_enum,
                        deleted_at = NULL,
                        deleted_by = NULL
                    WHERE tour_id = $1::uuid AND source_status = 'trashed'
                    RETURNING tour_id, source_status::text
                    """,
                    tour_id,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    409,
                    "Another active tour exists in the same source group. "
                    "Set it to superseded first, then restore.",
                )
            if not result:
                raise HTTPException(404, "Tour not found or not in trashed state")

            await NotificationService(conn).emit(
                event_type=EventType.SOURCE_RESTORED,
                entity_type="tour",
                entity_id=tour_id,
                tenant_id=str(row["tenant_id"]),
                payload={"tour_name": row["src_name"], "changed_by": "admin"},
                actor_type="admin",
            )

    return {"tour_id": tour_id, "source_status": "active"}


# ── PATCH /admin/master/{tour_id}/trash — Soft-delete master tour ─────────────


@router.patch("/master/{tour_id}/trash", summary="Soft-delete master tour (master_status=trashed)")
async def trash_master_tour(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT aa_name FROM gold_aa_internal.published_tours
               WHERE tour_id=$1::uuid AND master_status != 'trashed'""",
            tour_id,
        )
        if not row:
            raise HTTPException(404, "Tour not found or already trashed")

        async with conn.transaction():
            result = await conn.fetchrow(
                """
                UPDATE gold_aa_internal.published_tours
                SET master_status = 'trashed'::gold_aa_internal.master_status_enum,
                    deleted_at = NOW(),
                    deleted_by = 'admin'
                WHERE tour_id = $1::uuid AND master_status != 'trashed'
                RETURNING tour_id, master_status::text, deleted_at
                """,
                tour_id,
            )
            if not result:
                raise HTTPException(404, "Tour not found or already trashed")

            await NotificationService(conn).emit(
                event_type=EventType.MASTER_TRASHED,
                entity_type="tour",
                entity_id=tour_id,
                tenant_id=AA_INTERNAL_TENANT,
                payload={"tour_name": row["aa_name"], "changed_by": "admin"},
                actor_type="admin",
            )

    return {
        "tour_id": tour_id,
        "master_status": "trashed",
        "deleted_at": result["deleted_at"].isoformat(),
    }


# ── PATCH /admin/master/{tour_id}/restore — Restore trashed master tour ────────


@router.patch("/master/{tour_id}/restore", summary="Restore trashed master tour (master_status=inactive)")
async def restore_master_tour(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT aa_name FROM gold_aa_internal.published_tours
               WHERE tour_id=$1::uuid AND master_status='trashed'""",
            tour_id,
        )
        if not row:
            raise HTTPException(404, "Tour not found or not in trashed state")

        async with conn.transaction():
            result = await conn.fetchrow(
                """
                UPDATE gold_aa_internal.published_tours
                SET master_status = 'inactive'::gold_aa_internal.master_status_enum,
                    deleted_at = NULL,
                    deleted_by = NULL
                WHERE tour_id = $1::uuid AND master_status = 'trashed'
                RETURNING tour_id, master_status::text
                """,
                tour_id,
            )
            if not result:
                raise HTTPException(404, "Tour not found or not in trashed state")

            await NotificationService(conn).emit(
                event_type=EventType.MASTER_RESTORED,
                entity_type="tour",
                entity_id=tour_id,
                tenant_id=AA_INTERNAL_TENANT,
                payload={
                    "tour_name": row["aa_name"],
                    "new_status": "inactive",
                    "changed_by": "admin",
                },
                actor_type="admin",
            )

    return {"tour_id": tour_id, "master_status": "inactive"}


# ── PATCH /admin/master/{tour_id}/activate ────────────────────────────────────


@router.patch("/master/{tour_id}/activate", summary="Set master tour to active")
async def activate_master_tour(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT aa_name FROM gold_aa_internal.published_tours WHERE tour_id=$1::uuid",
            tour_id,
        )
        if not row:
            raise HTTPException(404, "Tour not found")

        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE gold_aa_internal.published_tours
                SET master_status = 'active'::gold_aa_internal.master_status_enum
                WHERE tour_id = $1::uuid
                """,
                tour_id,
            )
            if result == "UPDATE 0":
                raise HTTPException(404, "Tour not found or not updated")

            await NotificationService(conn).emit(
                event_type=EventType.MASTER_ACTIVATED,
                entity_type="tour",
                entity_id=tour_id,
                tenant_id=AA_INTERNAL_TENANT,
                payload={
                    "tour_name": row["aa_name"], "new_status": "active", "changed_by": "admin",
                },
                actor_type="admin",
            )

    return {"tour_id": tour_id, "master_status": "active"}


# ── PATCH /admin/master/{tour_id}/deactivate ──────────────────────────────────


@router.patch("/master/{tour_id}/deactivate", summary="Set master tour to inactive")
async def deactivate_master_tour(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT aa_name FROM gold_aa_internal.published_tours WHERE tour_id=$1::uuid",
            tour_id,
        )
        if not row:
            raise HTTPException(404, "Tour not found")

        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE gold_aa_internal.published_tours
                SET master_status = 'inactive'::gold_aa_internal.master_status_enum
                WHERE tour_id = $1::uuid
                """,
                tour_id,
            )
            if result == "UPDATE 0":
                raise HTTPException(404, "Tour not found or not updated")

            await NotificationService(conn).emit(
                event_type=EventType.MASTER_DEACTIVATED,
                entity_type="tour",
                entity_id=tour_id,
                tenant_id=AA_INTERNAL_TENANT,
                payload={
                    "tour_name": row["aa_name"], "new_status": "inactive", "changed_by": "admin",
                },
                actor_type="admin",
            )

    return {"tour_id": tour_id, "master_status": "inactive"}


# ── GET /admin/notifications/count ────────────────────────────────────────────


@router.get("/notifications/count", summary="Count unread notifications")
async def notification_count(
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS unread FROM shared.notifications WHERE is_read = FALSE"
        )
    return {"unread": int(row["unread"])}


# ── PUT /admin/notifications/read-all ────────────────────────────────────────


@router.put("/notifications/read-all", summary="Mark all notifications read")
async def mark_all_read(
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE shared.notifications SET is_read = TRUE WHERE is_read = FALSE"
        )
    cleared = int(result.split()[-1])
    return {"cleared": cleared}


# ── GET /admin/notifications ──────────────────────────────────────────────────


@router.get("/notifications", summary="List notifications")
async def list_notifications(
    request: Request,
    x_admin_secret: str = Header(None),
    unread_only: bool = False,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    verify_admin_secret(x_admin_secret)
    where = "WHERE is_read = FALSE" if unread_only else ""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, event_type, entity_type, entity_id,
                   payload, target_roles, is_read, dispatched_at, created_at
            FROM shared.notifications
            {where}
            ORDER BY created_at DESC LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )
    _event_labels = {
        "tour.pipeline.completed":  "Pipeline completed",
        "tour.pipeline.failed":     "Pipeline failed",
        "tour.brand_audit.flagged": "Brand audit flagged",
        "tour.brand_audit.fixed":   "Brand audit fixed",
        "tour.dedup.staged":        "Duplicate staged for review",
        "tour.dedup.promoted":      "Duplicate promoted",
        "tour.source.trashed":      "Source tour trashed",
        "tour.source.restored":     "Source tour restored",
        "tour.master.activated":    "Master tour activated",
        "tour.master.deactivated":  "Master tour deactivated",
        "tour.master.trashed":      "Master tour trashed",
        "tour.master.restored":     "Master tour restored",
    }

    items = []
    for r in rows:
        item = dict(r)
        item["id"] = int(item["id"])
        payload = dict(item["payload"]) if item["payload"] else {}
        item["payload"] = payload
        item["target_roles"] = list(item["target_roles"]) if item["target_roles"] else []
        item["dispatched_at"] = item["dispatched_at"].isoformat()
        item["created_at"] = item["created_at"].isoformat()
        item["title"] = _event_labels.get(item["event_type"], item["event_type"])
        item["message"] = payload.get("tour_name") or payload.get("message") or ""
        items.append(item)
    return {"items": items, "total": len(items)}


# ── PUT /admin/notifications/{notif_id}/read ─────────────────────────────────


@router.put("/notifications/{notif_id}/read", summary="Mark one notification read")
async def mark_read(
    notif_id: int,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE shared.notifications SET is_read = TRUE WHERE id = $1",
            notif_id,
        )
    return {"id": notif_id, "is_read": True}
