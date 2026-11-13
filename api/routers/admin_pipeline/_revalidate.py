# api/routers/admin_pipeline/_revalidate.py — the S1 re-validation of an edited version.
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import asyncio
import json
import asyncpg
from shared.llm_client import call_log as _call_log  # AA-1058: the tour every S1 ledger row names
from shared.secrets import get_database_url  # AA-838 D9: default application_name
from services.quality import final_contract as _fcon  # AA-1047 W1 (REPORT-mode exit contract)
from services.quality import final_contract_store as _fc_store  # AA-1047 W1

from api.routers.admin_pipeline._shared import _resolve_brand_rule, logger
from api.routers.admin_pipeline._engine import _rowcount


# ── AA-234 Phần A: re-validate a human-edited generated_content version ────────
async def _revalidate_tour(content_id: str) -> dict:
    """Re-validate a single human-edited generated_content version in place.

    Reads the edited content + its tour + seo_context (by tour_id, same keywords — NO
    DFS refetch) + brand rule, runs build_revalidation_graph, then overwrites the
    version's quality_scores rows and sets generated_content.revalidate_passed. A published tour of
    this version keeps pointing at the version's score: its quality_score_id moves to the new row
    (AA-983), in the same transaction.
    """
    from services.content_generation.graph import build_revalidation_graph

    conn = await asyncpg.connect(get_database_url())
    try:
        gc = await conn.fetchrow("""
            SELECT gc.id, gc.tour_id, gc.tenant_id, gc.version_num,
                   gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description,
                   gc.aa_highlights, gc.aa_itineraries, gc.mobile_card_text,
                   gc.seo_title, gc.seo_meta, gc.seo_keywords_used, gc.og_tags,
                   gc.brand_rules_version, gc.metadata,
                   rt.src_name, rt.country, rt.duration,
                   rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights,
                   rt.src_itineraries, rt.price_raw, rt.inclusions, rt.exclusions,
                   rt.group_size, rt.best_time_to_go
            FROM silver_aa_internal.generated_content gc
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
            WHERE gc.id = $1::uuid
        """, content_id)
        if not gc:
            raise ValueError(f"generated_content {content_id} not found")

        # AA-293: this function's own asyncpg.connect() has no jsonb type codec
        # registered (same as the rest of the app — no connection anywhere does),
        # so jsonb columns come back as raw JSON text, not list/dict. A codec
        # registered on this connection would also mis-encode the json.dumps(...)
        # params this same function binds to ::jsonb columns below (INSERT into
        # quality_scores) — encoding an already-dumped string a second time. Decode
        # each jsonb field explicitly instead, same idiom as admin_settings.py's
        # local _jsonb() helper.
        def _jsonb(val):
            if val is None:
                return None
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except (TypeError, ValueError):
                    return val
            return val

        tenant_uuid = str(gc["tenant_id"])
        _meta = _jsonb(gc["metadata"])
        _brand_name = (_meta or {}).get("brand_name")
        brand_row = await _resolve_brand_rule(conn, tenant_uuid, None, _brand_name)
        _br = dict(brand_row) if brand_row else {}

        seo_row = await conn.fetchrow("""
            SELECT keyword_search, provider, keyword_ideas, top_keywords
            FROM silver_aa_internal.seo_context
            WHERE tour_id = $1::uuid
            ORDER BY fetched_at DESC LIMIT 1
        """, gc["tour_id"])
        seo = dict(seo_row) if seo_row else {}
        if seo:
            seo["keyword_ideas"] = _jsonb(seo.get("keyword_ideas")) or []
            seo["top_keywords"] = _jsonb(seo.get("top_keywords")) or []

        generated = {
            "name":         gc["aa_name"],
            "subtitle":     gc["aa_subtitle"],
            "summary":      gc["aa_summary"],
            "description":  gc["aa_description"],
            "highlights":   _jsonb(gc["aa_highlights"]) or [],
            "itineraries":  gc["aa_itineraries"],
            "mobile_card_text": gc["mobile_card_text"],
            "seo_title":    gc["seo_title"],
            "seo_meta":     gc["seo_meta"],
            "seo_keywords_used": _jsonb(gc["seo_keywords_used"]) or [],
            "og_tags":      _jsonb(gc["og_tags"]) or {},
        }
        # AA-741 (S229): the revalidation graph judges the edit AGAINST THE SOURCE (judge, brand
        # audit, A1-3 meal/time grounding). It used to get name/country/duration only, so every
        # source-grounded check compared to an empty source: Jev answered "not in the source"
        # (p~0.03) for meals the source states verbatim and the version stayed flagged. Same
        # tour shape as the write path (`_execute_run_tour`).
        tour = {
            "name":        gc["src_name"],
            "subtitle":    gc["src_subtitle"],
            "summary":     gc["src_summary"],
            "description": gc["src_description"],
            "highlights":  _jsonb(gc["src_highlights"]) or [],
            "itineraries": gc["src_itineraries"],
            "country":     gc["country"],
            "duration":    gc["duration"],
            "price":       gc["price_raw"],
            "inclusions":  gc["inclusions"],
            "exclusions":  gc["exclusions"],
        }

        initial_state = {
            "tour": tour, "seo": seo, "generated": generated,
            "model_tier": "haiku", "is_tenant_rewrite": False, "few_shots": [],
            "quality_score": 0.0, "retry_count": 0, "feedback": "", "error": "",
            "cost_usd": 0.0, "model_used": "",
            "brand_system_prompt":  _br.get("system_prompt", ""),
            "brand_style_guide":    _br.get("style_guide", ""),
            "brand_forbidden_words": _br.get("forbidden_words", []),
            "rewrite_language":     "en-US",
            "brand_core_idea":        _br.get("core_idea", ""),
            "brand_customer_segment": _br.get("customer_segment", ""),
            "brand_customer_mindset": _br.get("customer_mindset", ""),
            "brand_voice_examples":   _br.get("voice_examples", []),
            "brand_good_examples":    _br.get("good_examples", ""),
            "subtitle_focus": "standard", "seo_mode": "dataforseo",
            "brand_audit_status": "", "brand_audit_codes": [], "brand_audit_issues": [],
            "brand_audit_fields": [], "lessons_extracted": [],
            "fix_pass_applied": False, "fix_pass_fields": [],
            # AA-645 d02: a v2 draft keeps brand fit report-only and its two v2 validate codes report-only on a
            # reviewer edit too (validate / judge read is_v2 from this key). None for v1 and T2 versions.
            "writer_version": (_meta or {}).get("writer_version") if isinstance(_meta, dict) else None,
        }
        if initial_state["writer_version"]:
            # AA-645 (S249): the agency fields the v2 fact sheet reads — the brand-fit rubric v2 judges within the same
            # source the writer saw (a stated group size is a source fact, not an unsupported specific).
            initial_state["tour_agency"] = {"group_size": gc.get("group_size"),
                                            "best_time_to_go": gc.get("best_time_to_go")}

        graph = build_revalidation_graph()

        def _run():
            import asyncio as _a
            loop = _a.new_event_loop()
            _a.set_event_loop(loop)
            try:
                # AA-1058: the re-validation's judge / brand audit / Jev rows name the tour.
                with _call_log.bind_attribution(tour_id=gc["tour_id"]):
                    return graph.invoke(initial_state)
            finally:
                loop.close()

        # AA-1058: run_in_executor does not copy contextvars (asyncio.to_thread does): without the copy the rows lost
        # the bound job id (revalidate job) and tenant.
        import contextvars as _cv
        result = await asyncio.get_event_loop().run_in_executor(None, _cv.copy_context().run, _run)

        passed = bool(result.get("revalidate_passed", False))
        q_score = float(result.get("quality_score", 0.0))
        _sub = result.get("sub_scores", {})
        _fc = result.get("failure_codes", [])

        # AA-983: overwrite the version's score in ONE transaction, in this order:
        #   1. INSERT the new score row (RETURNING its id),
        #   2. re-point every published tour of this version at it,
        #   3. DELETE the version's other (old) score rows.
        # It used to DELETE first, then INSERT, as three autocommit statements: the DELETE fired the
        # FK published_tours_quality_score_id_fkey (CIS 268, ON DELETE SET NULL) and the published
        # tour was left with quality_score_id = NULL (before 268: a dangling id). With the pointer
        # moved before the DELETE, the SET NULL never fires; the lock order (published_tours row,
        # then the score row) is also the export upsert's, so the two cannot deadlock. The published
        # `quality_score` number stays as exported (same rule as 268); only the pointer moves.
        async with conn.transaction():
            new_score_id = await conn.fetchval("""
                INSERT INTO silver_aa_internal.quality_scores (
                    generated_content_id, tour_id, tenant_id,
                    score_overall, score_brand, score_seo, score_structure, score_quality,
                    passed_count, failed_count, failure_codes,
                    brand_audit_status, brand_audit_codes, brand_audit_issues,
                    brand_audit_fields, lessons_extracted, validator_fn_version, evaluated_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11::jsonb,
                    $12, $13::jsonb, $14::jsonb, $15::jsonb, $16::jsonb, 'v2-revalidate', NOW()
                )
                RETURNING id
            """,
                content_id, str(gc["tour_id"]), tenant_uuid, q_score,
                float(_sub.get("brand", 0.0)), float(_sub.get("seo", 0.0)),
                float(_sub.get("structure", 0.0)), float(_sub.get("quality", 0.0)),
                int(result.get("passed_count", 0)), int(result.get("failed_count", 0)),
                json.dumps(_fc), result.get("brand_audit_status", ""),
                json.dumps(result.get("brand_audit_codes", [])),
                json.dumps(result.get("brand_audit_issues", [])),
                json.dumps(result.get("brand_audit_fields", [])),
                json.dumps(result.get("lessons_extracted", [])),
            )
            repointed = await conn.execute("""
                UPDATE gold_aa_internal.published_tours
                SET quality_score_id = $1::uuid
                WHERE generated_content_id = $2::uuid
                  AND quality_score_id IS DISTINCT FROM $1::uuid
            """, str(new_score_id), content_id)
            await conn.execute("""
                DELETE FROM silver_aa_internal.quality_scores
                WHERE generated_content_id = $1::uuid AND id <> $2::uuid
            """, content_id, str(new_score_id))
            await conn.execute(
                "UPDATE silver_aa_internal.generated_content SET revalidate_passed = $1 WHERE id = $2::uuid",
                passed, content_id)
        # AA-1047 W1: the re-validation graph's REPORT-mode final contract (its last node), with the gate's outcome
        # (passed = approvable) and the stored text's sha; best-effort, never fails the re-validation.
        _reval_fc = result.get("final_contract")
        if isinstance(_reval_fc, dict):
            _reval_fc = _fcon.stamp_stored(_reval_fc, _fcon.tour_text_sha(initial_state.get("generated") or {}),
                                         actual="approved" if passed else "hitl")
            await _fc_store.attach_s1(conn, content_id, _reval_fc)

        published_repointed = _rowcount(repointed)
        logger.info("revalidate_tour_done", content_id=content_id,
                    revalidate_passed=passed, quality_score=q_score,
                    quality_score_id=str(new_score_id), published_repointed=published_repointed,
                    brand_audit_status=result.get("brand_audit_status", ""))
        return {
            "content_id": content_id, "revalidate_passed": passed,
            "quality_score": q_score, "brand_audit_status": result.get("brand_audit_status", ""),
            "failure_codes": _fc,
            "quality_score_id": str(new_score_id), "published_repointed": published_repointed,
        }
    finally:
        await conn.close()
