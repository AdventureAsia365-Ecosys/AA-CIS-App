# api/routers/admin_pipeline/billing.py — the admin billing route.
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

from fastapi import Header, HTTPException, Request
from api.routers._billing import usage_row_to_dict
from api.routers.admin import verify_admin_secret

from api.routers.admin_pipeline._shared import router


# ── GET /admin/billing ────────────────────────────────────────────────────────

@router.get("/billing")
async def get_admin_billing(
    request: Request,
    x_admin_secret: str = Header(None),
    tenant_id: str = "00000000-0000-0000-0000-000000000001",
):
    """Admin billing view — defaults to aa_internal; pass ?tenant_id= for a specific tenant.

    AA-798: ``llm_cost_usd`` is the view's column, i.e. real month-to-date
    ``SUM(shared.llm_call_log.cost_usd)`` for the tenant (migration 223). An unknown tenant is a
    404 (it used to get an invented Starter plan); a tenant without a plan gets
    ``plan_attached: false`` and null quotas/price (see ``_billing.usage_row_to_dict``)."""
    verify_admin_secret(x_admin_secret)
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

    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return {
        **usage_row_to_dict(row),
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
