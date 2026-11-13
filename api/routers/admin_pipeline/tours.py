# api/routers/admin_pipeline/tours.py — tour list routes: ready, trashed, all tours, CSV export, export audit.
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

from fastapi import Header, HTTPException, Request
from typing import Optional
from api.routers.admin import verify_admin_secret

from api.routers.admin_pipeline._shared import router


# ── GET /admin/tours-ready ────────────────────────────────────────────────────

@router.get("/tours-ready")
async def get_tours_ready(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tours = await conn.fetch("""
            SELECT t.tour_id::text, t.src_name, t.country, t.ingest_at,
                   t.source_id::text, t.batch_id::text, rs.filename,
                   t.source_status::text AS source_status
            FROM silver_aa_internal.raw_tours t
            LEFT JOIN silver_aa_internal.raw_sources rs ON rs.id = t.source_id
            WHERE t.pipeline_status = 'ingested'
              -- AA-702: only the active version of a source group is rewritable; superseded
              -- rows (older re-uploads, S206 near-duplicates) are handled in Dup Review only.
              AND t.source_status = 'active'
              -- AA-604: apply the SAME itinerary floor as S1 get_all_tours (and
              -- acp_contract.v_trip_registry / AA-345) so "Tours Ready for Rewrite"
              -- matches the S1 count. A row with no itinerary body cannot be rewritten,
              -- so it must not be counted as "ready" here (was: S0=793 vs S1=763).
              AND t.deleted_at IS NULL
              AND t.src_itineraries IS NOT NULL
              AND TRIM(BOTH FROM t.src_itineraries) <> ''
            ORDER BY t.ingest_at DESC
        """)
    return {
        "tours": [
            {
                "tour_id":       t["tour_id"],
                "src_name":      t["src_name"],
                "country":       t["country"],
                "ingest_at":     str(t["ingest_at"]) if t["ingest_at"] else None,
                "source_id":     t["source_id"],
                "batch_id":      t["batch_id"],
                "filename":      t["filename"],
                "source_status": t["source_status"] or "active",
            }
            for t in tours
        ],
        "total": len(tours),
    }


# ── GET /admin/tours-trashed ──────────────────────────────────────────────────


@router.get("/tours-trashed")
async def get_tours_trashed(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tours = await conn.fetch("""
            SELECT t.tour_id::text, t.src_name, t.country, t.ingest_at,
                   t.source_id::text, t.batch_id::text, rs.filename,
                   t.deleted_at, t.source_status::text AS source_status
            FROM silver_aa_internal.raw_tours t
            LEFT JOIN silver_aa_internal.raw_sources rs ON rs.id = t.source_id
            WHERE t.source_status::text = 'trashed'
            ORDER BY t.deleted_at DESC NULLS LAST
        """)
    return {
        "tours": [
            {
                "tour_id":       t["tour_id"],
                "src_name":      t["src_name"],
                "country":       t["country"],
                "ingest_at":     str(t["ingest_at"]) if t["ingest_at"] else None,
                "source_id":     t["source_id"],
                "batch_id":      t["batch_id"],
                "filename":      t["filename"],
                "source_status": t["source_status"],
                "deleted_at":    t["deleted_at"].isoformat() if t["deleted_at"] else None,
            }
            for t in tours
        ],
        "total": len(tours),
    }


# ── GET /admin/tours — all raw_tours with rewrite_count ──────────────────────

@router.get("/tours")
async def get_all_tours(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # AA-345: floor copied verbatim from acp_contract.v_trip_registry
        # (migration 083) — NULL-safe on source_status (a bare `!= 'trashed'`
        # silently drops every NULL row, three-valued SQL logic) + excludes
        # deleted/empty-itinerary rows. Published tours are NOT excluded here
        # (open product question, AA-345 STEP 0 Phần 1 — not enough evidence
        # either way, Nghiep to confirm) — pipeline_status is still returned
        # below and the S1-rewrite frontend already badges it "Published"
        # (frontend/app/admin/s1-rewrite/page.tsx), so no separate is_published
        # field is added here — that would be a second, driftable source of
        # truth for the same fact.
        tours = await conn.fetch("""
            SELECT
                rt.tour_id::text, rt.src_name, rt.country,
                rt.pipeline_status::text, rt.ingest_at,
                rt.batch_id::text, rt.source_id::text,
                rs.filename,
                COUNT(gc.id)         AS rewrite_count,
                MAX(gc.created_at)   AS last_rewritten_at,
                -- S218: a written tour waiting in Review Queue is not "Ready" — S1 badges it.
                EXISTS (SELECT 1 FROM silver_aa_internal.review_queue q
                        WHERE q.tour_id = rt.tour_id AND q.review_status = 'pending') AS in_review
            FROM silver_aa_internal.raw_tours rt
            LEFT JOIN silver_aa_internal.raw_sources rs ON rs.id = rt.source_id
            LEFT JOIN silver_aa_internal.generated_content gc ON gc.tour_id = rt.tour_id
            -- AA-702: active only (was: not trashed) — superseded duplicates were offered for
            -- rewrite (S1 showed 761 = 736 active + 25 superseded).
            WHERE rt.source_status = 'active'
              AND rt.deleted_at IS NULL
              AND rt.src_itineraries IS NOT NULL
              AND TRIM(BOTH FROM rt.src_itineraries) <> ''::text
            GROUP BY rt.tour_id, rt.src_name, rt.country, rt.pipeline_status,
                     rt.ingest_at, rt.batch_id, rt.source_id, rs.filename
            ORDER BY rt.ingest_at DESC
        """)
    return {
        "tours": [
            {
                "tour_id":           t["tour_id"],
                "src_name":          t["src_name"],
                "country":           t["country"],
                "pipeline_status":   t["pipeline_status"],
                "ingest_at":         str(t["ingest_at"]) if t["ingest_at"] else None,
                "source_id":         t["source_id"],
                "batch_id":          t["batch_id"],
                "filename":          t["filename"],
                "rewrite_count":     int(t["rewrite_count"]),
                "last_rewritten_at": str(t["last_rewritten_at"]) if t["last_rewritten_at"] else None,
                "in_review":         bool(t["in_review"]),
            }
            for t in tours
        ],
        "total": len(tours),
    }


# ── GET /admin/tours/export ──────────────────────────────────────────────────
# NOTE: /tours/export MUST come before /tours/{tour_id}/... — FastAPI greedy matching

@router.get("/tours/export")
async def export_tours(
    request: Request,
    format: str = "csv",
    tour_ids: Optional[str] = None,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    if format not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="format must be csv or xlsx")

    # AA-220 (D): optional tour_ids filter. Absent → no WHERE (back-compat, all tours).
    # Present → WHERE rt.tour_id = ANY(...) BEFORE GROUP BY (pre-aggregation, keeps
    # rewrite_count / published join correct per remaining tour).
    params: list = []
    where = ""
    if tour_ids:
        ids = [t.strip() for t in tour_ids.split(",") if t.strip()]
        if ids:
            params.append(ids)
            where = f"WHERE rt.tour_id = ANY(${len(params)}::uuid[])"

    query = f"""
        SELECT
            rt.tour_id::text, rt.src_name, rt.country,
            rt.pipeline_status::text, rt.ingest_at,
            rs.filename,
            COUNT(gc.id)       AS rewrite_count,
            MAX(gc.created_at) AS last_rewritten_at,
            pt.aa_name, pt.aa_subtitle, pt.quality_score, pt.published_at
        FROM silver_aa_internal.raw_tours rt
        LEFT JOIN silver_aa_internal.raw_sources rs ON rs.id = rt.source_id
        LEFT JOIN silver_aa_internal.generated_content gc ON gc.tour_id = rt.tour_id
        LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id
        {where}
        GROUP BY rt.tour_id, rt.src_name, rt.country, rt.pipeline_status,
                 rt.ingest_at, rs.filename,
                 pt.aa_name, pt.aa_subtitle, pt.quality_score, pt.published_at
        ORDER BY rt.ingest_at DESC
    """
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    import io
    import pandas as pd
    from fastapi.responses import StreamingResponse

    data = [
        {
            "tour_id":          r["tour_id"],
            "src_name":         r["src_name"],
            "aa_name":          r["aa_name"] or "",
            "aa_subtitle":      r["aa_subtitle"] or "",
            "country":          r["country"] or "",
            "pipeline_status":  r["pipeline_status"],
            "rewrite_count":    int(r["rewrite_count"]),
            "quality_score":    float(r["quality_score"]) if r["quality_score"] else None,
            "filename":         r["filename"] or "",
            "ingest_at":        str(r["ingest_at"]) if r["ingest_at"] else "",
            "last_rewritten_at": str(r["last_rewritten_at"]) if r["last_rewritten_at"] else "",
            "published_at":     str(r["published_at"]) if r["published_at"] else "",
        }
        for r in rows
    ]
    df = pd.DataFrame(data)

    if format == "csv":
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=master_content_export.csv"},
        )

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Master Content")
    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=master_content_export.xlsx"},
    )


# ── GET /admin/export-audit — AA-134 full 28-col export with brand audit data ──
# NOTE: Route must come before /tours/{tour_id}/... — placed here with other exports

@router.get("/export-audit")
async def export_audit(
    request: Request,
    format: str = "csv",
    batch_id: Optional[str] = None,
    tour_ids: Optional[str] = None,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    if format not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="format must be csv or xlsx")

    tenant_id = "00000000-0000-0000-0000-000000000001"
    base_query = """
        SELECT
            pt.tour_id::text                                AS tour_id,
            rt.sku                                          AS "SKU",
            pt.aa_name                                      AS "NAME",
            pt.aa_subtitle                                  AS "SUBTITLE",
            rt.country                                      AS country,
            rt.src_name                                     AS "SOURCE_NAME",
            rt.src_subtitle                                 AS "SOURCE_SUBTITLE",
            rt.src_summary                                  AS "SOURCE_SUMMARY",
            rt.src_highlights::text                         AS "SOURCE_HIGHLIGHTS",
            rt.src_itineraries                              AS "SOURCE_ITINERARIES",
            pt.aa_name                                      AS "AA_NAME",
            pt.aa_subtitle                                  AS "AA_SUBTITLE",
            pt.aa_summary                                   AS "AA_SUMMARY",
            pt.aa_highlights::text                          AS "AA_HIGHLIGHTS",
            pt.aa_itineraries                               AS "AA_ITINERARIES",
            pt.seo_title                                    AS "SEO_TITLE",
            pt.seo_meta                                     AS "SEO_META",
            COALESCE(qs.brand_audit_status, 'not_audited') AS "AUDIT_STATUS",
            COALESCE(qs.brand_audit_codes::text, '[]')     AS "AUDIT_FAILURE_CODES",
            COALESCE(qs.brand_audit_issues::text, '[]')    AS "AUDIT_ISSUES",
            COALESCE(gc.fix_pass_fields::text, '[]')       AS "FIELDS_UPDATED",
            CASE
                WHEN qs.brand_audit_status = 'pass'
                  OR (qs.brand_audit_status = 'flagged' AND gc.fix_pass_applied = true)
                THEN 'TRUE'
                WHEN qs.brand_audit_status = 'manual_check' THEN 'FALSE'
                WHEN qs.brand_audit_status IS NULL           THEN 'NOT_AUDITED'
                ELSE 'FALSE'
            END                                             AS "PUBLISH_READY",
            COALESCE(sc.keyword_search, '')                 AS "DFS_KEYWORD_SEARCH",
            qs.score_overall::text                          AS quality_score
        FROM gold_aa_internal.published_tours pt
        JOIN silver_aa_internal.raw_tours rt
            ON rt.tour_id = pt.tour_id
        LEFT JOIN silver_aa_internal.quality_scores qs
            ON qs.generated_content_id = pt.generated_content_id
        LEFT JOIN silver_aa_internal.generated_content gc
            ON gc.id = pt.generated_content_id
        LEFT JOIN silver_aa_internal.seo_context sc
            ON sc.tour_id = pt.tour_id
        WHERE pt.tenant_id = $1::uuid
    """

    params: list = [tenant_id]
    extra = ""
    if batch_id:
        params.append(batch_id)
        extra += f" AND rt.batch_id = ${len(params)}::uuid"
    if tour_ids:
        ids = [t.strip() for t in tour_ids.split(",") if t.strip()]
        if ids:
            params.append(ids)
            extra += f" AND pt.tour_id = ANY(${len(params)}::uuid[])"

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(base_query + extra + " ORDER BY pt.published_at DESC", *params)

    import csv as _csv
    import io as _io
    import pandas as _pd
    from fastapi.responses import StreamingResponse

    data = [dict(r) for r in rows]

    if format == "csv":
        buf = _io.StringIO()
        if data:
            writer = _csv.DictWriter(buf, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=aa_tours_export.csv"},
        )

    df = _pd.DataFrame(data)
    buf = _io.BytesIO()
    with _pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="AA Tours Audit")
    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=aa_tours_export.xlsx"},
    )
