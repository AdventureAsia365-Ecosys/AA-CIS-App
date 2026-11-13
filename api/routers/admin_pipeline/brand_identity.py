# api/routers/admin_pipeline/brand_identity.py — brand identity routes (/admin/brand-identity, staff or tenant JWT).
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import base64
import os
import asyncpg
import boto3 as _boto3
from fastapi import Depends, Header, HTTPException, Request
from api.core.aa544_tenant_pool import BRAND_IDENTITY_FLAG, acquire_scoped_conn
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from typing import Optional
from api.routers.admin import verify_admin_secret
from api.routers.auth import verify_tenant_jwt
from api.middleware import tenant_role as _tenant_role  # AA-979 csr check in the dependency
from shared.aws_client.bronze import bronze_bucket
from services.brand_intake.save import CUSTOM_VALIDATORS_VALUE_SQL  # AA-1080
from shared.validators.prompt_sanitize import (
    sanitize_text, sanitize_list, MAX_SHORT_FIELD_LEN, MAX_SYSTEM_PROMPT_LEN, MAX_BRAND_LONG_LEN,
    MAX_BRAND_MEDIUM_LEN, brand_length_errors,
)

from api.routers.admin_pipeline._shared import BrandIdentityUpdate, logger, router
from api.routers.admin_pipeline.brands import _parse_brief_or_400


# ── GET/POST /admin/brand-identity ────────────────────────────────────────────
# AA-424: this endpoint has TWO real callers, discovered while fixing the hardcoded-tenant_id
# bug AA-423 found —
#   1. BrandTab.tsx ((tenant)/portal) — wants ITS OWN tenant's brand rules. Was 401ing for a real
#      tenant session (no cis_admin_token) even before this fix; the bug this issue targets.
#   2. frontend/app/(internal)/{brand,upload}/page.tsx, via the confusingly-named
#      /api/tenant/pipeline/brand-identity proxy (see that file's own header comment) — genuine
#      STAFF pages (admin/content role) that manage the AA-internal pseudo-tenant's OWN brand
#      rules. No tenant JWT available to these pages; X-Admin-Secret is the only credential they
#      carry. Must keep working exactly as before.
# _resolve_brand_tenant_id() below tries a tenant JWT first (real get_tenant()-pattern, same
# verify_jwt() used by v1_tours.py/v1_atoms.py/v1_s3.py/v1_competitors.py's get_tenant/_get_tenant)
# — this is the rewired path AA-424 asked for, no more hardcoded UUID for a real tenant caller.
# Only when no Bearer token is presented does it fall back to the pre-existing static-secret +
# hardcoded AA-internal tenant_id path, preserving the two staff pages' behavior unchanged.
_brand_identity_bearer = HTTPBearer(auto_error=False)
_AA_INTERNAL_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _resolve_brand_tenant_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_brand_identity_bearer),
    x_admin_secret: str = Header(None),
    request: Request = None,
) -> str:
    if credentials is not None:
        try:
            payload = verify_tenant_jwt(credentials.credentials)   # staff JWT → 403, never a tenant
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        _tenant_role.enforce(request, payload)   # AA-979: csr also checked where the token is accepted
        return payload["sub"]
    verify_admin_secret(x_admin_secret)
    return _AA_INTERNAL_TENANT_ID


def _brand_caller_is_tenant(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_brand_identity_bearer),
) -> bool:
    """AA-859 wave 3: True when the caller presented a Bearer. Any Bearer that is not a valid tenant token is
    refused by _resolve_brand_tenant_id (401 / 403) in the same request, so True means "a verified tenant".
    The handlers compare with `is True`: a direct call (tests, other code) passes nothing and gets this
    function's Depends default, which is not True, so it keeps the staff / main-pool path."""
    return credentials is not None


# ── AA-859 wave 3: the tenant branch of /admin/brand-identity on the tenant pool (BRAND_IDENTITY_FLAG) ───────
# A tenant Bearer runs GET / POST in one acquire_scoped_conn block, fail closed: aa_app_user reads and writes only
# its own tenant_brand_rules / tenant_seo_config rows (CIS 262 RLS; grants SELECT, INSERT on the POST's columns,
# UPDATE (is_active); seo config SELECT, INSERT / UPDATE (target_market, updated_at)). The POST takes an advisory
# xact lock per tenant before MAX(version) + 1 (two-int form, class 859 as W6), so two saves cannot compute the
# same version; a duplicate anyway (UNIQUE (tenant_id, brand_name, version), e.g. a concurrent staff save on MAIN)
# is a 409, never a 500. Flag off: the admin pool and the pre-AA-859 statements. The X-Admin-Secret staff branch
# (AA master brand) reads no flag and stays on the admin pool (plan D2). check_saved_cta -> notify_admins runs on
# MAIN after the transaction (D2: platform notifications, best-effort). One `aa859_brand_identity route=get|post
# used_tenant_pool=...` line per tenant request. SQL in module constants so tests can mutate the tenant condition.
AA859_BRAND_LOCK_CLASS = 859
BRAND_VERSION_CONFLICT_DETAIL = ("version_conflict: another save of this brand identity landed at the same time, "
                                 "reload and retry")
_BRAND_TENANT_COND = "tenant_id = $1"
_BRAND_COLS = (
    "system_prompt, style_guide, forbidden_words, version, updated_at, created_at, "
    "COALESCE(brand_name, '') AS brand_name, COALESCE(brand_type, '') AS brand_type, "
    "COALESCE(core_idea, '') AS core_idea, COALESCE(customer_segment, '') AS customer_segment, "
    "COALESCE(customer_mindset, '') AS customer_mindset, "
    "COALESCE(voice_examples, '[]'::jsonb) AS voice_examples, "
    "COALESCE(good_examples, '') AS good_examples, "
    "COALESCE(target_markets, ARRAY[]::text[]) AS target_markets, "
    "COALESCE(default_cta, '') AS default_cta, custom_validators"
)
_BRAND_ACTIVE_SQL = f"""
            SELECT {_BRAND_COLS}
            FROM shared.tenant_brand_rules
            WHERE {{tenant_cond}} AND is_active = true
            ORDER BY version DESC LIMIT 1
        """
_BRAND_HISTORY_SQL = f"""
            SELECT {_BRAND_COLS}, is_active
            FROM shared.tenant_brand_rules WHERE {{tenant_cond}} ORDER BY version DESC
        """
_BRAND_LOCK_SQL = "SELECT pg_advisory_xact_lock($1::int, hashtext($2::text || ':brand_identity'))"
_BRAND_MAX_VERSION_SQL = """
            SELECT COALESCE(MAX(version), 0) FROM shared.tenant_brand_rules WHERE tenant_id = $1
        """
_BRAND_DEACTIVATE_SQL = "UPDATE shared.tenant_brand_rules SET is_active = false WHERE tenant_id = $1"
_BRAND_INSERT_SQL = """
            INSERT INTO shared.tenant_brand_rules
                (tenant_id, system_prompt, style_guide, forbidden_words, version, is_active,
                 updated_at, brand_name, brand_type, core_idea, customer_segment,
                 customer_mindset, voice_examples, good_examples, target_markets, default_cta,
                 custom_validators)
            VALUES ($1, $2, $3, $4::jsonb, $5, true, NOW(), $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14,
                    """ + CUSTOM_VALIDATORS_VALUE_SQL.format(param="$15", tenant="$1", version="$5") + """)
        """
_BRAND_MARKETS_UPSERT_SQL = """
                INSERT INTO shared.tenant_seo_config (tenant_id, target_market)
                VALUES ($1::uuid, $2::jsonb)
                ON CONFLICT (tenant_id) DO UPDATE
                    SET target_market = EXCLUDED.target_market, updated_at = now()
            """


def _log_brand_identity(route: str, tenant_id, used_tenant_pool: bool, **extra) -> None:
    logger.info("aa859_brand_identity", route=route, tenant_id=str(tenant_id), used_tenant_pool=used_tenant_pool,
                **extra)


async def _read_brand_rows(conn, tenant_id):
    """(active row or None, history rows): the GET's two reads on `conn`; no history read without a row."""
    row = await conn.fetchrow(_BRAND_ACTIVE_SQL.format(tenant_cond=_BRAND_TENANT_COND), tenant_id)
    if not row:
        return None, []
    history_rows = await conn.fetch(_BRAND_HISTORY_SQL.format(tenant_cond=_BRAND_TENANT_COND), tenant_id)
    return row, history_rows


def _parse_brand_list(v):
    """Shared by GET /brand-identity and history rows — `voice_examples`/`target_markets` can
    come back as a JSON string (no jsonb codec on this connection) or already a list/None."""
    import json as _json_br
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        parsed = _json_br.loads(v)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _row_get(row, key):
    """A record column that older test doubles may not carry (AA-1080 added custom_validators)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


@router.get("/brand-identity")
async def get_brand_identity(
    request: Request, tenant_id: str = Depends(_resolve_brand_tenant_id),
    tenant_caller: bool = Depends(_brand_caller_is_tenant),
):
    # AA-557 J.23 — full field set (see BrandIdentityUpdate's own comment for why). Columns: _BRAND_COLS.
    from services.acp_shared.signature_vocabulary import explicit_signature_words
    from services.brand_intake.markets import market_summary
    if tenant_caller is True:
        # AA-859 wave 3: the tenant's own rows on the tenant pool (262 RLS) behind BRAND_IDENTITY_FLAG.
        async with acquire_scoped_conn(
            request, tenant_id, BRAND_IDENTITY_FLAG, fail_closed=True,
        ) as (conn, used_tenant_pool):
            row, history_rows = await _read_brand_rows(conn, tenant_id)
        _log_brand_identity("get", tenant_id, used_tenant_pool, configured=row is not None)
    else:
        # AA-859 MAIN exception (plan D2): the X-Admin-Secret staff branch (AA master brand) is outside the
        # tenant boundary.
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            row, history_rows = await _read_brand_rows(conn, tenant_id)
    if not row:
        return {
            "configured": False, "system_prompt": None, "style_guide": None,
            "forbidden_words": [], "history": [], "brand_name": "", "brand_type": "",
            "core_idea": "", "customer_segment": "", "customer_mindset": "",
            "tone_of_voice": [], "good_examples": "", "target_markets": [],
            "default_cta": "", "signature_words": [], "markets": None,
        }

    def parse_fw(fw):
        return _parse_brand_list(fw)

    history = [{
        "version":         h["version"],
        "is_active":       h["is_active"],
        "system_prompt":   h["system_prompt"] or "",
        "style_guide":     h["style_guide"] or "",
        "forbidden_words": parse_fw(h["forbidden_words"]),
        "brand_name":      h["brand_name"],
        "tone_of_voice":   _parse_brand_list(h["voice_examples"]),
        "target_markets":  list(h["target_markets"]) if h["target_markets"] else [],
        "updated_at": h["updated_at"].isoformat() if h["updated_at"] else None,
        "created_at": h["created_at"].isoformat() if h["created_at"] else None,
    } for h in history_rows]
    return {
        "configured":      True,
        "system_prompt":   row["system_prompt"],
        "style_guide":     row["style_guide"],
        "forbidden_words": parse_fw(row["forbidden_words"]),
        "version":         row["version"],
        "updated_at":      row["updated_at"].isoformat() if row["updated_at"] else None,
        "history":         history,
        "brand_name":       row["brand_name"],
        "brand_type":       row["brand_type"],
        "core_idea":        row["core_idea"],
        "customer_segment": row["customer_segment"],
        "customer_mindset": row["customer_mindset"],
        "tone_of_voice":    _parse_brand_list(row["voice_examples"]),
        "good_examples":    row["good_examples"],
        "target_markets":   list(row["target_markets"]) if row["target_markets"] else [],
        "default_cta":      row["default_cta"],
        # AA-1080 — the AA-800 signature words (custom_validators) and the market note (unsupported markets
        # with the reason, the market T2 keywords come from), shown on load, not only after a save.
        "signature_words":  explicit_signature_words(_row_get(row, "custom_validators")),
        "markets":          market_summary(list(row["target_markets"]) if row["target_markets"] else []),
    }


@router.post("/brand-identity")
async def update_brand_identity(
    body: BrandIdentityUpdate,
    request: Request,
    tenant_id: str = Depends(_resolve_brand_tenant_id),
    tenant_caller: bool = Depends(_brand_caller_is_tenant),
):
    import json as _json
    # AA-487: a direct prompt-injection surface (the DOCX brand-brief Lambda was retired in
    # AA-750) — BrandTab.tsx lets a tenant type system_prompt/
    # style_guide straight into a textarea and POST here, with no parsing step at all in
    # between. Sanitize the same way (shared.validators.prompt_sanitize, same module the Lambda
    # fix uses) before it lands in the column every future LLM call for this tenant replays.
    # AA-741 (S229): reject over-long fields with a clear message instead of cutting them silently.
    too_long = brand_length_errors(body.model_dump())
    if too_long:
        raise HTTPException(status_code=422, detail="Some brand fields are too long — "
                            + "; ".join(too_long))
    system_prompt = sanitize_text(body.system_prompt, MAX_SYSTEM_PROMPT_LEN)
    style_guide = sanitize_text(body.writing_style or body.style_guide, MAX_BRAND_LONG_LEN)
    forbidden_words = sanitize_list(body.forbidden_words or [], MAX_SHORT_FIELD_LEN, max_items=20)
    # AA-557 J.23 — extended fields, same sanitize_text/sanitize_list guards as the pre-existing
    # 3; brand_name falls back to 'default' (the single-brand-per-tenant sentinel this file
    # already uses elsewhere, _resolve_brand_rule) when the tenant hasn't named their brand yet.
    brand_name = sanitize_text(body.brand_name, MAX_SHORT_FIELD_LEN) or "default"
    brand_type = sanitize_text(body.brand_type, MAX_SHORT_FIELD_LEN)
    core_idea = sanitize_text(body.core_idea, MAX_BRAND_MEDIUM_LEN)
    customer_segment = sanitize_text(body.customer_segment, MAX_BRAND_MEDIUM_LEN)
    customer_mindset = sanitize_text(body.customer_mindset, MAX_BRAND_MEDIUM_LEN)
    tone_of_voice = sanitize_list(body.tone_of_voice or [], MAX_SHORT_FIELD_LEN, max_items=20)
    good_examples = sanitize_text(body.good_examples, MAX_BRAND_LONG_LEN)
    target_markets = sanitize_list(body.target_markets or [], MAX_SHORT_FIELD_LEN, max_items=20)
    default_cta = sanitize_text(body.default_cta, MAX_BRAND_MEDIUM_LEN)
    # AA-741 (S229): the form's markets are THE tenant markets. T2/T7 read
    # tenant_seo_config.target_market, which no tenant could write, so every tenant silently
    # wrote for the US default. Normalise and sync it in the same transaction as the brand row.
    from services.brand_intake.markets import market_summary, target_market_json
    from services.brand_intake.save import record_unsupported_markets, signature_words_json
    market_json = target_market_json(target_markets)
    custom_validators_json = signature_words_json(body.signature_words)  # AA-1080; None = keep the previous

    pool = request.app.state.pool
    args = (system_prompt, style_guide, _json.dumps(forbidden_words), brand_name, brand_type, core_idea,
            customer_segment, customer_mindset, _json.dumps(tone_of_voice), good_examples, target_markets,
            default_cta, _json.dumps(market_json))
    kwargs = {"custom_validators_json": custom_validators_json}
    if tenant_caller is True:
        # AA-859 wave 3: one transaction on the tenant pool (262 RLS + grants) behind BRAND_IDENTITY_FLAG.
        async with acquire_scoped_conn(
            request, tenant_id, BRAND_IDENTITY_FLAG, fail_closed=True,
        ) as (conn, used_tenant_pool):
            _log_brand_identity("post", tenant_id, used_tenant_pool)
            if used_tenant_pool:   # already inside acquire_scoped_conn's transaction
                current = await _save_brand_rows(conn, tenant_id, *args, guarded=True, **kwargs)
            else:                  # flag off: the admin pool, one transaction as before
                async with conn.transaction():
                    current = await _save_brand_rows(conn, tenant_id, *args, guarded=False, **kwargs)
    else:
        # AA-859 MAIN exception (plan D2): the X-Admin-Secret staff branch (AA master brand).
        async with pool.acquire() as conn, conn.transaction():
            current = await _save_brand_rows(conn, tenant_id, *args, guarded=False, **kwargs)
            # AA-900 W0: the AA master brand is the A1 releases' config_fp brand_config: scan on this connection.
            # A tenant save stays off MAIN (AA-859): the worker's hourly reverify tick catches its tenant releases.
            from services.writing_memory.releases import reverify_scan_in_tx
            await reverify_scan_in_tx(conn, actor="brand_save:brand_identity")
    out = {"status": "updated", "version": current + 1,
           "markets": {**{k: market_json[k] for k in ("countries", "unsupported", "unknown")},
                       **{k: v for k, v in market_summary(target_markets, market_json["countries"]).items()
                          if k in ("supported", "seo_market", "defaulted", "notes")}}}
    # AA-1080 / AA-859 MAIN exception (plan D2, like the CTA notice below): one unmapped-market request per declared
    # country without search data, recorded on the main pool after the tenant transaction (best effort).
    if tenant_id != _AA_INTERNAL_TENANT_ID:
        await record_unsupported_markets(pool, tenant_id, market_json)
    # AA-819 follow-up (S237): a default CTA that claims reviews or ratings is saved as typed (F19
    # exempts the tenant's CTA line); the portal shows a warning and AA admins get one bell row.
    # aa_internal is AA's own brand (staff path), so it only gets the warning.
    # AA-859 MAIN exception (plan D2): notify_admins writes platform notifications, after the tenant transaction.
    from services.acp_shared.cta_review_notice import WHERE_BRAND_DEFAULT, check_saved_cta
    warning = await check_saved_cta(pool, tenant_id, default_cta, WHERE_BRAND_DEFAULT,
                                    notify=tenant_id != _AA_INTERNAL_TENANT_ID)
    if warning:
        out["cta_warning"] = warning
    return out


async def _save_brand_rows(conn, tenant_id, system_prompt, style_guide, forbidden_words_json, brand_name,
                           brand_type, core_idea, customer_segment, customer_mindset, voice_examples_json,
                           good_examples, target_markets, default_cta, market_json, *, guarded: bool,
                           custom_validators_json: str | None = None) -> int:
    """The POST's statements on `conn`, inside the caller's transaction; returns the previous MAX(version).
    guarded=True (tenant pool): advisory lock first, a duplicate version is a 409. guarded=False (admin pool):
    the pre-AA-859 statements, unchanged."""
    if guarded:
        await conn.execute(_BRAND_LOCK_SQL, AA859_BRAND_LOCK_CLASS, str(tenant_id))
    # NOT scoped by brand_name — this tenant-facing endpoint keeps its pre-existing invariant
    # (AA-424: exactly one active row per tenant_id at a time, regardless of brand_name) so a
    # tenant editing their own brand_name mid-flow can't leave 2 simultaneously-active rows
    # behind for GET to pick between non-deterministically. The admin-only `/admin/brands`
    # CRUD (BrandCreateRequest, above) is the one genuinely multi-brand-per-tenant path.
    current = await conn.fetchval(_BRAND_MAX_VERSION_SQL, tenant_id)
    await conn.execute(_BRAND_DEACTIVATE_SQL, tenant_id)
    # AA-424 live-verify fix: brand_name is NOT NULL (migration 044) but this INSERT never
    # set it -- 500s (NotNullViolationError) on every call, for every tenant, not just the
    # ones this issue targets. Pre-existing bug, not introduced by this issue's auth rewire
    # (found because this is the first time POST got exercised end-to-end against a real
    # tenant_id here). 'default' matches the sentinel this same file already uses elsewhere
    # for the single-brand-per-tenant case (_resolve_brand_rule, admin_pipeline.py:328).
    # Positional order kept: ($1 tenant_id, $2 system_prompt, $3 style_guide, $4
    # forbidden_words, $5 version) — tests/unit/test_aa487_brand_identity_sanitize.py asserts
    # these exact `conn.execute.call_args` positions; new J.23 fields are appended after
    # rather than interleaved, so that pre-existing sanitization contract stays byte-stable.
    insert_args = (tenant_id, system_prompt, style_guide, forbidden_words_json, current + 1,
                   brand_name, brand_type, core_idea, customer_segment, customer_mindset,
                   voice_examples_json, good_examples, target_markets, default_cta, custom_validators_json)
    if guarded:
        try:
            await conn.execute(_BRAND_INSERT_SQL, *insert_args)
        except asyncpg.UniqueViolationError as exc:
            logger.warning("aa859_brand_identity_version_conflict", tenant_id=str(tenant_id), version=current + 1)
            raise HTTPException(status_code=409, detail=BRAND_VERSION_CONFLICT_DETAIL) from exc
    else:
        await conn.execute(_BRAND_INSERT_SQL, *insert_args)
    if tenant_id != _AA_INTERNAL_TENANT_ID:
        # aa_internal's markets stay owned by PATCH /admin/settings/seo (admin research).
        # AA-741 (S229): the form's markets are THE tenant markets; synced in the brand row's transaction.
        await conn.execute(_BRAND_MARKETS_UPSERT_SQL, tenant_id, market_json)
    return current


_BRIEF_PARSE_MAX_BYTES = 5 * 1024 * 1024   # base64 adds ~33%; API Gateway caps a request at 10 MB
_BRAND_UPLOAD_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
}


class BrandFileUpload(BaseModel):
    filename: str = "brand.docx"
    content_type: str
    file_base64: str


@router.post("/brand-identity/upload")
async def upload_brand_file(
    body: BrandFileUpload,
    tenant_id: str = Depends(_resolve_brand_tenant_id),
):
    """AA-441 (bug #4): was a JSON-body endpoint that only ever handed back a presigned S3 PUT
    URL — the client was expected to PUT the file directly to S3 afterward, but no code
    anywhere (BrandTab.tsx included) ever took that second step, so no upload actually
    completed regardless of auth. BrandTab.tsx has always sent the real file as multipart
    FormData in one request; this endpoint now matches that shape directly (accepts the file,
    uploads to S3 server-side) instead of the unused two-step presigned-URL contract. tenant_id
    now resolves via the same _resolve_brand_tenant_id() dependency AA-424 already wired for
    GET/POST /admin/brand-identity, instead of a hardcoded AA-internal UUID — a real tenant's
    upload now lands under their own S3 prefix, not aa_internal's.

    Does NOT parse/extract brand rules from the uploaded file (no "AI extracting rules" step
    exists yet, per BrandTab.tsx's own aspirational copy) — tracked as a separate follow-up,
    deliberately not built here (Nghiep, Linear AA-441 comment).
    """
    import uuid as _uuid2
    import re as _re2

    # AA-741 (S229): base64 JSON, not multipart. The REST API Gateway in front of the API has no
    # binaryMediaTypes, so multipart bytes were corrupted in transit — every brand file stored
    # here before was damaged (verified: a 15,191-byte .docx arrived as 25,187 bytes).
    if body.content_type not in _BRAND_UPLOAD_ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {body.content_type}")
    try:
        contents = base64.b64decode(body.file_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="The file could not be read (invalid encoding)")
    if len(contents) > _BRIEF_PARSE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 5MB limit")

    safe_name = _re2.sub(r'[^a-zA-Z0-9._-]', '_', body.filename or "brand.pdf")
    s3_key    = f"brand-identity/{tenant_id}/{_uuid2.uuid4()}_{safe_name}"
    bucket    = bronze_bucket()

    s3 = _boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    try:
        s3.put_object(Bucket=bucket, Key=s3_key, Body=contents, ContentType=body.content_type)
    except Exception as e:
        logger.error("brand_identity_upload_failed", tenant_id=tenant_id, s3_key=s3_key, error=str(e))
        raise HTTPException(status_code=502, detail="Upload to storage failed")

    return {"status": "uploaded", "s3_key": s3_key}


class ParseBriefRequest(BaseModel):
    filename: str = "brief.docx"
    file_base64: str


@router.post("/brand-identity/parse")
async def parse_brand_brief(
    body: ParseBriefRequest,
    tenant_id: str = Depends(_resolve_brand_tenant_id),
):
    """AA-741 (S229) — T0: read a brand brief (.docx) and return the Brand Identity form fields
    for the tenant to REVIEW. Nothing is saved here; the tenant saves through
    POST /admin/brand-identity after checking the form. Same auth as the form (tenant JWT, or the
    admin secret for aa_internal).

    The file comes as base64 JSON, not multipart: the REST API Gateway in front of the API has no
    binaryMediaTypes, so a multipart body is treated as text and its bytes are corrupted on the
    way in ("Bad offset for central directory")."""
    if not (body.filename or "").lower().endswith(".docx"):
        raise HTTPException(status_code=415, detail="Upload the brief as a Word .docx file")
    try:
        contents = base64.b64decode(body.file_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="The file could not be read (invalid encoding)")
    if len(contents) > _BRIEF_PARSE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The brief is larger than 5 MB")
    out = _parse_brief_or_400(contents)
    logger.info("brand_brief_parsed", tenant_id=tenant_id, filename=body.filename,
                fields=sorted(k for k, v in out["fields"].items() if v), warnings=len(out["warnings"]))
    return {"fields": out["fields"], "warnings": out["warnings"], "markets": out["markets"],
            "drafted": out.get("drafted", []), "unmapped_fields": out.get("unmapped_fields", [])}
