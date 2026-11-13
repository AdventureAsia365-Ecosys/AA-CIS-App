# api/routers/admin_pipeline/versions.py — tour version routes: promote, source, SEO context, versions export, DOCX.
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import json
from fastapi import Header, HTTPException, Request
from api.routers.admin import verify_admin_secret
from shared.tour_liveness import SOURCE_NOT_LIVE, SourceNotLiveError, source_not_live_message, source_state

from api.routers.admin_pipeline._shared import _as_list, logger, router
from api.routers.admin_pipeline.uploads import _lifecycle_http


# ── Tour version endpoints ────────────────────────────────────────────────────
# NOTE: /versions/{num}/promote MUST come before /versions/{num} — FastAPI greedy matching

@router.post("/tours/{tour_id}/versions/{version_num}/promote")
async def promote_tour_version(
    tour_id: str, version_num: int,
    request: Request, x_admin_secret: str = Header(None),
):
    """Make version `version_num` the tour's live Master Content again (the per-tour rollback).

    AA-645 step 0 (C2): this used to copy only aa_name / aa_subtitle / the score onto published_tours and reset
    published_at, so the page mixed two versions and the atoms stayed those of the replaced version. It now publishes
    the version through the one publish path (`process_export`: every text and SEO field, the accepted score
    pointer, the live-source rule) and re-atomizes it with `dedupe=False` + `force=True` (the version's own
    `a3_atomize:<id>` key and day fingerprints are already spent). Atom ids are content hashes, so the old text
    brings the old atoms back and the replaced version's atoms are soft-deleted. published_at is kept (d06); the
    change is recorded in published_tours.updated_at.

    AA-986 L2: process_export writes the Master through the tour lifecycle service (`services.tour_lifecycle.publish`,
    the only writer of published_tours inserts): the source row is locked FOR SHARE and re-checked there, so a retire
    that commits between the check below and the publish is refused (process_export turns it into the L1 refusal,
    SourceNotLiveError → 409 source_not_live). The d06 rule (first published_at kept, updated_at = now()) is the
    ON CONFLICT branch of the repository insert that service runs.

    Only an `approved` version can be promoted (the publish gate of every other path): a version that never passed
    the gate goes through the Review Queue. 404 unknown tour / version, 409 not approved or source not live."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        gc = await conn.fetchrow("""
            SELECT gc.id::text AS id, gc.status::text AS status
            FROM silver_aa_internal.generated_content gc
            WHERE gc.tour_id = $1::uuid AND gc.version_num = $2
        """, tour_id, version_num)
        if not gc:
            raise HTTPException(status_code=404, detail="Tour or version not found")
        if gc["status"] != "approved":
            raise HTTPException(
                status_code=409,
                detail=(f"v{version_num} is '{gc['status']}', not approved: only a version that passed the publish "
                        "gate can be made current. Approve it in the Review Queue instead."),
            )
        src = await source_state(conn, tour_id)
        if not src.live:
            raise HTTPException(status_code=409, detail=source_not_live_message(src, "promote this version"),
                                headers={"X-Error-Code": SOURCE_NOT_LIVE})

    from services.export.handler import process_export
    from services.tour_lifecycle import LifecycleError
    from services.tour_lifecycle.policy import POLICY_REVIEW_REQUIRED, PolicyReviewRequiredError
    try:
        exported = await process_export(gc["id"], atomize_dedupe=False, atomize_force=True,
                                        atomize_created_by="admin:promote_version", contract_exit="promote")
    except SourceNotLiveError as e:   # the source was retired between the check and the (locked) publish
        raise HTTPException(status_code=409, detail=source_not_live_message(e.state, "promote this version"),
                            headers={"X-Error-Code": SOURCE_NOT_LIVE})
    except ValueError:                # the version left 'approved' between the check and the publish
        raise HTTPException(status_code=409, detail=f"v{version_num} is no longer approved")
    except PolicyReviewRequiredError as e:   # AA-1049: elephant riding, no person approved this version
        raise HTTPException(status_code=409, detail=f"{e} (v{version_num} is now in the Review Queue)",
                            headers={"X-Error-Code": POLICY_REVIEW_REQUIRED})
    except LifecycleError as e:       # any other refusal of the lifecycle service's publish
        raise _lifecycle_http(e)
    logger.info("tour_version_promoted", tour_id=tour_id, version_num=version_num, version_id=gc["id"],
                atomize_job_id=exported.get("atomize_job_id"))
    return {"promoted": True, "published_tour_id": exported["catalog_id"], "version_num": version_num,
            "version_id": gc["id"], "atomize_job_id": exported.get("atomize_job_id")}


@router.get("/tours/{tour_id}/source")
async def get_tour_source(
    tour_id: str,
    request: Request, x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT rt.tour_id, rt.src_name, rt.src_subtitle, rt.src_summary,
                   rt.src_description, rt.src_highlights, rt.src_itineraries,
                   rt.country, rt.duration, rt.price_raw, rt.ingest_at AS created_at,
                   rt.group_size, rt.period, rt.provider, rt.inclusions, rt.exclusions,
                   sc.top_keywords
            FROM silver_aa_internal.raw_tours rt
            LEFT JOIN LATERAL (
                SELECT top_keywords FROM silver_aa_internal.seo_context
                WHERE tour_id = rt.tour_id
                ORDER BY fetched_at DESC LIMIT 1
            ) sc ON true
            WHERE rt.tour_id = $1::uuid
        """, tour_id)
    if not row:
        raise HTTPException(status_code=404, detail="Source tour not found")
    try:
        highlights = row["src_highlights"]
        if not isinstance(highlights, list):
            highlights = json.loads(highlights) if highlights else []
    except Exception:
        highlights = []
    try:
        keywords = row["top_keywords"]
        if not isinstance(keywords, list):
            keywords = json.loads(keywords) if keywords else []
    except Exception:
        keywords = []
    return {
        "id":             str(row["tour_id"]),
        "version_num":    0,
        "model_id":       "source",
        "quality_score":  None,
        "score_brand":    None,
        "score_seo":      None,
        "score_structure": None,
        "created_at":     row["created_at"].isoformat() if row["created_at"] else None,
        "aa_name":        row["src_name"],
        "aa_subtitle":    row["src_subtitle"],
        "aa_summary":     row["src_summary"],
        "aa_description": row["src_description"],
        "aa_highlights":  highlights,
        "aa_itineraries": row["src_itineraries"],
        "seo_title":      None,
        "seo_meta":       None,
        "brand_name":     "original",
        "seo_mode":       None,
        "dataforseo_used": False,
        "llm_cost_usd":   None,
        "top_keywords":   keywords,
        "country":        row["country"],
        "duration":       row["duration"],
        "group_size":     row["group_size"],
        "price_raw":      row["price_raw"],
        "period":         row["period"],
        "provider":       row["provider"],
        "inclusions":     row["inclusions"],
        "exclusions":     row["exclusions"],
    }

@router.get("/tours/{tour_id}/seo-context")
async def get_tour_seo_context(
    tour_id: str,
    request: Request, x_admin_secret: str = Header(None),
):
    # AA-203: return the DFS keyword-ideas row for a tour's seed. Decision (A):
    # match the stored keyword_search by the tour's country — do NOT re-build a seed.
    # Always 200 (UI gets has_data:false when nothing stored) — never 404.
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tour = await conn.fetchrow(
            "SELECT tenant_id, country FROM silver_aa_internal.raw_tours "
            "WHERE tour_id = $1::uuid",
            tour_id,
        )
        if not tour:
            raise HTTPException(status_code=404, detail="Tour not found")

        country = tour["country"]
        if country:
            row = await conn.fetchrow("""
                SELECT keyword_search, provider, keyword_ideas, top_keywords,
                       demographics, trends, people_also_ask, related_keywords,
                       fetched_at, expires_at
                FROM silver_aa_internal.seo_context
                WHERE tenant_id = $1 AND keyword_search ILIKE '%' || $2 || '%'
                ORDER BY fetched_at DESC LIMIT 1
            """, tour["tenant_id"], country)
        else:
            # No country on the tour — fall back to this tour's own row.
            row = await conn.fetchrow("""
                SELECT keyword_search, provider, keyword_ideas, top_keywords,
                       demographics, trends, people_also_ask, related_keywords,
                       fetched_at, expires_at
                FROM silver_aa_internal.seo_context
                WHERE tour_id = $1::uuid
                ORDER BY fetched_at DESC LIMIT 1
            """, tour_id)

        if not row:
            return {
                "has_data": False,
                "seed": None,
                "buyer_market": None,
                "buyer_market_location_code": None,
                "keyword_ideas": [],
                "top_keywords": [],
                "people_also_ask": [],
                "related_keywords": [],
                "fetched_at": None,
                "notes": "No seo_context row for this tour's seed yet.",
            }

        # keyword_ideas / top_keywords are stored as json.dumps strings → parse to list
        # (AA-235: shape guard via module-level _as_list).
        keyword_ideas = _as_list(row["keyword_ideas"])
        top_keywords = _as_list(row["top_keywords"])
        people_also_ask = _as_list(row["people_also_ask"])
        related_keywords = _as_list(row["related_keywords"])

        # Buyer market: derive from tenant target_market if cheap; else null.
        buyer_market = None
        buyer_market_location_code = None
        try:
            from shared.services.tenant_config_service import TenantConfigService
            from services.seo_intelligence.seed_builder import resolve_buyer_market
            cfg = await TenantConfigService(conn).get_seo_config(str(tour["tenant_id"]))
            loc_code, loc_name, _lang = resolve_buyer_market(cfg.target_market)
            buyer_market = loc_name
            buyer_market_location_code = loc_code
        except Exception as _bm_err:
            logger.warning("seo_context_buyer_market_failed", tour_id=tour_id, error=str(_bm_err))

    return {
        "has_data": True,
        "seed": row["keyword_search"],
        "buyer_market": buyer_market,
        "buyer_market_location_code": buyer_market_location_code,
        "keyword_ideas": keyword_ideas,
        "top_keywords": top_keywords,
        # AA-218: persisted on seo_context — read the real columns.
        "people_also_ask": people_also_ask,
        "related_keywords": related_keywords,
        "fetched_at": row["fetched_at"].isoformat() if row["fetched_at"] else None,
        "notes": None,
    }


@router.get("/tours/{tour_id}/versions/export")
async def export_tour_versions(
    tour_id: str,
    request: Request,
    versions: str = "",
    format: str = "xlsx",
    x_admin_secret: str = Header(None),
):
    """AA-220 (A): horizontal field×version comparison export (CSV/XLSX).

    Reuses the get_tour_version_detail SELECT but for N versions at once
    (version_num = ANY) → transpose into a table where each row is a field
    and each column is one version. MUST be registered ABOVE
    /versions/{version_num} — version_num is typed int, so "export" would 422
    there instead of falling through to this route.
    """
    verify_admin_secret(x_admin_secret)
    if format not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="format must be csv or xlsx")
    try:
        version_list = [int(v.strip()) for v in versions.split(",") if v.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="versions must be comma-separated ints")
    if not version_list:
        raise HTTPException(status_code=400, detail="versions query param is required")

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT gc.version_num, gc.model_editorial AS model_id,
                   qs.score_overall, qs.score_brand, qs.score_seo,
                   qs.score_structure, qs.score_quality, qs.failure_codes,
                   qs.brand_audit_status, qs.brand_audit_codes, qs.brand_audit_issues,
                   gc.fix_pass_applied, gc.fix_pass_fields, gc.metadata,
                   gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description,
                   gc.aa_highlights, gc.aa_itineraries, gc.seo_title, gc.seo_meta,
                   tbr.brand_name AS brand_name
            FROM silver_aa_internal.generated_content gc
            LEFT JOIN silver_aa_internal.quality_scores qs
                ON qs.generated_content_id = gc.id
            LEFT JOIN shared.tenant_brand_rules tbr
                ON tbr.id = NULLIF(gc.metadata->>'brand_rule_id', '')::uuid
            WHERE gc.tour_id = $1::uuid AND gc.version_num = ANY($2::int[])
            ORDER BY gc.version_num
        """, tour_id, version_list)
    if not rows:
        raise HTTPException(status_code=404, detail="No versions found")

    def _num(v):
        return float(v) if v is not None else None

    def _join(v):
        items = _as_list(v)
        return ", ".join(str(x) for x in items) if items else ""

    def _hl(v):
        items = _as_list(v)
        return "\n".join(f"- {x}" for x in items) if items else ""

    columns: dict = {}
    for row in rows:
        meta = {}
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"])
            except Exception:
                meta = {}
        judge = meta.get("judge") if isinstance(meta, dict) else None
        judge = judge if isinstance(judge, dict) else {}
        col = {
            "brand_name":         row["brand_name"] or meta.get("brand_rule_id") and "custom" or "default",
            "model":              row["model_id"],
            "version_num":        row["version_num"],
            "name":               row["aa_name"],
            "subtitle":           row["aa_subtitle"],
            "summary":            row["aa_summary"],
            "highlights":         _hl(row["aa_highlights"]),
            "itineraries":        row["aa_itineraries"],
            "seo_title":          row["seo_title"],
            "seo_meta":           row["seo_meta"],
            "score_brand":        _num(row["score_brand"]),
            "score_seo":          _num(row["score_seo"]),
            "score_structure":    _num(row["score_structure"]),
            "score_quality":      _num(row["score_quality"]),
            "score_overall":      _num(row["score_overall"]),
            "judge_fit":          judge.get("brand_fit"),
            "judge_distinct":     judge.get("distinct"),
            "judge_mission":      judge.get("mission_present"),
            "judge_score":        judge.get("judge_score"),
            "judge_feedback":     judge.get("feedback"),
            "brand_audit_status": row["brand_audit_status"],
            "brand_audit_codes":  _join(row["brand_audit_codes"]),
            "fix_pass_fields":    _join(row["fix_pass_fields"]),
            "failure_codes":      _join(row["failure_codes"]),
            "revalidate_ran":     bool(meta.get("revalidate_ran")) if isinstance(meta, dict) else False,
            "revalidate_passed":  bool(meta.get("revalidate_passed")) if isinstance(meta, dict) else False,
            "cost":               meta.get("llm_cost_usd"),
        }
        columns[f"v{row['version_num']}"] = col

    field_order = [
        "brand_name", "model", "version_num", "name", "subtitle", "summary",
        "highlights", "itineraries", "seo_title", "seo_meta",
        "score_brand", "score_seo", "score_structure", "score_quality", "score_overall",
        "judge_fit", "judge_distinct", "judge_mission", "judge_score", "judge_feedback",
        "brand_audit_status", "brand_audit_codes", "fix_pass_fields", "failure_codes",
        "revalidate_ran", "revalidate_passed", "cost",
    ]

    import io
    import pandas as pd
    from fastapi.responses import StreamingResponse

    table = {"field": field_order}
    for col_name, col in columns.items():
        table[col_name] = [col.get(f) for f in field_order]
    df = pd.DataFrame(table)

    short = tour_id.split("-")[0]
    if format == "csv":
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=tour_{short}_versions_compare.csv"},
        )

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Version Compare", freeze_panes=(1, 1))
    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=tour_{short}_versions_compare.xlsx"},
    )


@router.get("/tours/{tour_id}/versions/{version_num}/export-docx")
async def export_tour_version_docx(
    tour_id: str, version_num: int,
    request: Request, x_admin_secret: str = Header(None),
):
    """AA-220 (B): single-version DOCX report — vertical layout, AAA palette, full DFS.

    Registered ABOVE /versions/{version_num} for the same int-422 reason as endpoint A.
    Unlike A, B joins seo_context (LATERAL) because the DOCX surfaces the full DFS block
    (seed/buyer_market/keyword_ideas/top_keywords/PAA/related).
    """
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT gc.id, gc.tenant_id, gc.version_num, gc.model_editorial AS model_id,
                   qs.score_overall, qs.score_brand, qs.score_seo, qs.score_structure,
                   qs.score_quality, qs.failure_codes,
                   qs.brand_audit_status, qs.brand_audit_codes, qs.brand_audit_issues,
                   gc.fix_pass_applied, gc.fix_pass_fields, gc.created_at,
                   gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description,
                   gc.aa_highlights, gc.aa_itineraries, gc.seo_title, gc.seo_meta,
                   gc.metadata,
                   tbr.brand_name AS brand_name,
                   sc.top_keywords, sc.keyword_ideas, sc.people_also_ask,
                   sc.related_keywords, sc.keyword_search
            FROM silver_aa_internal.generated_content gc
            LEFT JOIN silver_aa_internal.quality_scores qs
                ON qs.generated_content_id = gc.id
            LEFT JOIN shared.tenant_brand_rules tbr
                ON tbr.id = NULLIF(gc.metadata->>'brand_rule_id', '')::uuid
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

        # Buyer market: derive from tenant target_market (best-effort, inside conn scope).
        buyer_market = None
        try:
            from shared.services.tenant_config_service import TenantConfigService
            from services.seo_intelligence.seed_builder import resolve_buyer_market
            cfg = await TenantConfigService(conn).get_seo_config(str(row["tenant_id"]))
            _code, buyer_market, _lang = resolve_buyer_market(cfg.target_market)
        except Exception as _bm_err:
            logger.warning("docx_buyer_market_failed", tour_id=tour_id, error=str(_bm_err))

    meta = {}
    if row["metadata"]:
        try:
            meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"])
        except Exception:
            meta = {}
    judge = meta.get("judge") if isinstance(meta, dict) else None
    judge = judge if isinstance(judge, dict) else {}
    brand_name = row["brand_name"] or meta.get("brand_rule_id") and "custom" or "default"
    model = row["model_id"]
    created = row["created_at"].strftime("%Y-%m-%d %H:%M") if row["created_at"] else ""

    highlights = _as_list(row["aa_highlights"])
    keyword_ideas = _as_list(row["keyword_ideas"])
    top_keywords = _as_list(row["top_keywords"])
    paa = _as_list(row["people_also_ask"])
    related = _as_list(row["related_keywords"])

    import io
    from docx import Document
    from docx.shared import Pt, RGBColor
    from fastapi.responses import StreamingResponse

    ORANGE = RGBColor(0xDB, 0x96, 0x28)     # heading-accent
    BLACKBLUE = RGBColor(0x1F, 0x29, 0x33)  # section headers
    GRAY = RGBColor(0x33, 0x36, 0x3D)       # body

    doc = Document()

    # Off-white page background (best-effort — Word ignores silently if unsupported).
    try:
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
        bg = OxmlElement("w:background")
        bg.set(qn("w:color"), "F8F6F2")
        doc.element.insert(0, bg)
        disp = OxmlElement("w:displayBackgroundShape")
        doc.settings.element.append(disp)
    except Exception:
        pass

    def _section(text):
        p = doc.add_paragraph()
        bar = p.add_run("▌ ")
        bar.bold = True
        bar.font.color.rgb = ORANGE
        r = p.add_run(text)
        r.bold = True
        r.font.size = Pt(13)
        r.font.color.rgb = BLACKBLUE
        return p

    def _kv(label, value):
        p = doc.add_paragraph()
        lr = p.add_run(f"{label}: ")
        lr.bold = True
        lr.font.size = Pt(10.5)
        lr.font.color.rgb = BLACKBLUE
        vr = p.add_run("" if value is None else str(value))
        vr.font.size = Pt(10.5)
        vr.font.color.rgb = GRAY
        return p

    def _body(text):
        # Preserve \n as soft line breaks within a single paragraph (no flatten).
        p = doc.add_paragraph()
        for i, line in enumerate(str(text or "").split("\n")):
            if i > 0:
                p.add_run().add_break()
            r = p.add_run(line)
            r.font.size = Pt(10.5)
            r.font.color.rgb = GRAY
        return p

    # ── Header
    title = doc.add_paragraph()
    tr = title.add_run(row["aa_name"] or "(untitled)")
    tr.bold = True
    tr.font.size = Pt(20)
    tr.font.color.rgb = BLACKBLUE
    sub = doc.add_paragraph()
    sr = sub.add_run(f"Version {version_num}   ·   {brand_name}   ·   {model}   ·   {created}")
    sr.font.size = Pt(10)
    sr.font.color.rgb = ORANGE

    # ── Scores & Judge
    _section("Scores & Judge")
    _kv("Brand", row["score_brand"])
    _kv("SEO", row["score_seo"])
    _kv("Structure", row["score_structure"])
    _kv("Quality", row["score_quality"])
    _kv("Overall", row["score_overall"])
    _kv("Judge — brand fit", judge.get("brand_fit"))
    _kv("Judge — distinct", judge.get("distinct"))
    _kv("Judge — mission present", judge.get("mission_present"))
    _kv("Judge — score", judge.get("judge_score"))
    if judge.get("feedback"):
        _body(judge.get("feedback"))

    # ── Brand Audit
    _section("Brand Audit")
    _kv("Status", row["brand_audit_status"])
    _kv("Codes", ", ".join(str(c) for c in _as_list(row["brand_audit_codes"])) or "—")
    _kv("Issues", ", ".join(str(c) for c in _as_list(row["brand_audit_issues"])) or "—")
    _kv("Fix-pass fields", ", ".join(str(c) for c in _as_list(row["fix_pass_fields"])) or "—")
    _kv("Failure codes", ", ".join(str(c) for c in _as_list(row["failure_codes"])) or "—")
    _kv("Revalidate ran", bool(meta.get("revalidate_ran")))
    _kv("Revalidate passed", bool(meta.get("revalidate_passed")))

    # ── SEO / DataForSEO
    _section("SEO / DataForSEO")
    _kv("Seed", row["keyword_search"] or "—")
    _kv("Buyer market", buyer_market or "—")
    if keyword_ideas:
        tbl = doc.add_table(rows=1, cols=5)
        tbl.style = "Light Grid Accent 1"
        for cell, label in zip(
            tbl.rows[0].cells,
            ["Keyword", "Volume", "Competition", "Comp. Index", "CPC"],
        ):
            cr = cell.paragraphs[0].add_run(label)
            cr.bold = True
        for kw in keyword_ideas[:25]:
            cells = tbl.add_row().cells
            if isinstance(kw, dict):
                cells[0].text = str(kw.get("keyword") or "")
                cells[1].text = "" if kw.get("search_volume") is None else str(kw.get("search_volume"))
                cells[2].text = str(kw.get("competition") or "")
                cells[3].text = "" if kw.get("competition_index") is None else str(kw.get("competition_index"))
                cells[4].text = "" if kw.get("cpc") is None else str(kw.get("cpc"))
            else:
                cells[0].text = str(kw)
    if top_keywords:
        _kv("Top keywords", ", ".join(
            str(k.get("keyword") if isinstance(k, dict) else k) for k in top_keywords))
    if paa:
        _kv("People also ask", "")
        for q in paa:
            doc.add_paragraph(str(q), style="List Bullet")
    if related:
        _kv("Related keywords", ", ".join(str(r) for r in related))

    # ── Content
    _section("Content")
    _kv("Name", row["aa_name"])
    _kv("Subtitle", row["aa_subtitle"])
    _section("Summary")
    _body(row["aa_summary"])
    _section("Highlights")
    for h in highlights:
        doc.add_paragraph(str(h), style="List Bullet")
    _section("Itineraries")
    _body(row["aa_itineraries"])
    _section("SEO")
    _kv("SEO Title", row["seo_title"])
    _kv("SEO Meta", row["seo_meta"])
    _kv("SEO Meta length", f"{len(row['seo_meta'] or '')} chars")

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    short = tour_id.split("-")[0]
    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename=tour_{short}_v{version_num}.docx"},
    )
