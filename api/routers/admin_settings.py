# api/routers/admin_settings.py
# AA-158: Admin Settings — GET /admin/settings, PATCH /admin/settings/seo
import json
import os
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/admin", tags=["admin-settings"])

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
AA_INTERNAL_TENANT_ID = "00000000-0000-0000-0000-000000000001"

PIPELINE_GATES = {
    "brand_audit_threshold": 7.0,
    "dedup_key": "lower(trim(src_name)) + lower(trim(provider))",
    # AA-454: was missing "llm_judge" (validate->llm_judge->brand_audit) and "revalidate"
    # (flag_fix->revalidate->END) — real edge order per services/content_generation/graph.py
    # ::build_graph(). increment_retry/hitl aren't included: they're retry-loop control nodes,
    # not part of the linear happy-path sequence this chip displays.
    "pipeline_flow": ["generate", "validate", "llm_judge", "brand_audit", "grounding", "flag_fix", "revalidate"],
}


def verify_admin(x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin secret not configured")
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")


class SeoConfigPatch(BaseModel):
    custom_keywords: Optional[list] = None
    target_market: Optional[dict] = None
    overrides: Optional[dict] = None


# ── GET /admin/settings ───────────────────────────────────────────────────────


@router.get("/settings", summary="Admin settings for aa_internal tenant")
async def get_settings(
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin(x_admin_secret)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        tenant_row = await conn.fetchrow("""
            SELECT
                t.tenant_id::text,
                t.name,
                t.slug,
                t.plan_tier::text,
                t.is_active,
                COALESCE(u.tours_quota_monthly, 0)  AS tours_quota_monthly,
                COALESCE(u.price_usd_monthly, 0.0)  AS price_usd_monthly
            FROM shared.tenants t
            LEFT JOIN shared.v_tenant_monthly_usage u ON u.tenant_id = t.tenant_id
            WHERE t.tenant_id = $1::uuid
        """, AA_INTERNAL_TENANT_ID)

        if not tenant_row:
            raise HTTPException(status_code=404, detail="aa_internal tenant not found")

        brand_row = await conn.fetchrow("""
            SELECT
                system_prompt,
                style_guide,
                forbidden_words,
                version,
                is_active,
                updated_at
            FROM shared.tenant_brand_rules
            WHERE tenant_id = $1 AND is_active = true
            ORDER BY version DESC
            LIMIT 1
        """, AA_INTERNAL_TENANT_ID)

        seo_row = await conn.fetchrow("""
            SELECT seo_provider, custom_keywords, target_market, overrides, updated_at
            FROM shared.tenant_seo_config
            WHERE tenant_id = $1
        """, AA_INTERNAL_TENANT_ID)

    def _trunc(text: Any, n: int = 200) -> Optional[str]:
        if not text:
            return None
        return text[:n] + ("…" if len(text) > n else "")

    def _jsonb(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return val
        return val

    brand: dict = {}
    if brand_row:
        brand = {
            "system_prompt":   _trunc(brand_row["system_prompt"], 200),
            "style_guide":     _trunc(brand_row["style_guide"], 200),
            "style_guide_full": brand_row["style_guide"],
            "forbidden_words": _jsonb(brand_row["forbidden_words"]) or [],
            "version":         brand_row["version"],
            "is_active":       brand_row["is_active"],
            "updated_at":      brand_row["updated_at"].isoformat() if brand_row["updated_at"] else None,
        }

    seo: dict = {}
    if seo_row:
        seo = {
            "seo_provider":    seo_row["seo_provider"],
            "custom_keywords": _jsonb(seo_row["custom_keywords"]) or [],
            "target_market":   _jsonb(seo_row["target_market"]) or {},
            "overrides":       _jsonb(seo_row["overrides"]) or {},
            "updated_at":      seo_row["updated_at"].isoformat() if seo_row["updated_at"] else None,
        }

    return {
        "tenant": {
            "tenant_id":  tenant_row["tenant_id"],
            "name":       tenant_row["name"],
            "slug":       tenant_row["slug"],
            "plan_tier":  tenant_row["plan_tier"],
            "is_active":  tenant_row["is_active"],
        },
        "plan": {
            "tours_quota_monthly": tenant_row["tours_quota_monthly"],
            "price_usd_monthly":   float(tenant_row["price_usd_monthly"]),
            "trash_retention_days": 30,
        },
        "brand_rules": brand,
        "seo_config":  seo,
        "pipeline_gates": PIPELINE_GATES,
    }


# ── PATCH /admin/settings/seo ─────────────────────────────────────────────────


@router.patch("/settings/seo", summary="Update SEO config for aa_internal tenant")
async def patch_seo_config(
    body: SeoConfigPatch,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin(x_admin_secret)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM shared.tenant_seo_config WHERE tenant_id = $1",
            AA_INTERNAL_TENANT_ID,
        )

        if not existing:
            await conn.execute(
                "INSERT INTO shared.tenant_seo_config"
                " (tenant_id, custom_keywords, target_market, overrides)"
                " VALUES ($1, $2::jsonb, $3::jsonb, $4::jsonb)",
                AA_INTERNAL_TENANT_ID,
                json.dumps(body.custom_keywords or []),
                json.dumps(body.target_market or {}),
                json.dumps(body.overrides or {}),
            )
        else:
            sets = []
            params: list = []
            idx = 1
            if body.custom_keywords is not None:
                sets.append(f"custom_keywords = ${idx}::jsonb")
                params.append(json.dumps(body.custom_keywords))
                idx += 1
            if body.target_market is not None:
                sets.append(f"target_market = ${idx}::jsonb")
                params.append(json.dumps(body.target_market))
                idx += 1
            if body.overrides is not None:
                sets.append(f"overrides = ${idx}::jsonb")
                params.append(json.dumps(body.overrides))
                idx += 1
            if sets:
                sets.append("updated_at = now()")
                params.append(AA_INTERNAL_TENANT_ID)
                await conn.execute(
                    f"UPDATE shared.tenant_seo_config SET {', '.join(sets)} WHERE tenant_id = ${idx}",
                    *params,
                )

        row = await conn.fetchrow("""
            SELECT seo_provider, custom_keywords, target_market, overrides, updated_at
            FROM shared.tenant_seo_config WHERE tenant_id = $1
        """, AA_INTERNAL_TENANT_ID)

    def _jsonb(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return val
        return val

    return {
        "seo_provider":    row["seo_provider"],
        "custom_keywords": _jsonb(row["custom_keywords"]) or [],
        "target_market":   _jsonb(row["target_market"]) or {},
        "overrides":       _jsonb(row["overrides"]) or {},
        "updated_at":      row["updated_at"].isoformat() if row["updated_at"] else None,
    }
