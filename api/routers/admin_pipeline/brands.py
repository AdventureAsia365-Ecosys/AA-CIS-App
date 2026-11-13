# api/routers/admin_pipeline/brands.py — brand rule routes (/admin/brands).
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import base64
import json
from fastapi import Header, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, List
from api.routers.admin import verify_admin_secret
from shared.validators.prompt_sanitize import brand_length_errors

from api.routers.admin_pipeline._shared import router


# ── /admin/brands — multi-brand CRUD ─────────────────────────────────────────


class ParseDocxRequest(BaseModel):
    file_base64: str
    filename: str = "brand.docx"


class BrandCreateRequest(BaseModel):
    brand_name: str
    brand_type: Optional[str] = None
    core_idea: Optional[str] = None
    customer_segment: Optional[str] = None
    customer_mindset: Optional[str] = None
    tone_of_voice: Optional[List[str]] = None
    writing_style: Optional[str] = None
    good_examples: Optional[str] = None
    should_write: Optional[str] = None
    forbidden_words: Optional[List[str]] = None
    target_markets: Optional[List[str]] = None
    rewrite_language: str = "en"


def _parse_fw(fw):
    if fw is None:
        return []
    if isinstance(fw, list):
        return fw
    try:
        return json.loads(fw)
    except Exception:
        return []


def _parse_jsonb_list(val) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


@router.get("/brands")
async def list_brands(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    tenant_id = "00000000-0000-0000-0000-000000000001"
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT ON (COALESCE(brand_name, 'default'))
                    id, brand_name, brand_type, core_idea, version, is_active, updated_at
                FROM shared.tenant_brand_rules
                WHERE tenant_id = $1 AND is_active = true
                ORDER BY COALESCE(brand_name, 'default'), version DESC
            """, tenant_id)
    except Exception as e:
        if "brand_name" in str(e).lower() or "column" in str(e).lower():
            raise HTTPException(
                status_code=503,
                detail="Migration 043 not applied — run 043_add_brand_name_to_brand_rules.sql first",
            )
        raise
    return {"brands": [
        {
            "brand_name":  r["brand_name"] or "default",
            "brand_type":  r["brand_type"],
            "core_idea":   r["core_idea"],
            "version":     r["version"],
            "is_active":   r["is_active"],
            "updated_at":  r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]}


@router.post("/brands/parse-docx")
async def parse_brand_docx(
    body: ParseDocxRequest,
    x_admin_secret: str = Header(None),
):
    """Parse a brand brief DOCX (base64-encoded JSON) and return pre-filled brand fields.
    AA-741 (S229): same parser as the tenant upload (services/brand_intake/brief.py) — the old
    inline parser folded "Should Not Write" into "Should Write"; one parser, never two."""
    verify_admin_secret(x_admin_secret)
    try:
        content = base64.b64decode(body.file_base64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")
    out = _parse_brief_or_400(content)
    return {"parsed": out["fields"], "warnings": out["warnings"], "markets": out["markets"],
            "drafted": out.get("drafted", []), "unmapped_fields": out.get("unmapped_fields", []),
            "filename": body.filename}


def _parse_brief_or_400(content: bytes) -> dict:
    from services.brand_intake.brief import parse_brief_docx
    from services.brand_intake.markets import normalize_markets
    try:
        out = parse_brief_docx(content)
    except ImportError:
        raise HTTPException(status_code=501, detail="python-docx not installed on this server")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read this .docx file: {e}")
    from services.brand_intake.markets import market_summary
    m = normalize_markets(out["fields"].get("target_markets"))
    out["markets"] = {k: m[k] for k in ("supported", "unsupported", "unknown")}
    # AA-1080: the review screen names the market T2 keywords come from and why each unsupported one has none.
    summary = market_summary(out["fields"].get("target_markets"))
    out["markets"].update({k: summary[k] for k in ("seo_market", "notes")})
    too_long = brand_length_errors(out["fields"])
    if too_long:
        out["warnings"].append("Some sections are longer than the form allows — shorten them before "
                               "saving: " + "; ".join(too_long))
    return out


@router.get("/brands/{brand_name}")
async def get_brand(brand_name: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    tenant_id = "00000000-0000-0000-0000-000000000001"
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, brand_name, brand_type, core_idea,
                   customer_segment, customer_mindset, voice_examples,
                   style_guide, good_examples, system_prompt, forbidden_words,
                   target_markets, rewrite_language,
                   version, is_active, updated_at, created_at
            FROM shared.tenant_brand_rules
            WHERE tenant_id = $1 AND COALESCE(brand_name, 'default') = $2
            ORDER BY version DESC
        """, tenant_id, brand_name)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_name}' not found")
    current = rows[0]
    voice = _parse_jsonb_list(current["voice_examples"]) if current["voice_examples"] else []
    history = [
        {
            "version":          r["version"],
            "is_active":        r["is_active"],
            "updated_at":       r["updated_at"].isoformat() if r["updated_at"] else None,
            "brand_type":       r["brand_type"] or "",
            "core_idea":        r["core_idea"] or "",
            "customer_segment": r["customer_segment"] or "",
            "customer_mindset": r["customer_mindset"] or "",
            "tone_of_voice":    _parse_jsonb_list(r["voice_examples"]) if r["voice_examples"] else [],
            "writing_style":    r["style_guide"] or "",
            "should_write":     r["system_prompt"] or "",
            "forbidden_words":  _parse_fw(r["forbidden_words"]),
            "target_markets":   list(r["target_markets"]) if r["target_markets"] else [],
            "rewrite_language": r["rewrite_language"] or "en",
        }
        for r in rows
    ]
    return {
        "brand_name":       current["brand_name"] or "default",
        "brand_type":       current["brand_type"] or "",
        "core_idea":        current["core_idea"] or "",
        "customer_segment": current["customer_segment"] or "",
        "customer_mindset": current["customer_mindset"] or "",
        "tone_of_voice":    voice,
        "writing_style":    current["style_guide"] or "",
        "good_examples":    current["good_examples"] or "",
        "should_write":     current["system_prompt"] or "",
        "forbidden_words":  _parse_fw(current["forbidden_words"]),
        "target_markets":   list(current["target_markets"]) if current["target_markets"] else [],
        "rewrite_language": current["rewrite_language"] or "en",
        "version":          current["version"],
        "is_active":        current["is_active"],
        "updated_at":       current["updated_at"].isoformat() if current["updated_at"] else None,
        "history":          history,
    }


async def _wm_reverify_after_brand_save(pool, what: str) -> None:
    """AA-900 W0: the AA house style (tenant_brand_rules / tenant_seo_config of the AA master tenant) is part of the
    A1 releases' config_fp (brand_config): scan the writing-memory releases at once after a save (own transaction,
    never fails the save). Other tenants' saves are caught by the same scan for their tenant releases."""
    from services.writing_memory.releases import reverify_scan_safe
    await reverify_scan_safe(pool, actor=f"brand_save:{what}"[:128])


@router.post("/brands")
async def create_brand(
    body: BrandCreateRequest,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    tenant_id = "00000000-0000-0000-0000-000000000001"
    voice = body.tone_of_voice or []
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT COUNT(*) FROM shared.tenant_brand_rules WHERE tenant_id=$1 AND brand_name=$2",
            tenant_id, body.brand_name,
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"Brand '{body.brand_name}' already exists — use PUT to update")
        await conn.execute(
            "UPDATE shared.tenant_brand_rules SET is_active=false WHERE tenant_id=$1 AND brand_name=$2",
            tenant_id, body.brand_name,
        )
        row = await conn.fetchrow("""
            INSERT INTO shared.tenant_brand_rules
                (tenant_id, brand_name, brand_type, core_idea,
                 customer_segment, customer_mindset, voice_examples,
                 style_guide, good_examples, system_prompt, forbidden_words,
                 target_markets, rewrite_language, version, is_active, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11::jsonb,$12,$13,1,true,NOW())
            RETURNING id, version
        """,
            tenant_id, body.brand_name, body.brand_type, body.core_idea,
            body.customer_segment, body.customer_mindset,
            json.dumps(voice),
            body.writing_style, body.good_examples, body.should_write,
            json.dumps(body.forbidden_words or []),
            body.target_markets or [], body.rewrite_language,
        )
    await _wm_reverify_after_brand_save(pool, "create")
    return {"status": "created", "brand_name": body.brand_name, "version": row["version"]}


@router.put("/brands/{brand_name}")
async def update_brand(
    brand_name: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    body_raw = await request.json()
    pool = request.app.state.pool
    tenant_id = "00000000-0000-0000-0000-000000000001"
    async with pool.acquire() as conn:
        current_ver = await conn.fetchval(
            "SELECT COALESCE(MAX(version),0) FROM shared.tenant_brand_rules WHERE tenant_id=$1 AND brand_name=$2",
            tenant_id, brand_name,
        )
        await conn.execute(
            "UPDATE shared.tenant_brand_rules SET is_active=false WHERE tenant_id=$1 AND brand_name=$2",
            tenant_id, brand_name,
        )
        voice = body_raw.get("tone_of_voice") or []
        row = await conn.fetchrow("""
            INSERT INTO shared.tenant_brand_rules
                (tenant_id, brand_name, brand_type, core_idea,
                 customer_segment, customer_mindset, voice_examples,
                 style_guide, good_examples, system_prompt, forbidden_words,
                 target_markets, rewrite_language, version, is_active, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11::jsonb,$12,$13,$14,true,NOW())
            RETURNING version
        """,
            tenant_id, brand_name,
            body_raw.get("brand_type"), body_raw.get("core_idea"),
            body_raw.get("customer_segment"), body_raw.get("customer_mindset"),
            json.dumps(voice if isinstance(voice, list) else [voice]),
            body_raw.get("writing_style"), body_raw.get("good_examples"),
            body_raw.get("should_write"),
            json.dumps(body_raw.get("forbidden_words") or []),
            body_raw.get("target_markets") or [],
            body_raw.get("rewrite_language", "en"),
            current_ver + 1,
        )
    await _wm_reverify_after_brand_save(pool, "update")
    return {"status": "updated", "brand_name": brand_name, "version": row["version"]}


@router.post("/brands/{brand_name}/activate")
async def activate_brand_version(brand_name: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    body_raw = await request.json()
    version = body_raw.get("version")
    if not version:
        raise HTTPException(status_code=422, detail="version is required")
    pool = request.app.state.pool
    tenant_id = "00000000-0000-0000-0000-000000000001"
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT COUNT(*) FROM shared.tenant_brand_rules"
            " WHERE tenant_id=$1 AND COALESCE(brand_name,'default')=$2 AND version=$3",
            tenant_id, brand_name, int(version),
        )
        if not exists:
            raise HTTPException(status_code=404, detail=f"Brand '{brand_name}' version {version} not found")
        await conn.execute(
            "UPDATE shared.tenant_brand_rules SET is_active=false"
            " WHERE tenant_id=$1 AND COALESCE(brand_name,'default')=$2",
            tenant_id, brand_name,
        )
        await conn.execute(
            "UPDATE shared.tenant_brand_rules SET is_active=true"
            " WHERE tenant_id=$1 AND COALESCE(brand_name,'default')=$2 AND version=$3",
            tenant_id, brand_name, int(version),
        )
    await _wm_reverify_after_brand_save(pool, "activate")
    return {"status": "activated", "brand_name": brand_name, "version": version}


@router.delete("/brands/{brand_name}")
async def delete_brand(brand_name: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    tenant_id = "00000000-0000-0000-0000-000000000001"
    async with pool.acquire() as conn:
        deleted = await conn.fetchval("""
            DELETE FROM shared.tenant_brand_rules
            WHERE tenant_id=$1 AND brand_name=$2
            RETURNING id
        """, tenant_id, brand_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_name}' not found")
    await _wm_reverify_after_brand_save(pool, "delete")
    return {"status": "deleted", "brand_name": brand_name}
