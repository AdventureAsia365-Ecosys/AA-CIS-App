# api/routers/admin_pipeline/tour_detail.py — tour detail and edit routes: version detail, versions, history,
# detail, country, raw and generated edits, revalidate.
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import json
from uuid import UUID
from fastapi import Header, HTTPException, Request
from typing import Optional
from api.routers.admin import verify_admin_secret
from services.quality import final_contract_store as _fc_store  # AA-1047 W1

from api.routers.admin_pipeline._shared import CountryUpdateRequest, _as_list, _extract_judge_score, router


@router.get("/tours/{tour_id}/versions/{version_num}")
async def get_tour_version_detail(
    tour_id: str, version_num: int,
    request: Request, x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT gc.id, gc.version_num, gc.model_editorial AS model_id,
                   qs.score_overall AS quality_score,
                   qs.score_brand, qs.score_seo, qs.score_structure,
                   qs.score_quality, qs.failure_codes,
                   qs.brand_audit_status, qs.brand_audit_codes, qs.brand_audit_issues,
                   gc.fix_pass_applied, gc.fix_pass_fields,
                   gc.created_at,
                   gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description,
                   gc.aa_highlights, gc.aa_itineraries, gc.seo_title, gc.seo_meta,
                   gc.metadata,
                   tbr.brand_name AS brand_name,
                   sc.top_keywords, sc.keyword_ideas, sc.people_also_ask,
                   sc.related_keywords, sc.keyword_search,
                   rt.country, rt.duration, rt.group_size, rt.price_raw,
                   rt.period, rt.provider, rt.inclusions, rt.exclusions
            FROM silver_aa_internal.generated_content gc
            LEFT JOIN silver_aa_internal.quality_scores qs
                ON qs.generated_content_id = gc.id
            LEFT JOIN shared.tenant_brand_rules tbr
                ON tbr.id = NULLIF(gc.metadata->>'brand_rule_id', '')::uuid
            LEFT JOIN silver_aa_internal.raw_tours rt
                ON rt.tour_id = gc.tour_id
            LEFT JOIN LATERAL (
                SELECT top_keywords, keyword_ideas, people_also_ask,
                       related_keywords, keyword_search
                FROM silver_aa_internal.seo_context
                WHERE tour_id = gc.tour_id
                ORDER BY fetched_at DESC LIMIT 1
            ) sc ON true
            WHERE gc.tour_id = $1::uuid AND gc.version_num = $2
            LIMIT 1
        """, tour_id, version_num)
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")
    meta = {}
    if row["metadata"]:
        try:
            meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"])
        except Exception:
            meta = {}
    highlights = row["aa_highlights"]
    if not isinstance(highlights, list):
        highlights = json.loads(highlights) if highlights else []
    keywords = row["top_keywords"]
    if not isinstance(keywords, list):
        keywords = json.loads(keywords) if keywords else []

    # AA-219/AA-235: jsonb columns arrive as str OR list/dict depending on driver path —
    # module-level _as_list collapses dict/None → []. judge/revalidate live in metadata (AA-213).
    _judge = meta.get("judge") if isinstance(meta, dict) else None
    return {
        "id":             str(row["id"]),
        "version_num":    row["version_num"],
        "model_id":       row["model_id"],
        "quality_score":  float(row["quality_score"]) if row["quality_score"] else None,
        "score_brand":    float(row["score_brand"]) if row["score_brand"] else None,
        "score_seo":      float(row["score_seo"]) if row["score_seo"] else None,
        "score_structure": float(row["score_structure"]) if row["score_structure"] else None,
        "created_at":     row["created_at"].isoformat() if row["created_at"] else None,
        "aa_name":        row["aa_name"],
        "aa_subtitle":    row["aa_subtitle"],
        "aa_summary":     row["aa_summary"],
        "aa_description": row["aa_description"],
        "aa_highlights":  highlights,
        "aa_itineraries": row["aa_itineraries"],
        "seo_title":      row["seo_title"],
        "seo_meta":       row["seo_meta"],
        "brand_name":     row["brand_name"] or meta.get("brand_rule_id") and "custom" or "default",
        "seo_mode":       meta.get("seo_mode", "standard"),
        "dataforseo_used": meta.get("dataforseo_used", False),
        "llm_cost_usd":   meta.get("llm_cost_usd"),
        "top_keywords":   keywords,
        "country":        row["country"],
        "duration":       row["duration"],
        "group_size":     row["group_size"],
        "price_raw":      row["price_raw"],
        "period":         row["period"],
        "provider":       row["provider"],
        "inclusions":     row["inclusions"],
        "exclusions":     row["exclusions"],
        # AA-219: surface judge/quality/audit/fix/failure/revalidate (were dropped → silent-empty UI).
        "judge":            _judge,
        "score_quality":    float(row["score_quality"]) if row["score_quality"] is not None else None,
        "brand_audit_status": row["brand_audit_status"],
        "brand_audit_codes":  _as_list(row["brand_audit_codes"]),
        "brand_audit_issues": _as_list(row["brand_audit_issues"]),
        "fix_pass_applied":   bool(row["fix_pass_applied"]) if row["fix_pass_applied"] is not None else False,
        "fix_pass_fields":    _as_list(row["fix_pass_fields"]),
        "failure_codes":      _as_list(row["failure_codes"]),
        "revalidate_ran":     bool(meta.get("revalidate_ran")) if isinstance(meta, dict) else False,
        "revalidate_passed":  bool(meta.get("revalidate_passed")) if isinstance(meta, dict) else False,
        # AA-219 + AA-218: full DFS per tour seed (keyword_ideas/PAA/related + seed).
        "keyword_ideas":      _as_list(row["keyword_ideas"]),
        "people_also_ask":    _as_list(row["people_also_ask"]),
        "related_keywords":   _as_list(row["related_keywords"]),
        "seed":               row["keyword_search"],
    }


@router.get("/tours/{tour_id}/versions")
async def list_tour_versions(
    tour_id: str, request: Request, x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT gc.id, gc.version_num, gc.model_editorial AS model_id,
                   qs.score_overall AS quality_score,
                   qs.score_brand, qs.score_seo, qs.score_structure, qs.score_quality,
                   gc.metadata, gc.created_at,
                   (pt.generated_content_id = gc.id) AS is_current,
                   gc.fix_pass_applied, gc.fix_pass_fields,
                   qs.brand_audit_status, qs.brand_audit_codes, qs.brand_audit_issues
            FROM silver_aa_internal.generated_content gc
            LEFT JOIN silver_aa_internal.quality_scores qs
                ON qs.generated_content_id = gc.id
            LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = gc.tour_id
            WHERE gc.tour_id = $1::uuid
            ORDER BY gc.version_num DESC
        """, tour_id)

    return {"versions": [
        {
            "id":                 str(r["id"]),
            "version_num":        r["version_num"],
            "model_id":           r["model_id"],
            "quality_score":      float(r["quality_score"]) if r["quality_score"] else None,
            "score_brand":        float(r["score_brand"]) if r["score_brand"] is not None else None,
            "score_seo":          float(r["score_seo"]) if r["score_seo"] is not None else None,
            "score_structure":    float(r["score_structure"]) if r["score_structure"] is not None else None,
            "score_quality":      float(r["score_quality"]) if r["score_quality"] is not None else None,
            "judge_score":        _extract_judge_score(r["metadata"]),
            "created_at":         r["created_at"].isoformat() if r["created_at"] else None,
            "is_current":         bool(r["is_current"]),
            "brand_audit_status": r["brand_audit_status"],
            "brand_audit_codes":  (
                json.loads(r["brand_audit_codes"]) if isinstance(r["brand_audit_codes"], str)
                else (list(r["brand_audit_codes"]) if r["brand_audit_codes"] else [])
            ),
            "brand_audit_issues": (
                json.loads(r["brand_audit_issues"]) if isinstance(r["brand_audit_issues"], str)
                else (list(r["brand_audit_issues"]) if r["brand_audit_issues"] else [])
            ),
            "fix_pass_applied":   (
                bool(r["fix_pass_applied"]) if r["fix_pass_applied"] is not None else False
            ),
            "fix_pass_fields":    (
                json.loads(r["fix_pass_fields"]) if isinstance(r["fix_pass_fields"], str)
                else (list(r["fix_pass_fields"]) if r["fix_pass_fields"] else [])
            ),
        }
        for r in rows
    ]}


# ── GET /admin/tours/{tour_id}/history ───────────────────────────────────────

@router.get("/tours/{tour_id}/history")
async def get_tour_history(tour_id: str, request: Request, x_admin_secret: str = Header(None)):
    """AA-318 bug fix, found while wiring prompt-version observability into the Rewrite History
    UI: this query selected `gc.prompt_version` as a bare column — that column has never
    existed on `silver_aa_internal.generated_content` (grepped every migration; only
    `_build_generated_metadata()`'s JSONB `metadata.prompt_version` — see admin_pipeline.py's
    own docstring there — and the unrelated `acp_contract.s1_from_atom_runs.prompt_version`,
    migration 088, exist). Every call to this endpoint (TourDetailPanelV2.tsx's "Rewrite
    History" tab) would have raised `UndefinedColumnError` — real, live-broken, not a
    theoretical gap."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                gc.id::text, gc.version_num, gc.created_at, gc.status::text,
                gc.model_editorial, gc.brand_rules_version,
                gc.metadata->>'prompt_version' AS prompt_version,
                gc.tenant_id,
                qs.score_overall, qs.score_brand, qs.score_seo, qs.score_structure,
                (gc.metadata->>'llm_cost_usd')::numeric AS cost_usd,
                tbr.brand_name AS brand_name
            FROM silver_aa_internal.generated_content gc
            LEFT JOIN silver_aa_internal.quality_scores qs
                ON qs.generated_content_id = gc.id
            LEFT JOIN shared.tenant_brand_rules tbr
                ON tbr.version = gc.brand_rules_version
                AND tbr.tenant_id = gc.tenant_id
                AND tbr.brand_name = gc.metadata->>'brand_name'
            WHERE gc.tour_id = $1::uuid
            ORDER BY gc.created_at DESC
        """, tour_id)
    return {
        "history": [
            {
                "id":                   r["id"],
                "version_num":          r["version_num"],
                "created_at":           str(r["created_at"]) if r["created_at"] else None,
                "status":               r["status"],
                "model_editorial":      r["model_editorial"],
                "brand_rules_version":  r["brand_rules_version"],
                "brand_name":           r["brand_name"],
                "prompt_version":       r["prompt_version"],
                "score_overall":        float(r["score_overall"]) if r["score_overall"] is not None else None,
                "score_brand":          float(r["score_brand"])   if r["score_brand"] is not None else None,
                "score_seo":            float(r["score_seo"])     if r["score_seo"] is not None else None,
                "score_structure":      float(r["score_structure"]) if r["score_structure"] is not None else None,
                "llm_model":            r["model_editorial"],
                "cost_usd":             float(r["cost_usd"]) if r["cost_usd"] is not None else None,
            }
            for r in rows
        ]
    }


# ── GET /admin/tours/{tour_id}/detail ────────────────────────────────────────

@router.get("/tours/{tour_id}/detail")
async def get_tour_detail(tour_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        raw = await conn.fetchrow("""
            SELECT tour_id::text, src_name, src_subtitle, src_summary, src_description,
                   src_highlights, src_itineraries, country, duration, price_raw,
                   group_size, period, provider, inclusions, exclusions,
                   pipeline_status::text, ingest_at
            FROM silver_aa_internal.raw_tours
            WHERE tour_id = $1::uuid
        """, tour_id)
        if not raw:
            raise HTTPException(status_code=404, detail=f"Tour {tour_id} not found")

        gen = await conn.fetchrow("""
            SELECT gc.id::text, gc.version_num, gc.created_at, gc.status::text,
                   gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description,
                   gc.aa_highlights, gc.aa_itineraries, gc.seo_title, gc.seo_meta,
                   gc.seo_keywords_used, gc.model_editorial, gc.metadata,
                   qs.score_overall, qs.score_brand, qs.score_seo,
                   qs.score_structure, qs.score_quality, qs.brand_audit_status
            FROM silver_aa_internal.generated_content gc
            LEFT JOIN silver_aa_internal.quality_scores qs
                ON qs.generated_content_id = gc.id
            WHERE gc.tour_id = $1::uuid
            ORDER BY gc.version_num DESC LIMIT 1
        """, tour_id)

        pub = await conn.fetchrow("""
            SELECT id::text, aa_name, aa_subtitle, quality_score, published_at
            FROM gold_aa_internal.published_tours
            WHERE tour_id = $1::uuid
            ORDER BY published_at DESC LIMIT 1
        """, tour_id)

    def _parse_jsonb(v):
        if v is None:
            return []
        if isinstance(v, (list, dict)):
            return v
        try:
            return __import__("json").loads(v)
        except Exception:
            return []

    # AA-209: surface the persisted judge object (metadata.judge) so the UI can show what actually
    # drove score_overall. Absent for legacy/no-profile versions → judge stays None (UI renders "—").
    def _parse_meta(v):
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        try:
            return __import__("json").loads(v)
        except Exception:
            return {}

    _meta = _parse_meta(gen["metadata"]) if gen else {}
    _judge = _meta.get("judge") if isinstance(_meta, dict) else None

    return {
        "raw": {
            "tour_id":         raw["tour_id"],
            "src_name":        raw["src_name"],
            "src_subtitle":    raw["src_subtitle"],
            "src_summary":     raw["src_summary"],
            "src_description": raw["src_description"],
            "src_highlights":  _parse_jsonb(raw["src_highlights"]),
            "src_itineraries": raw["src_itineraries"],
            "country":         raw["country"],
            "duration":        raw["duration"],
            "price_raw":       raw["price_raw"],
            "group_size":      raw["group_size"],
            "period":          raw["period"],
            "provider":        raw["provider"],
            "inclusions":      raw["inclusions"],
            "exclusions":      raw["exclusions"],
            "pipeline_status": raw["pipeline_status"],
            "ingest_at":       str(raw["ingest_at"]) if raw["ingest_at"] else None,
        },
        "generated": {
            "id":               gen["id"],
            "version_num":      gen["version_num"],
            "created_at":       str(gen["created_at"]) if gen["created_at"] else None,
            "status":           gen["status"],
            "aa_name":          gen["aa_name"],
            "aa_subtitle":      gen["aa_subtitle"],
            "aa_summary":       gen["aa_summary"],
            "aa_description":   gen["aa_description"],
            "aa_highlights":    _parse_jsonb(gen["aa_highlights"]),
            "aa_itineraries":   gen["aa_itineraries"],
            "seo_title":        gen["seo_title"],
            "seo_meta":         gen["seo_meta"],
            "seo_keywords_used": _parse_jsonb(gen["seo_keywords_used"]),
            "model_editorial":  gen["model_editorial"],
            "score_overall":    float(gen["score_overall"]) if gen["score_overall"] is not None else None,
            "score_brand":      float(gen["score_brand"])   if gen["score_brand"] is not None else None,
            "score_seo":        float(gen["score_seo"])     if gen["score_seo"] is not None else None,
            "score_structure":  float(gen["score_structure"]) if gen["score_structure"] is not None else None,
            # AA-209: score_quality was SELECTed but dropped from the response — now returned so the
            # UI can show all 4 sub-scores; brand_audit_status + judge expose the GPT-4.1 judge.
            "score_quality":    float(gen["score_quality"]) if gen["score_quality"] is not None else None,
            "brand_audit_status": gen["brand_audit_status"],
            "judge":            _judge,
        } if gen else None,
        "published": {
            "id":            pub["id"],
            "aa_name":       pub["aa_name"],
            "aa_subtitle":   pub["aa_subtitle"],
            "quality_score": float(pub["quality_score"]) if pub["quality_score"] is not None else None,
            "published_at":  str(pub["published_at"]) if pub["published_at"] else None,
        } if pub else None,
    }


# ── PATCH /admin/tours/{tour_id}/country ─────────────────────────────────────

@router.patch("/tours/{tour_id}/country")
async def update_tour_country(
    tour_id: str,
    body: CountryUpdateRequest,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    country = _tour_country(body.country)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        updated = await conn.fetchrow(
            "UPDATE silver_aa_internal.raw_tours SET country = $1 WHERE tour_id = $2::uuid"
            " RETURNING tour_id, now() AS changed_at",
            country, tour_id,
        )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Tour {tour_id} not found")
    await _source_fanout_raw(pool, tour_id, updated["changed_at"], ["country"])
    return {"tour_id": tour_id, "country": country}


async def _source_fanout_raw(pool, tour_id: str, changed_at, fields: list[str]) -> None:
    """AA-1052 W6: a raw tour edit re-checks the writer objects written from it (job source_fanout, $0; marks only).
    Best-effort after the commit: a failed enqueue never fails the edit."""
    from services.quality import source_fanout
    await source_fanout.enqueue_safe(pool, source_fanout.Change(
        source_kind=source_fanout.RAW_TOUR, reason="raw_tour_edited", changed_at=changed_at, tour_id=str(tour_id),
        fields=tuple(fields)), created_by="admin:raw_tour_edit")


def _tour_country(value) -> str:
    """AA-990: raw_tours.country is a shared.countries name (CIS 288 FK): any ISO 3166-1 country, written in its
    canonical spelling ('india' / 'IN' / 'Viet Nam' -> India / India / Vietnam); anything else is a 422, not a 500."""
    from shared import countries
    c = countries.resolve(value if isinstance(value, str) else None)
    if c is None:
        raise HTTPException(status_code=422, detail=f"{value!r} is not an ISO 3166-1 country (shared.countries)")
    return c.name


# ── PATCH /admin/tours/{tour_id}/raw ─────────────────────────────────────────

_ALLOWED_RAW_FIELDS = {
    "src_name", "country", "duration", "group_size", "price_raw",
    "period", "provider", "src_summary", "src_highlights",
    "src_itineraries", "src_description", "inclusions", "exclusions",
}
_JSONB_RAW_FIELDS = {"src_highlights"}


@router.patch("/tours/{tour_id}/raw")
async def update_raw_tour_fields(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    body = await request.json()
    invalid = set(body.keys()) - _ALLOWED_RAW_FIELDS
    if invalid:
        raise HTTPException(status_code=400, detail=f"Fields not allowed: {sorted(invalid)}")
    if not body:
        raise HTTPException(status_code=400, detail="No fields provided")

    import json as _j_raw
    if "country" in body and body["country"] is not None:
        body["country"] = _tour_country(body["country"])
    fields = list(body.keys())
    values = []
    for f in fields:
        v = body[f]
        values.append(_j_raw.dumps(v) if f in _JSONB_RAW_FIELDS and isinstance(v, list) else v)

    set_clause = ", ".join(
        f"{f} = ${i+1}::jsonb" if f in _JSONB_RAW_FIELDS else f"{f} = ${i+1}"
        for i, f in enumerate(fields)
    )
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        updated = await conn.fetchrow(
            f"UPDATE silver_aa_internal.raw_tours SET {set_clause}"
            f" WHERE tour_id = ${len(fields)+1}::uuid RETURNING tour_id, now() AS changed_at",
            *values, tour_id,
        )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Tour {tour_id} not found")
    await _source_fanout_raw(pool, tour_id, updated["changed_at"], fields)   # AA-1052 W6
    return {"tour_id": tour_id, "updated": fields}


# ── PATCH /admin/tours/{tour_id}/generated/{content_id} ──────────────────────

_ALLOWED_GC_FIELDS = {
    "aa_name", "aa_subtitle", "aa_summary", "aa_description", "aa_highlights",
    "aa_itineraries", "mobile_card_text", "seo_title", "seo_meta",
    "seo_keywords_used", "og_tags",
}
_JSONB_GC_FIELDS = {"aa_highlights", "seo_keywords_used", "og_tags"}


# AA-916 WM-05: fields that are writer text. seo_keywords_used is the DataForSEO keyword list the writer was given
# (metadata, not writing), so an edit of it is no writing signal.
_SIGNAL_GC_FIELDS = _ALLOWED_GC_FIELDS - {"seo_keywords_used"}


def _gc_text(field: str, value) -> Optional[str]:
    """A generated_content value as signal text: a text column as is, a JSONB column as canonical JSON (sorted
    keys; asyncpg hands jsonb back as its JSON text, the body as a list / dict)."""
    if value is None or field not in _JSONB_GC_FIELDS:
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


async def _record_field_edit_signals(conn, content_id: str, before, body: dict, fields: list[str],
                                     actor_id: str) -> list[str]:
    """AA-916 WM-05 — one writing signal per writer-text field whose value the admin changed: writer a1, the
    field name, before / after (JSONB as canonical JSON). The AA master's versions are platform signals; a
    tenant's own tour (generated_content.tenant_id another tenant) is tenant text and stays tenant scope (ADR
    0010 decision 10). Same transaction as the UPDATE: the signal is the edit's history, a failure fails the
    edit (no text is lost)."""
    from services.writing_memory import signals as wm
    scope, tenant = wm.scope_for_tenant(before["tenant_id"])
    ids = []
    for f in fields:
        if f not in _SIGNAL_GC_FIELDS:
            continue
        old, new = _gc_text(f, before[f]), _gc_text(f, body[f])
        if old == new:
            continue
        ref = await wm.record_writer_feedback(
            conn, writer="a1", scope=scope, tenant_id=tenant, channel="master", field=f, before_text=old,
            after_text=new, action="field_edit", actor_type="admin", actor_id=str(actor_id)[:128],
            source_table="generated_content", source_id=content_id)
        ids.append(ref.signal_id)
    return ids


@router.patch("/tours/{tour_id}/generated/{content_id}")
async def update_generated_content_fields(
    tour_id: str,
    content_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    body = await request.json()
    invalid = set(body.keys()) - _ALLOWED_GC_FIELDS
    if invalid:
        raise HTTPException(status_code=400, detail=f"Fields not allowed: {sorted(invalid)}")
    if not body:
        raise HTTPException(status_code=400, detail="No fields provided")

    import json as _j_gc
    fields = list(body.keys())
    values = []
    for f in fields:
        v = body[f]
        values.append(_j_gc.dumps(v) if f in _JSONB_GC_FIELDS and isinstance(v, (list, dict)) else v)

    set_clause = ", ".join(
        f"{f} = ${i+1}::jsonb" if f in _JSONB_GC_FIELDS else f"{f} = ${i+1}"
        for i, f in enumerate(fields)
    )
    # AA-234: a human edit invalidates any prior re-validation. Mark human_edited + audit,
    # and reset revalidate_passed to NULL so the approve gate forces a fresh re-validation.
    #
    # AA-232: reviewed_by is now a UUID FK -> shared.admin_users (migration 074). The BFF
    # forwards x-admin-user-id (the verified JWT's `sub` claim) when the caller has a real
    # admin session; use it directly if present and well-formed. Sessions without a JWT
    # (e.g. legacy ADMIN_SECRET login fallback — see login/route.ts) don't send this header,
    # so we fall back to NULL + the free-text handle in edit_diff, same as before.
    reviewer_handle = request.headers.get("x-reviewer-id") or "admin"
    admin_user_id_raw = request.headers.get("x-admin-user-id")
    reviewed_by_value = None
    if admin_user_id_raw:
        try:
            reviewed_by_value = str(UUID(admin_user_id_raw))
        except (ValueError, AttributeError):
            # Malformed header — don't 500, just fall back to the NULL path.
            reviewed_by_value = None

    audit_sql = (", human_edited = true, reviewed_by = $%d::uuid, edited_at = now(),"
                 " edit_diff = $%d::jsonb, revalidate_passed = NULL"
                 % (len(fields) + 1, len(fields) + 2))
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # AA-916 WM-05 — the values before the edit, locked, for one writing signal per changed field.
            before = await conn.fetchrow(
                f"SELECT tenant_id, {', '.join(fields)} FROM silver_aa_internal.generated_content"
                f" WHERE id = $1::uuid AND tour_id = $2::uuid FOR UPDATE",
                content_id, tour_id)
            if before is None:
                raise HTTPException(status_code=404, detail=f"Content {content_id} not found")
            signal_ids = await _record_field_edit_signals(conn, content_id, before, body, fields,
                                                          reviewed_by_value or reviewer_handle)
            edit_diff = {"fields": fields, "reviewer_handle": reviewer_handle}
            if signal_ids:
                edit_diff["signal_ids"] = signal_ids
            updated = await conn.fetchval(
                f"UPDATE silver_aa_internal.generated_content SET {set_clause}{audit_sql}"
                f" WHERE id = ${len(fields)+3}::uuid AND tour_id = ${len(fields)+4}::uuid RETURNING id",
                *values, reviewed_by_value, _j_gc.dumps(edit_diff),
                content_id, tour_id,
            )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Content {content_id} not found")
    # AA-1047 W1: the edited text through the REPORT-mode final contract (it needs a re-validation before an approve;
    # recorded, never blocks). Best-effort: its own connection, after the edit committed.
    async with pool.acquire() as conn:
        await _fc_store.record_s1_exit(conn, content_id, "human_edit")
    return {"content_id": content_id, "updated": fields, "human_edited": True,
            "revalidate_required": True}


@router.post("/tours/{tour_id}/generated/{content_id}/revalidate")
async def revalidate_generated_content(
    tour_id: str,
    content_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """AA-234 / AA-723: async re-validate a human-edited version (202 + job poll).

    Runs build_revalidation_graph (validate+judge+brand_audit, NO flag_fix) on the durable job
    runner + worker, overwrites the version's scores, and sets revalidate_passed. Poll
    GET /admin/jobs/{job_id} for the outcome.
    """
    verify_admin_secret(x_admin_secret)
    from fastapi.responses import JSONResponse
    from shared.jobs.registry import enqueue

    # AA-723: on the durable runner + worker; survives deploys, shows on /admin/job-runner.
    # AA-741 (S229): dedupe against an ACTIVE revalidate of this version only. The old key
    # `revalidate:{content_id}` is unique for ever in shared.job, so a version could be
    # revalidated exactly once: a second human edit + Revalidate got the first (finished) job
    # back and the UI showed its stale result as if it had just run.
    pool = request.app.state.pool
    active = await pool.fetchval("""
        SELECT id FROM shared.job
        WHERE kind = 'revalidate' AND payload->>'content_id' = $1
          AND status IN ('queued', 'running')
        ORDER BY created_at DESC LIMIT 1
    """, content_id)
    if active:
        job_id, created = str(active), False
    else:
        job_id, created = await enqueue(
            pool, "revalidate",
            {"content_id": content_id, "tour_id": tour_id},
            created_by="admin:revalidate")
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "queued" if created else "running",
        "dedup": not created, "poll_url": f"/admin/jobs/{job_id}",
    })
