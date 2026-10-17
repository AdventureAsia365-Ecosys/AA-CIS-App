# api/routers/admin.py
# P2-S5 — Multi-tenant onboarding + billing metrics
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID
from typing import List, Optional
import asyncpg
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from api.core.secret_compare import secrets_equal

from services.acp_publish import daily_limit
from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.acp_shared.e2e_tenant_key_sync import sync_note, sync_test_tenant_key
from services.notifications import NotificationService, EventType
from shared.validators.prompt_sanitize import (
    sanitize_text, sanitize_list, MAX_LONG_FIELD_LEN, MAX_SHORT_FIELD_LEN, MAX_SYSTEM_PROMPT_LEN,
    MAX_BRAND_LONG_LEN, MAX_BRAND_MEDIUM_LEN, brand_length_errors,
)
from services.acp_planning.tenant_config import (
    TenantNotFoundError,
    fetch_tenant_planning_config,
    save_tenant_planning_config,
)

import structlog

router = APIRouter(prefix="/admin", tags=["admin"])
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
_log = structlog.get_logger()


async def _recompute_after_status_change(pool, tour_id: str, new_status: str) -> None:
    """AA-713 / AA-723 — when a master tour leaves (inactive/trashed) or re-enters (active) the
    active set, its atoms must leave/re-enter the Score + Route caches the tenant Slate reads
    (both read atoms through `v_active_tour_atoms`, migration 203). The recompute now runs as a
    durable `recompute` job on the worker (scope=platform) instead of a fire-and-forget asyncio
    task that died on deploy and was invisible (ADR 0003). Debounced: a burst of status flips
    reuses one pending job. Best-effort to enqueue — a status change must not fail if the queue
    insert does.

    AA-743 — scoped: a tour leaving the active set has its ranking/route rows superseded right
    away (no platform pass), and the debounced platform job re-lands questions only for the
    Segments this tour touched."""
    try:
        from services.export.handler import remove_tour_from_caches, tour_segment_ids
        from services.jobs.recompute_job import STATUS_DEBOUNCE_S, enqueue_recompute
        async with pool.acquire() as conn:
            if new_status != "active":
                await remove_tour_from_caches(conn, tour_id)
            segment_ids = await tour_segment_ids(conn, tour_id)
        job_id, created = await enqueue_recompute(
            pool, scope="platform", reason=f"master_status={new_status}:{tour_id}",
            created_by="admin:status_change", segment_ids=segment_ids, debounce_s=STATUS_DEBOUNCE_S)
        _log.info("aa713_recompute_enqueued", tour_id=tour_id, new_status=new_status,
                  job_id=job_id, created=created, segments=len(segment_ids))
    except Exception:
        _log.warning("aa713_recompute_enqueue_failed", tour_id=tour_id, exc_info=True)
    # AA-712 — the tour graph follows the live set; a tour (re-)entering also gets its places.
    try:
        from services.places.enqueue import after_liveness_change
        await after_liveness_change(pool, tour_id=str(tour_id), new_status=new_status,
                                    created_by="admin:status_change")
    except Exception:
        _log.warning("aa712_place_enqueue_failed", tour_id=tour_id, exc_info=True)


async def _notify_adopting_tenants(conn, tour_id: str, event_type, tour_name: str) -> None:
    """AA-716 — emit one tenant-facing notification per tenant that adopted this tour (its own
    rewrite in gold_aa_internal.tenant_tour_versions). Runs inside the caller's transaction so the
    warnings are atomic with the discontinuation. A tour with no adopting tenant emits nothing."""
    tenant_ids = await conn.fetch(
        """
        SELECT DISTINCT ttv.tenant_id
        FROM gold_aa_internal.tenant_tour_versions ttv
        JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
        WHERE pt.tour_id = $1::uuid
        """,
        tour_id,
    )
    svc = NotificationService(conn)
    for r in tenant_ids:
        await svc.emit(
            event_type=event_type,
            entity_type="tour",
            entity_id=tour_id,
            tenant_id=str(r["tenant_id"]),
            payload={"tour_name": tour_name, "changed_by": "admin"},
            actor_type="admin",
        )


PLAN_LIMITS = {
    # AA-640: tours per month now live ONLY in shared.membership_plans (what tenants see and are
    # billed on). This table keeps the per-plan rate limit.
    "starter":  {"rpm": 60},
    "growth":   {"rpm": 300},
    "business": {"rpm": 1000},
    "internal": {"rpm": 60},
}
# AA-766: upper bound for an admin RPM override — a technical guard against typos (10x the largest
# plan default), not a commercial limit.
RATE_LIMIT_RPM_MAX = 10000

# ── Auth guard ────────────────────────────────────────────────────────────────


def verify_admin_secret(x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin secret not configured")
    if not secrets_equal(x_admin_secret, ADMIN_SECRET):
        raise HTTPException(status_code=403, detail="Invalid admin secret")


# ── AA-797 tenant-write helpers ───────────────────────────────────────────────

def _require_tenant_admin(x_admin_role: Optional[str]) -> None:
    """Defense in depth behind the BFF rule (lib/rbac.ts API_WRITE_RULES): a write to a tenant
    account (create, plan, status, key, brand, config, offboard) is admin-role only. The BFF
    always forwards the verified role as x-admin-role; a direct call must send it too."""
    if x_admin_role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can change a tenant")


def _admin_actor(x_admin_user_id: Optional[str]) -> str:
    """audit_log.actor is varchar(64); the BFF sends the admin's UUID. A direct (non-FastAPI)
    call leaves the parameter as its Header() default, which is not a str."""
    user = x_admin_user_id if isinstance(x_admin_user_id, str) and x_admin_user_id else "unknown"
    return f"admin:{user}"[:64]


def tenant_status(is_active: bool, cancelled_at) -> str:
    """The three tenant states (AA-797): offboarded (cancelled_at set, key revoked; reversible only
    through POST /tenants/{id}/restore), suspended is a reversible is_active=false, everything
    else is active."""
    if cancelled_at is not None:
        return "offboarded"
    return "active" if is_active else "suspended"


async def _resolve_plan_id(conn, plan_tier: str):
    """shared.membership_plans.id for a plan_tier. A tenant without plan_id shows quota/price as
    0/inf (S234 audit), so a tier with no active plan row is refused instead of stored."""
    plan_id = await conn.fetchval(
        "SELECT id FROM shared.membership_plans WHERE plan_name = $1 AND is_active LIMIT 1",
        plan_tier,
    )
    if plan_id is None:
        raise HTTPException(status_code=400, detail=f"No active membership plan for plan_tier '{plan_tier}'")
    return plan_id


async def _forget_tenant_meta(request: Request, tenant_id) -> None:
    """Drop the rate-limit middleware's 30 s tenant_meta cache so a suspend/offboard blocks the
    tenant at once (api/middleware/rate_limit.py). Best effort: the TTL still expires it."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    try:
        await redis.delete(f"tenant_meta:{tenant_id}")
    except Exception:
        _log.warning("aa797_tenant_meta_delete_failed", tenant_id=str(tenant_id), exc_info=True)

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
    # S237: "synced" when the E2E test copy aa-cis/dev/e2e-tenant/<slug> was updated, "not_test_tenant",
    # "skipped" (not on ECS) or "failed" (services/acp_shared/e2e_tenant_key_sync.py).
    test_secret_sync: Optional[str] = None

# ── POST /admin/tenants — Create tenant ───────────────────────────────────────


@router.post("/tenants", response_model=CreateTenantResponse, summary="Create new tenant")
async def create_tenant(
    body: CreateTenantRequest,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    """AA-473 (ADR-2026-038 §0.2): Gate A removed entirely -- a new tenant is `is_active=true`
    immediately on creation, same as AA-472 already did for Gate B. This function itself never
    touched silver_aa_internal.raw_tours (no code to remove here) -- that "tenant brings its own
    tours" assumption lives only in list_tenants()/get_tenant_details() below, the old ACP v1
    shape N1 deliberately does not extend: a tenant's tour/atom selection is GET /v1/marketplace
    (AA-444, api/routers/v1_marketplace.py), never rows the tenant uploads under its own
    tenant_id."""
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)

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
        # AA-797: plan_id was never set here, so every new tenant showed quota/price 0/inf.
        plan_id = await _resolve_plan_id(conn, body.plan_tier)

        async with conn.transaction():
            tenant_id = await conn.fetchval("""
                INSERT INTO shared.tenants
                    (name, slug, plan_tier, plan_id, posts_per_week, api_key_hash, rate_limit_rpm, is_active)
                VALUES ($1, $2, $3::plan_tier_enum, $4, $5, $6, $7, true)
                RETURNING tenant_id
            """, body.name, body.slug, body.plan_tier, plan_id, body.posts_per_week, key_hash, rpm)

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
            await write_audit_log(
                conn, tenant_id=str(tenant_id), actor=_admin_actor(x_admin_user_id),
                action=AdminTenantAuditAction.ONBOARD, resource_type="tenant", resource_id=str(tenant_id),
                details={"name": body.name, "plan_tier": body.plan_tier, "plan_id": str(plan_id),
                         "posts_per_week": body.posts_per_week},
            )

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

async def _publish_limit_overrides(conn) -> dict:
    """AA-819 — {tenant_id: daily_publish_limit} for tenants with an override; {} before migration 230."""
    if not await daily_limit.override_column_exists(conn):
        return {}
    rows = await conn.fetch(
        "SELECT tenant_id, daily_publish_limit FROM shared.tenants WHERE daily_publish_limit IS NOT NULL")
    return {r["tenant_id"]: r["daily_publish_limit"] for r in rows}


# ── GET /admin/tenants — List all tenants + usage ────────────────────────────


@router.get("/tenants", summary="List all tenants with usage stats")
async def list_tenants(
    request: Request,
    x_admin_secret: str = Header(None),
    include_inactive: bool = Query(False, description="AA-797: also list suspended and offboarded tenants"),
):
    """AA-473: Gate A removed -- every tenant is `is_active=true` from creation, so this endpoint
    is back to a plain active-tenant list (`pending_tenants`/`gate_a_status` concept removed
    entirely; `is_active=false` now only means manually deactivated/offboarded, tracked
    separately if ever needed).

    AA-797: `include_inactive=true` (the /admin/tenants page) also returns suspended and
    offboarded tenants; each row carries a derived `status` (active/suspended/offboarded) and
    `cancelled_at`. Opt-in so the other callers (atom-curation, tenant-activity, the command
    palette) keep the active-only list.

    AA-472: `seeded`/`seeded_tour_count`/`angle_assigned` are removed from this response --
    acp_shared.tenant_atom_state is no longer part of the N1 onboarding flow (portfolio seeding
    and per-tenant angle assignment were both removed, Hướng B)."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                t.tenant_id, t.name, t.slug, t.plan_tier::text, t.posts_per_week,
                t.country, t.rate_limit_rpm, t.is_active, t.created_at, t.cancelled_at,
                COALESCE(u.api_calls_used, 0)          AS api_calls_used,
                COALESCE(u.quota_tours_pct, 0)         AS quota_tours_pct,
                COALESCE(u.quota_calls_pct, 0)         AS quota_calls_pct,
                COALESCE(u.tours_overage, 0)           AS tours_overage,
                COALESCE(u.overage_usd, 0)             AS overage_usd,
                COALESCE(u.llm_cost_usd, 0)            AS llm_cost_usd,
                -- AA-798: no COALESCE on plan columns; NULL = no plan attached (shown as such)
                u.tours_quota_monthly,
                u.api_calls_quota_monthly,
                u.price_usd_monthly,
                COALESCE(src.active, 0)     AS source_active,
                COALESCE(src.superseded, 0) AS source_superseded,
                COALESCE(src.trashed, 0)    AS source_trashed,
                COALESCE(mst.active, 0)     AS master_active,
                COALESCE(mst.inactive, 0)   AS master_inactive,
                COALESCE(mst.trashed, 0)    AS master_trashed
            FROM shared.tenants t
            LEFT JOIN shared.v_tenant_monthly_usage u
                ON u.tenant_id = t.tenant_id
            -- Count each table on its own: joining raw_tours AND published_tours onto the
            -- same tenant row multiplied them (source x published), e.g. 91,718 "source"
            -- tours for aa_internal instead of 121.
            LEFT JOIN (
                SELECT tenant_id,
                       COUNT(*) FILTER (WHERE source_status::text = 'active')     AS active,
                       COUNT(*) FILTER (WHERE source_status::text = 'superseded') AS superseded,
                       COUNT(*) FILTER (WHERE source_status::text = 'trashed')    AS trashed
                FROM silver_aa_internal.raw_tours GROUP BY tenant_id
            ) src ON src.tenant_id = t.tenant_id
            LEFT JOIN (
                SELECT tenant_id,
                       COUNT(*) FILTER (WHERE master_status::text = 'active')   AS active,
                       COUNT(*) FILTER (WHERE master_status::text = 'inactive') AS inactive,
                       COUNT(*) FILTER (WHERE master_status::text = 'trashed')  AS trashed
                FROM gold_aa_internal.published_tours GROUP BY tenant_id
            ) mst ON mst.tenant_id = t.tenant_id
            WHERE t.is_active = true OR $1::boolean
            ORDER BY t.created_at
        """, include_inactive)
        publish_limits = await _publish_limit_overrides(conn)
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
                # AA-819 — the publish/day override (null = the code default) and the limit in force
                "daily_publish_limit": publish_limits.get(r["tenant_id"]),
                "daily_publish_limit_effective": publish_limits.get(r["tenant_id"],
                                                                    daily_limit.DEFAULT_DAILY_PUBLISH_LIMIT),
                "is_active":      r["is_active"],
                "status":         tenant_status(r["is_active"], r["cancelled_at"]),
                "cancelled_at":   r["cancelled_at"].isoformat() if r["cancelled_at"] else None,
                "created_at":     r["created_at"].isoformat(),
                "plan_attached":  r["tours_quota_monthly"] is not None,  # AA-798
                "plan": {
                    "tours_quota_monthly":     r["tours_quota_monthly"],
                    "api_calls_quota_monthly": r["api_calls_quota_monthly"],
                    "price_usd_monthly": (
                        float(r["price_usd_monthly"]) if r["price_usd_monthly"] is not None else None
                    ),
                },
                "this_month": {
                    "tours_rewritten":  r["source_active"],
                    "api_calls_used":   r["api_calls_used"],
                    "quota_tours_pct":  float(r["quota_tours_pct"]),
                    "quota_calls_pct":  float(r["quota_calls_pct"]),
                    "tours_overage":    r["tours_overage"],
                    "overage_usd":      float(r["overage_usd"]),
                    # AA-798: the view's real llm_call_log month-to-date spend (migration 223)
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

# AA-825 (DM-7.4): raw shared.tenant_api_usage is kept 90 days; a month the retention job has rolled
# up (shared.tenant_api_usage_monthly, written from the complete raw month before any of it is
# deleted) is read from the rollup, every other month from the raw rows. Same numbers either way.
TENANT_USAGE_MONTHLY_SQL = """
    WITH rolled AS (
        SELECT (m.month::timestamp AT TIME ZONE 'UTC') AS month,
               sum(m.calls)::bigint              AS total_calls,
               sum(m.ok_calls)::bigint           AS successful_calls,
               sum(m.rate_limited_calls)::bigint AS rate_limited_calls,
               sum(m.response_ms_sum)::numeric / NULLIF(sum(m.response_ms_count), 0) AS avg_response_ms
        FROM shared.tenant_api_usage_monthly m
        WHERE m.tenant_id = $1
          AND m.month >= (DATE_TRUNC('month', NOW()) - make_interval(months => $2::int - 1))::date
        GROUP BY m.month
    ),
    raw AS (
        SELECT
            DATE_TRUNC('month', called_at) AS month,
            COUNT(*) AS total_calls,
            COUNT(*) FILTER (WHERE status_code < 400) AS successful_calls,
            COUNT(*) FILTER (WHERE status_code = 429) AS rate_limited_calls,
            AVG(response_ms) AS avg_response_ms
        FROM shared.tenant_api_usage
        WHERE tenant_id = $1
          AND called_at >= DATE_TRUNC('month', NOW()) - make_interval(months => $2::int - 1)
        GROUP BY 1
    )
    SELECT * FROM rolled
    UNION ALL
    SELECT * FROM raw WHERE raw.month NOT IN (SELECT month FROM rolled)
    ORDER BY 1 DESC
"""



@router.get("/tenants/{tenant_id}/usage", summary="Tenant billing metrics")
async def get_tenant_usage(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
    months: int = Query(3, ge=1, le=24),
):
    """AA-797: this 500'd for every tenant. It selected month/total_calls/... from
    shared.v_tenant_monthly_usage, which are the migration-007 column names; the live view was
    redefined by hand (billing_month/api_calls_used/..., see db-schema-reference.md, and AA-798
    for the baseline migration). The monthly series is now computed straight from
    shared.tenant_api_usage, whose columns match in repo and live. `tours_published` (always 0:
    tenants never own published_tours rows) is replaced by the tenant's T2 versions."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tenant = await conn.fetchrow("""
            SELECT t.name, t.slug, t.plan_tier::text AS plan_tier, t.plan_id, t.rate_limit_rpm,
                   t.is_active, t.cancelled_at
            FROM shared.tenants t WHERE t.tenant_id = $1
        """, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        usage = await conn.fetch(TENANT_USAGE_MONTHLY_SQL, tenant_id, months)

        t2 = await conn.fetchrow("""
            SELECT COUNT(*) AS versions, COUNT(DISTINCT published_tour_id) AS tours
            FROM gold_aa_internal.tenant_tour_versions WHERE tenant_id = $1
        """, tenant_id)
        # AA-797: quota by plan_id (what billing uses); plan_tier only for a row without one.
        tours_quota = await conn.fetchval("""
            SELECT tours_quota_monthly FROM shared.membership_plans
            WHERE id = $1 OR ($1::uuid IS NULL AND plan_name = $2)
            LIMIT 1
        """, tenant["plan_id"], tenant["plan_tier"])
        # AA-819 — the publish/day limit in force and today's count (UTC), as v1_publish counts it
        publish_override = await daily_limit.tenant_override(conn, tenant_id)
        publishes_today = await daily_limit.count_today(conn, tenant_id)

    return {
        "tenant_id":   str(tenant_id),
        "name":        tenant["name"],
        "slug":        tenant["slug"],
        "plan_tier":   tenant["plan_tier"],
        "plan_id":     str(tenant["plan_id"]) if tenant["plan_id"] else None,
        "status":      tenant_status(tenant["is_active"], tenant["cancelled_at"]),
        "limits": {
            "rate_limit_rpm":    tenant["rate_limit_rpm"],
            "tours_per_month":   tours_quota,  # AA-640: shared.membership_plans
            "daily_publish_limit": publish_override,  # AA-819: null = the code default
            "daily_publish_limit_effective": (publish_override if publish_override is not None
                                              else daily_limit.DEFAULT_DAILY_PUBLISH_LIMIT),
            "publishes_today": publishes_today,
        },
        "t2_versions": t2["versions"],
        "t2_tours":    t2["tours"],
        "monthly_usage": [
            {
                "month":               r["month"].strftime("%Y-%m"),
                "total_calls":         r["total_calls"],
                "successful_calls":    r["successful_calls"],
                "rate_limited_calls":  r["rate_limited_calls"],
                "avg_response_ms":     float(r["avg_response_ms"]) if r["avg_response_ms"] is not None else None,
            }
            for r in usage
        ],
    }

# ── PATCH /admin/tenants/{id} — Update plan/status ───────────────────────────


class UpdateTenantRequest(BaseModel):
    """AA-797: a JSON body. The route used to read these as query params while the UI sent a
    body, so the Active toggle returned 200 and changed nothing."""
    plan_tier: Optional[str] = None
    is_active: Optional[bool] = None
    # AA-766: an explicit per-tenant override of the plan's default RPM (PLAN_LIMITS). Sent alone
    # it changes only the limit; sent with plan_tier it wins over that plan's default.
    rate_limit_rpm: Optional[int] = Field(None, ge=1, le=RATE_LIMIT_RPM_MAX)
    # AA-819 (AUP 2026-11): per-tenant override of the daily publish limit (code default 20 per UTC
    # day). An explicit `null` clears the override (back to the default); leaving the key out keeps it.
    daily_publish_limit: Optional[int] = Field(None, ge=1, le=daily_limit.DAILY_PUBLISH_LIMIT_MAX)


@router.patch("/tenants/{tenant_id}", summary="Update tenant plan, rate limit, publish limit or status")
async def update_tenant(
    tenant_id: UUID,
    body: UpdateTenantRequest,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    """Plan change (plan_tier + plan_id + rate_limit_rpm together), an RPM override
    (rate_limit_rpm, AA-766), a daily publish limit override (daily_publish_limit, AA-819; `null`
    clears it) and/or suspend/resume (is_active). An offboarded tenant (cancelled_at set) gets 409
    here: it comes back only through POST /tenants/{id}/restore, which also issues a new key.
    Each change writes one audit row with before/after, in the same transaction. Before migration 230
    the publish limit override is refused with 409 (the code default applies to every tenant)."""
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    set_publish_limit = "daily_publish_limit" in body.model_fields_set
    if body.plan_tier is None and body.is_active is None and body.rate_limit_rpm is None and not set_publish_limit:
        raise HTTPException(status_code=422, detail="Nothing to update: send plan_tier, rate_limit_rpm, "
                                                    "daily_publish_limit and/or is_active")
    if body.plan_tier is not None and body.plan_tier not in PLAN_LIMITS:
        raise HTTPException(status_code=400, detail=f"Invalid plan_tier. Choose: {list(PLAN_LIMITS.keys())}")

    actor = _admin_actor(x_admin_user_id)
    changed: list[str] = []
    pool = request.app.state.pool
    if set_publish_limit:
        async with pool.acquire() as conn:
            if not await daily_limit.override_column_exists(conn):
                raise HTTPException(status_code=409, detail=(
                    "The per-tenant publish limit needs migration 230 (shared.tenants.daily_publish_limit); "
                    f"until it is applied every tenant has the default {daily_limit.DEFAULT_DAILY_PUBLISH_LIMIT}/day."))
    publish_limit_now: Optional[int] = None
    async with pool.acquire() as conn, conn.transaction():
        before = await conn.fetchrow("""
            SELECT plan_tier::text AS plan_tier, plan_id, rate_limit_rpm, is_active, cancelled_at
            FROM shared.tenants WHERE tenant_id = $1 FOR UPDATE
        """, tenant_id)
        if not before:
            raise HTTPException(status_code=404, detail="Tenant not found")
        if before["cancelled_at"] is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"Tenant was offboarded at {before['cancelled_at'].isoformat()}; "
                        "restore it first (POST /admin/tenants/{id}/restore)"),
            )

        rpm_now = before["rate_limit_rpm"]
        if body.plan_tier is not None and body.plan_tier != before["plan_tier"]:
            plan_id = await _resolve_plan_id(conn, body.plan_tier)
            rpm = body.rate_limit_rpm if body.rate_limit_rpm is not None else PLAN_LIMITS[body.plan_tier]["rpm"]
            rpm_now = rpm
            await conn.execute("""
                UPDATE shared.tenants
                SET plan_tier = $2::plan_tier_enum, plan_id = $3, rate_limit_rpm = $4, updated_at = NOW()
                WHERE tenant_id = $1
            """, tenant_id, body.plan_tier, plan_id, rpm)
            await write_audit_log(
                conn, tenant_id=str(tenant_id), actor=actor, action=AdminTenantAuditAction.PLAN_CHANGED,
                resource_type="tenant", resource_id=str(tenant_id),
                details={
                    "before": {"plan_tier": before["plan_tier"], "rate_limit_rpm": before["rate_limit_rpm"],
                               "plan_id": str(before["plan_id"]) if before["plan_id"] else None},
                    "after": {"plan_tier": body.plan_tier, "rate_limit_rpm": rpm, "plan_id": str(plan_id)},
                },
            )
            changed.append("plan")

        if body.rate_limit_rpm is not None and body.rate_limit_rpm != rpm_now:
            # AA-766 — RPM override without a plan change (or the same plan re-sent).
            await conn.execute("""
                UPDATE shared.tenants SET rate_limit_rpm = $2, updated_at = NOW() WHERE tenant_id = $1
            """, tenant_id, body.rate_limit_rpm)
            await write_audit_log(
                conn, tenant_id=str(tenant_id), actor=actor, action=AdminTenantAuditAction.RATE_LIMIT_CHANGED,
                resource_type="tenant", resource_id=str(tenant_id),
                details={"before": {"rate_limit_rpm": rpm_now}, "after": {"rate_limit_rpm": body.rate_limit_rpm}},
            )
            changed.append("rate_limit")

        if set_publish_limit:
            # AA-819 — the override (NULL = the code default), read here: the column exists (checked above)
            publish_limit_now = await conn.fetchval(
                "SELECT daily_publish_limit FROM shared.tenants WHERE tenant_id = $1", tenant_id)
            if body.daily_publish_limit != publish_limit_now:
                await conn.execute("""
                    UPDATE shared.tenants SET daily_publish_limit = $2, updated_at = NOW() WHERE tenant_id = $1
                """, tenant_id, body.daily_publish_limit)
                await write_audit_log(
                    conn, tenant_id=str(tenant_id), actor=actor,
                    action=AdminTenantAuditAction.DAILY_PUBLISH_LIMIT_CHANGED,
                    resource_type="tenant", resource_id=str(tenant_id),
                    details={"before": {"daily_publish_limit": publish_limit_now},
                             "after": {"daily_publish_limit": body.daily_publish_limit},
                             "default": daily_limit.DEFAULT_DAILY_PUBLISH_LIMIT},
                )
                changed.append("daily_publish_limit")
                publish_limit_now = body.daily_publish_limit

        if body.is_active is not None and body.is_active != before["is_active"]:
            await conn.execute("""
                UPDATE shared.tenants
                SET is_active = $2, updated_at = NOW()
                WHERE tenant_id = $1
            """, tenant_id, body.is_active)
            await write_audit_log(
                conn, tenant_id=str(tenant_id), actor=actor, action=AdminTenantAuditAction.STATUS_CHANGED,
                resource_type="tenant", resource_id=str(tenant_id),
                details={"before": tenant_status(before["is_active"], None),
                         "after": tenant_status(body.is_active, None)},
            )
            changed.append("status")

    if changed:
        await _forget_tenant_meta(request, tenant_id)
    is_active = before["is_active"] if body.is_active is None else body.is_active
    return {
        "status": "updated" if changed else "unchanged",
        "tenant_id": str(tenant_id),
        "changed": changed,
        "tenant_status": tenant_status(is_active, None),
        # AA-766: the values now stored, so the Settings editor can show the plan default RPM a
        # plan change applied without re-reading the tenant.
        "plan_tier": body.plan_tier if body.plan_tier is not None else before["plan_tier"],
        "rate_limit_rpm": body.rate_limit_rpm if body.rate_limit_rpm is not None else rpm_now,
        # AA-819 — only when the request set it (the override; null = the code default)
        **({"daily_publish_limit": publish_limit_now,
            "daily_publish_limit_effective": (publish_limit_now if publish_limit_now is not None
                                              else daily_limit.DEFAULT_DAILY_PUBLISH_LIMIT)}
           if set_publish_limit else {}),
    }

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


# AA-797: the bare DELETE /admin/tenants/{id} (is_active=false, no key revoke, no audit) was
# removed. Suspend is PATCH {"is_active": false}; leaving for good is POST /tenants/{id}/offboard.


# A run stored 'ingesting' with partial progress is only really "running" if it is recent — a batch
# from weeks ago cannot still be mid-pipeline (no job is that long-lived). Past this age a partial
# 'ingesting' run is stalled/abandoned, not running.
_RUN_STALE_AFTER = timedelta(hours=24)


def _derive_run_display_status(
    status: str, tours_total: int, tours_passed: int, started_at=None, now=None
) -> str:
    """AA-718 bug 3 — read-only honest display status for a pipeline run.

    `pipeline_runs.status` only ever advances to 'completed' when every tour in the batch reaches a
    terminal raw_tours.pipeline_status (services/export/handler.py::sync_batch_completion). Older
    ingest-only or abandoned batches sit at 'ingesting' forever, and the UI showed a misleading
    "ingesting"/"running". We derive a display label here without writing to the DB:
      - 'completed' / 'failed' → unchanged.
      - stored 'ingesting', all tours passed → 'completed' (really done).
      - stored 'ingesting', 0 passed with a total → 'ingested' (uploaded, not yet run).
      - stored 'ingesting', partial progress and OLDER than _RUN_STALE_AFTER → 'stalled'
        (started long ago, some tours stuck non-terminal — not actually running).
      - stored 'ingesting', partial progress and recent → 'running' (genuinely mid-pipeline).
    """
    if status in ("completed", "failed"):
        return status
    if status == "ingesting":
        if tours_total > 0 and tours_passed >= tours_total:
            return "completed"
        if tours_total > 0 and tours_passed == 0:
            return "ingested"
        # partial progress: running only if recent, else stalled
        if started_at is not None:
            ref = now or datetime.now(timezone.utc)
            try:
                if ref - started_at > _RUN_STALE_AFTER:
                    return "stalled"
            except TypeError:
                pass  # naive/aware mismatch — fall through to 'running'
        return "running"
    return status


# ── GET /admin/tenants/{id}/audit — AA-666 Tenant 360 Audit tab ─────────────


@router.get("/tenants/{tenant_id}/audit", summary="AA-666 — a tenant's action timeline")
async def get_tenant_audit(
    tenant_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    x_admin_secret: str = Header(None),
):
    """Per-tenant audit timeline for the Tenant 360 Audit tab (acp_shared.audit_log). Read-only;
    tenant_id is stored as text there. Newest first."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, actor, actor_type, resource_type, resource_id, details, created_at "
            "FROM acp_shared.audit_log WHERE tenant_id = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            str(tenant_id), limit,
        )
    return {"events": [{
        "action": r["action"],
        "actor": r["actor"],
        "actor_type": r["actor_type"],
        "resource_type": r["resource_type"],
        "resource_id": r["resource_id"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    } for r in rows]}


# ── GET /admin/tenants/{id}/details — 4-tab detail view ─────────────────────


@router.get("/tenants/{tenant_id}/details", summary="Tenant 4-tab detail view")
async def get_tenant_details(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
    # AA-718 — server-side paging/sort/filter for the internal Master Content catalog. All optional;
    # omitting them keeps the legacy behaviour (first page, published_at desc). The tenant (B2B)
    # branch ignores these and is unchanged.
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    sort: str = Query("created_at"),
    sort_dir: str = Query("desc"),
    country: Optional[str] = Query(None),
    master_status: Optional[str] = Query(None),
    score: Optional[str] = Query(None),  # 9.5+ / 9.0+ / 8.0+ / below8
    search: Optional[str] = Query(None),
    version: Optional[str] = Query(None),  # AA-739: v1 / v2+ / v3+ on the latest content version
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    # AA-718 — windowed LLM cost (reconcile with External Spend: rolling 30 days, UTC).
    _now = datetime.now(timezone.utc)
    _cost_since = _now - timedelta(days=30)

    # AA-718 — whitelist sortable columns (never interpolate a raw client string into ORDER BY).
    _SORT_COLS = {
        "tour_name": "pt.aa_name",
        "country": "rt.country",
        "quality_score": "pt.quality_score",
        "created_at": "pt.published_at",
        "master_status": "pt.master_status",
    }
    _sort_col = _SORT_COLS.get(sort, "pt.published_at")
    _sort_dir = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    _score_min = {"9.5+": 9.5, "9.0+": 9.0, "8.0+": 8.0}.get(score or "")
    _score_below8 = score == "below8"

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

        # AA-702: real LLM spend from shared.llm_call_log (same source as External Spend). The old
        # pipeline_runs sum only saw the legacy S1 batch path, so tenants showed $0.000. Internal
        # work is logged both as NULL and as the aa_internal UUID.
        total_cost = await conn.fetchval("""
            SELECT COALESCE(SUM(cost_usd), 0)
            FROM shared.llm_call_log
            WHERE tenant_id = $1 OR ($2 AND tenant_id IS NULL)
        """, tenant_id, is_internal)

        # AA-718 bug 2: a windowed cost (rolling 30d, UTC) that reconciles with External Spend's
        # own 30-day window, alongside the all-time figure above. External Spend buckets by
        # created_at in [now-30d, now) over the same llm_call_log — matching that here.
        cost_window = await conn.fetchval("""
            SELECT COALESCE(SUM(cost_usd), 0)
            FROM shared.llm_call_log
            WHERE (tenant_id = $1 OR ($2 AND tenant_id IS NULL))
              AND created_at >= $3 AND created_at < $4
        """, tenant_id, is_internal, _cost_since, _now)

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

        catalog_total = 0           # AA-718: true filtered COUNT (not len of a capped list)
        facet_countries: list = []  # AA-739
        catalog_avg_quality = None  # AA-718: avg over the SAME filter set as the table
        if is_internal:
            # AA-718 — server-side filter/sort/paginate the internal catalog (was ORDER BY
            # published_at DESC LIMIT 200, which silently dropped the newest rows past 200 and made
            # Total Tours / Avg Quality cap at 200). Filters are applied in SQL so the summary
            # stats below match exactly what the table shows.
            where = ["TRUE"]
            params: list = []
            if country:
                params.append(country)
                where.append(f"rt.country = ${len(params)}")
            if master_status and master_status != "all":
                params.append(master_status)
                where.append(f"pt.master_status::text = ${len(params)}")
            else:
                # default: hide trashed (matches the old client default)
                where.append("pt.master_status::text <> 'trashed'")
            if _score_min is not None:
                params.append(_score_min)
                where.append(f"pt.quality_score >= ${len(params)}")
            if _score_below8:
                where.append("pt.quality_score < 8.0")
            if search:
                params.append(f"%{search}%")
                where.append(f"(pt.aa_name ILIKE ${len(params)} OR rt.country ILIKE ${len(params)})")
            # AA-739: version used to be a client-side filter over the current page only.
            _ver_min = {"v1": None, "v2+": 2, "v3+": 3}.get(version or "")
            if version in ("v1", "v2+", "v3+"):
                _latest_ver = ("(SELECT gc.version_num FROM silver_aa_internal.generated_content gc "
                               "WHERE gc.tour_id = pt.tour_id ORDER BY gc.created_at DESC LIMIT 1)")
                where.append(f"{_latest_ver} = 1" if version == "v1" else f"{_latest_ver} >= {_ver_min}")
            where_sql = " AND ".join(where)

            # Filter-aware summary (bug 1 + bug 5): real COUNT and AVG over the filtered set.
            stat_row = await conn.fetchrow(f"""
                SELECT COUNT(*) AS n, AVG(pt.quality_score) AS avg_q
                FROM gold_aa_internal.published_tours pt
                LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
                WHERE {where_sql}
            """, *params)
            catalog_total = int(stat_row["n"] or 0)
            catalog_avg_quality = float(stat_row["avg_q"]) if stat_row["avg_q"] is not None else None
            # AA-739: every country in the catalog (non-trashed), not just the current page's — the
            # Country dropdown used to be built from the 20 rows on screen.
            facet_countries = [r["country"] for r in await conn.fetch("""
                SELECT DISTINCT rt.country
                FROM gold_aa_internal.published_tours pt
                JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
                WHERE pt.master_status::text <> 'trashed' AND rt.country IS NOT NULL
                ORDER BY rt.country
            """)]

            offset = (page - 1) * page_size
            tours = await conn.fetch(f"""
                SELECT pt.id, pt.tour_id, pt.aa_name, rt.country,
                       pt.quality_score, pt.master_status::text AS master_status,
                       (SELECT gc.version_num FROM silver_aa_internal.generated_content gc
                        WHERE gc.tour_id = pt.tour_id ORDER BY gc.created_at DESC LIMIT 1) AS version_number,
                       (SELECT COUNT(*) FROM silver_aa_internal.review_queue rq
                        WHERE rq.tour_id = pt.tour_id AND rq.review_status = 'pending')
                        AS pending_review_count,
                       'published'::text AS status, pt.published_at AS created_at
                FROM gold_aa_internal.published_tours pt
                LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
                WHERE {where_sql}
                ORDER BY {_sort_col} {_sort_dir} NULLS LAST, pt.id
                LIMIT {page_size} OFFSET {offset}
            """, *params)
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
                COALESCE(target_markets, ARRAY[]::text[]) AS target_markets,
                COALESCE(default_cta, '')        AS default_cta
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
            # AA-718 bug 2: windowed cost + label, reconciled with External Spend's 30d window.
            "llm_cost_window_usd":  float(cost_window or 0) if is_internal else None,
            "llm_cost_window_label": "last 30 days",
            # AA-718 bug 1 + 5: true filtered catalog count + filter-aware avg quality (internal).
            "catalog_total":        catalog_total if is_internal else int(total_rewrites or 0),
            "avg_quality":          catalog_avg_quality,
            "api_calls_this_month": api_calls,
            "quota_pct":            quota_pct,
            "plan_name":            str(tenant["plan_tier"]).title(),
            "member_since":         tenant["created_at"].isoformat()[:10],
            "tours_view":           "published" if is_internal else "rewrites",
            "pipeline_note":        None if is_internal else "Showing pipeline runs for tours in your catalog",
        },
        # AA-718 bug 4: server-side pagination metadata so the UI shows true totals, not a 200 cap.
        "pagination": {
            "page":       page,
            "page_size":  page_size,
            "total":      catalog_total if is_internal else int(total_rewrites or 0),
        },
        "facets": {"countries": facet_countries if is_internal else []},  # AA-739
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
                # AA-718 bug 3: an honest DISPLAY status derived read-only (never written back).
                # A run stored 'ingesting' can be: actually finished (all tours passed → show
                # Completed), freshly ingested but not yet run through S1 (0 passed → show Ingested),
                # or truly mid-run (partial → show Running). 'completed'/'failed' pass through.
                "display_status":  _derive_run_display_status(
                    r["status"], int(r["tours_total"] or 0), int(r["tours_passed"] or 0),
                    started_at=r["started_at"], now=_now,
                ),
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
            "default_cta":      brand["default_cta"]      if brand else "",
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
    default_cta:        Optional[str] = None  # AA-771 (S233, migration 220)


@router.put("/tenants/{tenant_id}/brand-identity")
async def update_tenant_brand_identity(
    tenant_id: UUID,
    body: TenantBrandIdentityUpdate,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    """Admin-side write for a SPECIFIC tenant's brand identity (AA-557 J.24) — same table, same
    columns, same NOT-scoped-by-brand_name "1 active row per tenant" invariant as
    admin_pipeline.py's tenant-facing `update_brand_identity()` (kept identical deliberately, see
    that function's own comment for why: avoids 2 simultaneously-active rows if brand_name
    changes mid-flow). Prompt-injection sanitized the same way (AA-487's `shared.validators.
    prompt_sanitize`) since these free-text fields feed straight into every future rewrite for
    this tenant, same as the tenant's own self-service path."""
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    tenant_id_s = str(tenant_id)

    # AA-741 (S229): same rules as the tenant's own path — reject over-long, never cut silently.
    too_long = brand_length_errors(body.model_dump())
    if too_long:
        raise HTTPException(status_code=422, detail="Some brand fields are too long — "
                            + "; ".join(too_long))
    system_prompt = sanitize_text(body.system_prompt, MAX_SYSTEM_PROMPT_LEN)
    style_guide = sanitize_text(body.writing_style or body.style_guide, MAX_BRAND_LONG_LEN)
    forbidden_words = sanitize_list(body.forbidden_words or [], MAX_SHORT_FIELD_LEN, max_items=20)
    brand_name = sanitize_text(body.brand_name, MAX_SHORT_FIELD_LEN) or "default"
    brand_type = sanitize_text(body.brand_type, MAX_SHORT_FIELD_LEN)
    core_idea = sanitize_text(body.core_idea, MAX_BRAND_MEDIUM_LEN)
    customer_segment = sanitize_text(body.customer_segment, MAX_BRAND_MEDIUM_LEN)
    customer_mindset = sanitize_text(body.customer_mindset, MAX_BRAND_MEDIUM_LEN)
    tone_of_voice = sanitize_list(body.tone_of_voice or [], MAX_SHORT_FIELD_LEN, max_items=20)
    good_examples = sanitize_text(body.good_examples, MAX_BRAND_LONG_LEN)
    target_markets = sanitize_list(body.target_markets or [], MAX_SHORT_FIELD_LEN, max_items=20)
    default_cta = sanitize_text(body.default_cta, MAX_BRAND_MEDIUM_LEN)
    from services.brand_intake.markets import target_market_json
    market_json = target_market_json(target_markets)

    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
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
                 forbidden_words, target_markets, version, is_active, updated_at, default_cta)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11::jsonb, $12, $13, true, NOW(), $14)
        """, tenant_id_s, brand_name, brand_type, core_idea, customer_segment, customer_mindset,
            json.dumps(tone_of_voice), style_guide, good_examples, system_prompt,
            json.dumps(forbidden_words), target_markets, current + 1, default_cta)
        if tenant_id_s != "00000000-0000-0000-0000-000000000001":
            # AA-741 (S229): the brand's markets drive T2/T7 (tenant_seo_config.target_market).
            await conn.execute("""
                INSERT INTO shared.tenant_seo_config (tenant_id, target_market)
                VALUES ($1::uuid, $2::jsonb)
                ON CONFLICT (tenant_id) DO UPDATE
                    SET target_market = EXCLUDED.target_market, updated_at = now()
            """, tenant_id_s, json.dumps(market_json))
        # AA-797: the rules table keeps every version, so the audit row names the version and the
        # fields that were set rather than copying the prompt text.
        await write_audit_log(
            conn, tenant_id=tenant_id_s, actor=_admin_actor(x_admin_user_id),
            action=AdminTenantAuditAction.BRAND_UPDATED, resource_type="tenant_brand_rules",
            resource_id=tenant_id_s,
            details={"version": current + 1, "brand_name": brand_name,
                     "fields_set": sorted(k for k, v in body.model_dump().items() if v)},
        )
    return {"status": "updated", "version": current + 1,
            "markets": {k: market_json[k] for k in ("countries", "unsupported", "unknown")}}


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
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT tenant_id, name, slug, cancelled_at FROM shared.tenants WHERE tenant_id = $1 FOR UPDATE",
            tenant_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Tenant not found")
        # AA-797: an offboarded tenant's key stays revoked; restore issues the new key.
        if row["cancelled_at"] is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"Tenant was offboarded at {row['cancelled_at'].isoformat()}; restore it to issue a "
                        "new API key (POST /admin/tenants/{id}/restore)"),
            )
        plaintext = f"cis_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        await conn.execute("""
            UPDATE shared.tenants
            SET api_key_hash = $1, updated_at = NOW()
            WHERE tenant_id = $2
        """, key_hash, tenant_id)
        await write_audit_log(
            conn, tenant_id=str(tenant_id), actor=_admin_actor(x_admin_user_id),
            action=AdminTenantAuditAction.KEY_ROTATED, resource_type="tenant", resource_id=str(tenant_id),
            details={"name": row["name"]},
        )

    # S237: after the commit, so a rolled-back rotation never overwrites the test copy.
    sync = await sync_test_tenant_key(row.get("slug"), plaintext)
    return GenerateKeyResponse(
        tenant_id=str(row["tenant_id"]),
        tenant_name=row["name"],
        api_key=plaintext,
        message="Store this key securely — it will not be shown again." + sync_note(sync, row.get("slug")),
        test_secret_sync=sync,
    )


# AA-750: POST /admin/tenants/{id}/brand-brief (DOCX upload → acc1 Lambda acp-brand-brief-parser) was
# removed — it pointed at acc1 resources destroyed in AA-271 and no page called it. Tenants edit the
# brand in BrandTab → /brand-identity (sanitized in-process, shared/validators/prompt_sanitize.py).


# ── POST /admin/tenants/{id}/offboard — GDPR offboard ────────────────────────

class OffboardRequest(BaseModel):
    reason: str


@router.post("/tenants/{tenant_id}/offboard", summary="GDPR offboarding — cancel tenant + revoke key")
async def offboard_tenant(
    tenant_id: str,
    body: OffboardRequest,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    """
    GDPR offboarding: cancel tenant, revoke API key, log for data deletion. PRD v1.0 §3.2.
    Soft state only (AA-797, Nghiệp 14/10/2026): no tenant data or setting is deleted or
    overwritten (brand rules, planning config, T2 versions, content, markets, plan_id, users), so
    POST /tenants/{id}/restore brings the tenant back whole. PATCH / generate-key on an
    offboarded tenant return 409. The tenant's QUEUED jobs are cancelled (running ones finish).
    The Jev allow-list row is removed (AA-741) but snapshotted into the audit row so restore can
    put it back. GDPR data deletion is a separate follow-up, not automated here.
    """
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required for GDPR audit trail")

    try:
        UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid tenant_id UUID")
    if tenant_id == AA_INTERNAL_TENANT:
        raise HTTPException(status_code=400, detail="The AA internal tenant cannot be offboarded")

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
            # AA-797: the allow-list row is a setting; keep a copy so restore can re-add it.
            allow = await conn.fetchrow(
                "SELECT reason, added_at, added_by FROM shared.jev_tenant_allowlist WHERE tenant_id = $1::uuid",
                tenant_id,
            )
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
            # AA-797: the queued branch of shared.jobs.queue.request_cancel(), scoped to this
            # tenant. Tenant job kinds (t2_rewrite, t7_slate, t8_angles, t9_write) carry tenant_id
            # at the top of the payload. `status = 'queued'` is re-checked on the locked row, so a
            # job a worker claims meanwhile is left running.
            cancelled = await conn.fetch(
                """
                UPDATE shared.job SET
                    status = 'cancelled', finished_at = now(), error = 'cancelled: tenant offboarded',
                    cancel_requested = true, updated_at = now()
                WHERE status = 'queued' AND payload->>'tenant_id' = $1
                RETURNING id::text AS id, kind
                """,
                tenant_id,
            )
            await write_audit_log(
                conn, tenant_id=tenant_id, actor=_admin_actor(x_admin_user_id),
                action=AdminTenantAuditAction.OFFBOARD, resource_type="tenant", resource_id=tenant_id,
                details={
                    "reason": body.reason,
                    "name": tenant["name"],
                    "cancelled_jobs": [{"id": r["id"], "kind": r["kind"]} for r in cancelled],
                    "jev_allowlist": ({"reason": allow["reason"], "added_at": allow["added_at"].isoformat(),
                                       "added_by": allow["added_by"]} if allow else None),
                    "note": "Nothing is deleted; POST /admin/tenants/{id}/restore brings the tenant back",
                },
            )
            # AA-741 (S229): an offboarded tenant's content must never reach Jev again.
            await conn.execute(
                "DELETE FROM shared.jev_tenant_allowlist WHERE tenant_id = $1::uuid", tenant_id)

    # Same follow-up as POST /admin/job-runner/jobs/{id}/cancel: a cancelled job's domain row
    # (T2 version / T9 piece) is marked failed by its kind's on_terminal hook. Never raises.
    from shared.jobs.registry import run_terminal_hook
    for r in cancelled:
        await run_terminal_hook(pool, r["id"])
    await _forget_tenant_meta(request, tenant_id)

    return {
        "tenant_id": tenant_id,
        "status": "offboarded",
        "api_key": "REVOKED",
        "cancelled_jobs": len(cancelled),
        "note": "Tenant data and settings are kept; POST /admin/tenants/{id}/restore brings the tenant back.",
    }


# ── POST /admin/tenants/{id}/restore — undo an offboard (AA-797) ─────────────

class RestoreTenantResponse(BaseModel):
    tenant_id: str
    tenant_name: str
    status: str
    api_key: str
    previous_offboarded_at: str
    previous_reason: Optional[str]
    jev_allowlist_restored: bool
    cancelled_jobs: list[dict]
    message: str
    test_secret_sync: Optional[str] = None  # S237, see GenerateKeyResponse


@router.post("/tenants/{tenant_id}/restore", response_model=RestoreTenantResponse,
             summary="AA-797 — restore an offboarded tenant with its data and settings")
async def restore_tenant(
    tenant_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    """Nghiệp (14/10/2026): an offboarded tenant must come back quickly with its data and settings.
    Offboard deletes nothing, so restore only clears the offboard markers (cancelled_at,
    cancellation_reason), sets is_active, re-adds the Jev allow-list row offboard snapshotted, and
    issues a NEW API key (the old one stays revoked). plan_tier / plan_id / rate limit and every
    setting are left exactly as they were. Jobs offboard cancelled are NOT re-enqueued; they are
    listed so the admin can re-run them. 409 unless the tenant is offboarded."""
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    tid = str(tenant_id)
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("""
            SELECT tenant_id, name, slug, is_active, cancelled_at, cancellation_reason
            FROM shared.tenants WHERE tenant_id = $1 FOR UPDATE
        """, tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="Tenant not found")
        if row["cancelled_at"] is None:
            raise HTTPException(
                status_code=409,
                detail=(f"Tenant is {tenant_status(row['is_active'], None)}, not offboarded; "
                        "only an offboarded tenant can be restored"),
            )
        offboarded_at = row["cancelled_at"]

        # The jobs this offboard cancelled (same transaction set finished_at = cancelled_at).
        jobs = await conn.fetch("""
            SELECT id::text AS id, kind FROM shared.job
            WHERE payload->>'tenant_id' = $1 AND status = 'cancelled'
              AND error = 'cancelled: tenant offboarded' AND finished_at >= $2
            ORDER BY created_at
        """, tid, offboarded_at)
        cancelled_jobs = [{"id": j["id"], "kind": j["kind"]} for j in jobs]

        # Re-add the Jev allow-list row from the latest offboard audit snapshot (AA-741 removed it).
        details = await conn.fetchval("""
            SELECT details FROM acp_shared.audit_log
            WHERE tenant_id = $1 AND action = $2 ORDER BY created_at DESC LIMIT 1
        """, tid, AdminTenantAuditAction.OFFBOARD)
        if isinstance(details, str):
            details = json.loads(details)
        allow = (details or {}).get("jev_allowlist")
        allow_restored = False
        if allow:
            inserted = await conn.execute("""
                INSERT INTO shared.jev_tenant_allowlist (tenant_id, reason, added_at, added_by)
                VALUES ($1, $2, $3::timestamptz, $4) ON CONFLICT (tenant_id) DO NOTHING
            """, tenant_id, allow["reason"], datetime.fromisoformat(allow["added_at"]), allow["added_by"])
            allow_restored = inserted.endswith(" 1")

        plaintext = f"cis_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        await conn.execute("""
            UPDATE shared.tenants
            SET cancelled_at = NULL, cancellation_reason = NULL, is_active = TRUE,
                api_key_hash = $2, updated_at = NOW()
            WHERE tenant_id = $1
        """, tenant_id, key_hash)
        await write_audit_log(
            conn, tenant_id=tid, actor=_admin_actor(x_admin_user_id),
            action=AdminTenantAuditAction.RESTORED, resource_type="tenant", resource_id=tid,
            details={
                "name": row["name"],
                "previous_offboarded_at": offboarded_at.isoformat(),
                "previous_reason": row["cancellation_reason"],
                "restored": ["status:active", "api_key:new"] + (["jev_allowlist"] if allow_restored else []),
                "unchanged": "plan_tier, plan_id, rate limit, brand rules, planning config, content, users",
                "cancelled_jobs_not_rerun": cancelled_jobs,
            },
        )

    await _forget_tenant_meta(request, tenant_id)
    sync = await sync_test_tenant_key(row.get("slug"), plaintext)  # S237, after the commit
    return RestoreTenantResponse(
        tenant_id=tid,
        tenant_name=row["name"],
        status="active",
        api_key=plaintext,
        previous_offboarded_at=offboarded_at.isoformat(),
        previous_reason=row["cancellation_reason"],
        jev_allowlist_restored=allow_restored,
        cancelled_jobs=cancelled_jobs,
        message=("Store this key securely — it will not be shown again. "
                 f"{len(cancelled_jobs)} job(s) cancelled by the offboard were not re-run."
                 + sync_note(sync, row.get("slug"))),
        test_secret_sync=sync,
    )


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
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    invalid = [c for c in body.channels if c not in _VALID_CHANNELS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid channel(s): {invalid} — must be one of {sorted(_VALID_CHANNELS)}",
        )
    pool = request.app.state.pool
    try:
        before = await fetch_tenant_planning_config(tenant_id, pool)
        await save_tenant_planning_config(
            tenant_id, body.markets, body.channels, body.posts_per_week, pool,
        )
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
    # AA-797: written after the save commits (the config helper owns its own connection).
    await write_audit_log(
        pool, tenant_id=str(tenant_id), actor=_admin_actor(x_admin_user_id),
        action=AdminTenantAuditAction.CONFIG_UPDATED, resource_type="tenant_config",
        resource_id=str(tenant_id),
        details={
            "before": {"markets": before.markets, "channels": before.channels,
                       "posts_per_week": before.capacity_posts_per_week},
            "after": {"markets": body.markets, "channels": body.channels,
                      "posts_per_week": body.posts_per_week},
        },
    )
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

    await _recompute_after_status_change(pool, tour_id, status)   # AA-713
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

            # AA-716 — cascade the discontinuation. AA-713 wired master_status -> atoms; this adds
            # the missing upstream link raw_tours -> master. All in the same transaction so the
            # trash is atomic; the Score/Route recompute runs after commit (reads committed state).
            # 1. Inactivate the Master Content tour (if one was published). v_active_tour_atoms
            #    (migration 203) gates on master_status='active', so this drops the atoms from the
            #    view immediately; the post-commit recompute rebuilds the caches that held them.
            master_inactivated = await conn.execute(
                """
                UPDATE gold_aa_internal.published_tours
                SET master_status = 'inactive'::gold_aa_internal.master_status_enum
                WHERE tour_id = $1::uuid AND master_status = 'active'
                """,
                tour_id,
            )
            # 2. Dismiss any pending review_queue rows for this tour — a discontinued source tour
            #    must not stay an open review item (AA-653 found live S212). Same SQL discipline.
            await conn.execute(
                """
                UPDATE silver_aa_internal.review_queue
                SET review_status = 'dismissed'::review_status_enum, reviewed_at = NOW(),
                    reviewer_notes = 'AA-716: source tour discontinued'
                WHERE tour_id = $1::uuid AND review_status = 'pending'
                """,
                tour_id,
            )
            # 3. Admin/content-facing event (unchanged).
            await NotificationService(conn).emit(
                event_type=EventType.SOURCE_TRASHED,
                entity_type="tour",
                entity_id=tour_id,
                tenant_id=str(row["tenant_id"]),
                payload={"tour_name": row["src_name"], "changed_by": "admin"},
                actor_type="admin",
            )
            # 4. Warn every tenant that adopted this tour (tenant_tour_versions) — one tenant-facing
            #    notification each. Their work with the OTHER active tours is untouched.
            await _notify_adopting_tenants(
                conn, tour_id, EventType.TOUR_DISCONTINUED, row["src_name"])

    # AA-713 chain — recompute Score/Route platform-wide on the remaining active tours (after commit).
    await _recompute_after_status_change(pool, tour_id, "trashed")

    return {
        "tour_id": tour_id,
        "source_status": "trashed",
        "master_inactivated": master_inactivated != "UPDATE 0",
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

            # AA-716 — reverse the cascade. Bring the Master Content tour back to active (only the
            # rows this cascade had inactivated; a tour an admin deliberately trashed at the master
            # level stays trashed). The post-commit recompute re-admits its atoms to Score/Route.
            master_reactivated = await conn.execute(
                """
                UPDATE gold_aa_internal.published_tours
                SET master_status = 'active'::gold_aa_internal.master_status_enum
                WHERE tour_id = $1::uuid AND master_status = 'inactive'
                """,
                tour_id,
            )
            # Dismissed review_queue rows are NOT auto-reopened — dismissal is one-way, same as the
            # publish-based dismiss (AA-653). A reinstated tour re-enters the normal pipeline fresh.
            await NotificationService(conn).emit(
                event_type=EventType.SOURCE_RESTORED,
                entity_type="tour",
                entity_id=tour_id,
                tenant_id=str(row["tenant_id"]),
                payload={"tour_name": row["src_name"], "changed_by": "admin"},
                actor_type="admin",
            )
            # Tell adopting tenants the tour is back.
            await _notify_adopting_tenants(
                conn, tour_id, EventType.TOUR_REINSTATED, row["src_name"])

    # AA-713 chain — recompute so the restored tour's atoms re-enter Score/Route (after commit).
    await _recompute_after_status_change(pool, tour_id, "inactive")

    return {
        "tour_id": tour_id,
        "source_status": "active",
        "master_reactivated": master_reactivated != "UPDATE 0",
    }


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

    await _recompute_after_status_change(pool, tour_id, "trashed")   # AA-713
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

    await _recompute_after_status_change(pool, tour_id, "inactive")   # AA-713 (restore -> inactive)
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

    await _recompute_after_status_change(pool, tour_id, "active")   # AA-713
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

    await _recompute_after_status_change(pool, tour_id, "inactive")   # AA-713 (deactivate)
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
        "tenant.cta.review_claim":  "Tenant CTA mentions reviews or ratings",  # AA-819 follow-up
    }

    items = []
    for r in rows:
        item = dict(r)
        item["id"] = int(item["id"])
        # AA-702: the pool has no jsonb codec, so payload arrives as a JSON string — dict() on it
        # raised and the whole list 500'd while the unread count still showed on the bell.
        raw = item["payload"]
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
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
