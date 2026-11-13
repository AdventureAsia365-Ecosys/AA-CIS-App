# api/routers/admin_pipeline/_engine.py — the S1 rewrite engine of one tour (job, sync endpoint, batch ingest).
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import asyncio
import json
import os
import asyncpg
from fastapi import HTTPException
from typing import Awaitable, Callable, Optional
from api.routers.v1_pipeline import _rewrite_tour
from shared.llm_client import call_log as _call_log  # AA-1058: the tour every S1 ledger row names
from shared.llm_client.errors import is_llm_access_denied
from services.content_generation import s1_checkpoint as _ckpt  # AA-993 G2
from shared.secrets import get_database_url  # AA-838 D9: default application_name
from shared.tour_liveness import SourceNotLiveError
from services.quality import final_contract as _fcon  # AA-1047 W1 (REPORT-mode exit contract)
from services.quality import lineage as _lineage  # AA-1052 W6 (writer-time lineage stamp)

from api.routers.admin_pipeline._shared import (
    TourRunRequest, _CONTENT_INSERT_ERRORS, _MASTER_TENANT_ID, _REWRITE_CLASS_HARD_CODES, _build_generated_metadata,
    _enqueue_review, _final_contract_at_store, _is_publishable, _is_uuid, _pipeline_semaphore, _resolve_brand_rule,
    _s1_prompt_version, _stamp_fact_sheet, _surviving_hard_codes, _tour_passed, _version_of_job, logger,
)


# AA-1058: every ledger row of the rewrite (writer, judges, flag fix, brand audit, grounding, Jev a1_*) names the tour.
@_call_log.attributed(tour_id=lambda *a, **k: (k["req"] if "req" in k else a[0]).tour_id)
async def _execute_run_tour(
    req: TourRunRequest,
    job_id: str | None = None,
    *,
    batch_text: str | None = None,       # AA-606: Bedrock Batch attempt-1 writer text (skip live writer)
    batch_model_used: str | None = None,
    batch_account: str | None = None,
    stage_cb: "Callable[[str], Awaitable[None]] | None" = None,  # AA-723
    checkpoint: "_ckpt.S1Checkpoint | None" = None,  # AA-993 G2: resumable persist stages (s1_rewrite job only)
) -> dict:
    """Core tour rewrite — called by HTTP endpoint and background retry task.

    AA-993 G2: `checkpoint` (the s1_rewrite job passes it) records each finished step in the job's progress and,
    when a released job is re-claimed, resumes from the last one: no new writer call after the writer finished, no
    rewrite at all after the rewrite finished, one generated_content row (found again by its metadata.s1_job_id
    marker), one quality_scores row, one review entry. Without it (sync /admin/run-tour, Bedrock Batch ingest) the
    behaviour is unchanged. Stage list: services/content_generation/s1_checkpoint.py.

    AA-606: when batch_text is set, the writer's attempt-1 came from a Bedrock Batch job, so
    _rewrite_tour seeds it (build_graph_from_generated) instead of calling the writer live. All
    brand-resolve / SEO / persist / export / review-queue logic below is reused unchanged.

    AA-250 B2 / AA-723: `on_stage` persists each completed LangGraph node name so the S1 job poll
    can drive a stage-progress bar. The s1_rewrite worker kind passes `stage_cb`, which writes it
    into shared.job.progress; the sync /admin/run-tour path passes neither (no stage tracking).

    AA-735 (ADR 0003 nac 5): the old `job_id`-only branch (jobs_repo.update_stage -> pipeline_jobs)
    is gone — pipeline_jobs is read-only history now. `job_id` is still accepted (the worker kind
    passes it alongside stage_cb) but no longer drives any pipeline_jobs write here.
    """
    on_stage = stage_cb

    conn = await asyncpg.connect(get_database_url())
    TENANT_SLUG_MAP = {"aa_internal": "00000000-0000-0000-0000-000000000001"}
    tenant_uuid = TENANT_SLUG_MAP.get(req.tenant_id, req.tenant_id)
    try:
        row = await conn.fetchrow(
            "SELECT * FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid",
            req.tour_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"Tour {req.tour_id} not found")

        if row.get("source_status") and str(row["source_status"]) == "trashed":
            raise HTTPException(
                status_code=400,
                detail="Tour is trashed. Restore before rewriting.",
            )
        if row.get("source_status") and str(row["source_status"]) == "superseded":
            # AA-702: a superseded source is an older/duplicate version of another tour.
            raise HTTPException(
                status_code=400,
                detail=("Tour is superseded by another version. Rewrite the active version "
                        "or promote this one in Dup Review."),
            )

        # AA-314: no jsonb codec is registered on this asyncpg connection (or anywhere in
        # this app) — src_highlights arrives as a JSON-encoded str, not a list. Feeding it
        # straight into prompts.py's f-string leaked raw JSON syntax (["A", "B"]) into the
        # rewrite prompt for every tour. Parse it here so downstream gets a real list.
        src_highlights = row["src_highlights"]
        if not isinstance(src_highlights, list):
            src_highlights = json.loads(src_highlights) if src_highlights else []

        tour = {
            "name":        row["src_name"],
            "subtitle":    row["src_subtitle"],
            "summary":     row["src_summary"],
            "description": row["src_description"],
            "highlights":  src_highlights,
            "itineraries": row["src_itineraries"],
            "country":     row["country"],
            "duration":    row["duration"],
            "price":       row["price_raw"],
            "inclusions":  row["inclusions"],
            "exclusions":  row["exclusions"],
        }

        brand_rules: dict = {}
        brand_rule_id: Optional[str] = None
        brand_name_val: str = ""
        brand_rule_version: Optional[int] = None
        try:
            br_row = await _resolve_brand_rule(
                conn,
                tenant_uuid,
                getattr(req, "brand_identity_id", None),
                getattr(req, "brand_name", None),
            )
            if br_row:
                brand_rule_id = str(br_row["id"]) if br_row["id"] else None
                brand_name_val = br_row["brand_name"] or ""
                brand_rule_version = int(br_row["version"]) if br_row["version"] is not None else None
                _voice = br_row["voice_examples"]
                brand_rules = {
                    "system_prompt":    br_row["system_prompt"] or "",
                    "style_guide":      br_row["style_guide"] or "",
                    "forbidden_words":  (
                        list(br_row["forbidden_words"]) if isinstance(br_row["forbidden_words"], list)
                        else __import__("json").loads(br_row["forbidden_words"] or "[]")
                    ),
                    "rewrite_language": getattr(req, "rewrite_language", "en-US"),
                    # AA-202: brand differentiation fields (resolver now selects these)
                    "core_idea":        br_row.get("core_idea") or "",
                    "customer_segment": br_row.get("customer_segment") or "",
                    "customer_mindset": br_row.get("customer_mindset") or "",
                    "voice_examples":   (
                        list(_voice) if isinstance(_voice, list)
                        else __import__("json").loads(_voice or "[]")
                    ),
                    "good_examples":    br_row.get("good_examples") or "",
                    "brand_type":       br_row.get("brand_type") or "",
                }
        except Exception as _br_err:
            logger.warning("brand_rules_fetch_failed", error=str(_br_err))

        # AA-923 (WM-34): the AA-132 lessons append is retired (frozen since AA-915, it added nothing);
        # learned rules reach the writer only through a writing-memory release (app ADR 0010).

        seo_data: dict = {}
        dataforseo_used: bool = False
        _SEO_MODE_MAP = {"standard": "dataforseo", "aggressive": "dataforseo", "minimal": "disabled"}
        effective_seo_mode = _SEO_MODE_MAP.get(req.seo_mode, req.seo_mode)
        # AA-993 G2: a run resumed past the rewrite needs no SEO data (the saved result already used it).
        _resume_rewritten = _ckpt.done(checkpoint, "rewritten")
        if _resume_rewritten:
            dataforseo_used = checkpoint.dataforseo_used
        else:
            try:
                from services.seo_intelligence.handler import S1_REWRITE_BUDGET_KIND, process_seo
                from services.seo_intelligence.seed_builder import (
                    activity_terms, build_seed, idea_seeds, title_place_terms,
                )
                # AA-197: build a complete seed (no double-"tours"); fall back to src_name.
                # AA-251 (ADR-2026-021): pass src_name through so the seed falls back to
                # tour-specific text instead of the generic "{country} tours" phrase.
                seed = build_seed(
                    row.get("country"), row.get("activities"), row.get("src_name"),
                ) or row.get("src_name", "")
                if seed:
                    # AA-249: no cache= passed — process_seo() defaults to its own
                    # module-level Redis singleton (services/seo_intelligence/handler.py),
                    # shared across same-country tours. Kept out of this call because this
                    # function also runs from the background 3-worker job queue (AA-223)
                    # with no Request in scope, so request.app.state.redis isn't reachable
                    # here anyway.
                    # AA-702: extra seeds + the title's place words go into the same paid
                    # keyword-ideas task so S1 gets keywords with real search volume.
                    seo_result = await process_seo(
                        tour_id=req.tour_id, destination=seed, seed=seed,
                        tenant_id=tenant_uuid, seo_mode=effective_seo_mode,
                        extra_seeds=idea_seeds(row.get("country"), row.get("activities"), row.get("src_name"))[1:],
                        place_terms=title_place_terms(row.get("src_name"), row.get("country")),
                        activity_words=activity_terms(row.get("activities")),
                        country=row.get("country") or "",
                        # AA-642 (S0): the per-tour DataForSEO cap (dfs / job:s1_rewrite); a stop leaves S1
                        # without fresh SEO data, as a DataForSEO failure always did.
                        budget_kind=S1_REWRITE_BUDGET_KIND,
                    )
                    seo_data = seo_result.get("data", {})
                    dataforseo_used = seo_result.get("status") == "fetched"
                    # B2: Normalize seo schema — ensure both flat and nested keys exist
                    if "keywords" in seo_data and "top_keywords" not in seo_data:
                        seo_data["top_keywords"] = seo_data["keywords"].get("top_keywords", [])
                    elif "top_keywords" in seo_data and "keywords" not in seo_data:
                        seo_data["keywords"] = {"top_keywords": seo_data["top_keywords"]}
                    seo_data.setdefault("top_keywords", [])
                    seo_data.setdefault("people_also_ask", [])
            except Exception as _seo_err:
                logger.warning("seo_step_failed", tour_id=req.tour_id, error=str(_seo_err))

        if _resume_rewritten:
            # AA-993 G2: the rewrite (writer, judge, fix pass, any sonnet upgrade) finished before the release.
            result = dict(checkpoint.result)
            _auto_upgraded = checkpoint.auto_upgraded
            logger.info("s1_rewrite_resumed", tour_id=req.tour_id, job_id=checkpoint.job_id,
                        resumed_from=checkpoint.resumed_from)
        else:
            # AA-645 step 0: the agency fields only the S1 writer v2's fact sheet reads (A1, flag S1_WRITER_V2).
            tour_agency = {"group_size": row.get("group_size"), "best_time_to_go": row.get("best_time_to_go")}
            # AA-919 (WM-18 / WM-20): the A1 writing-memory release (platform only; None = memory off, fails open). Only
            # the v2 writer consumes it and stamps the version (metadata.writing_release); release 0 changes no byte.
            from services.writing_memory.insertion import resolve as _wm_resolve
            writing_memory = await _wm_resolve(conn, "a1")
            # AA-993 G2: the primary rewrite saves each writer's state and, on a resume, re-enters after the writer.
            # Only passed with a checkpoint, so the other callers' _rewrite_tour call is unchanged.
            _resume_kw = ({"resume_state": checkpoint.resume_writer_state,
                           "on_writer_state": checkpoint.save_writer_state} if checkpoint is not None else {})
            result = await _rewrite_tour(
                tour, idx=0, total=1,
                brand_rules=brand_rules,
                seo=seo_data,
                subtitle_focus=req.subtitle_focus,
                seo_mode=effective_seo_mode,
                on_stage=on_stage,
                tour_agency=tour_agency,
                writing_memory=writing_memory,       # AA-919
                batch_text=batch_text,               # AA-606
                batch_model_used=batch_model_used,   # AA-606
                batch_account=batch_account,         # AA-606
                **_resume_kw,                        # AA-993 G2
            )

            _UPGRADE_THRESHOLD = float(os.environ.get("AUTO_UPGRADE_THRESHOLD", "8.5"))
            _score = result.get("quality_score", 0.0)
            _model = result.get("model_used", "")
            _auto_upgraded = False  # AA-237/AA-736: did a sonnet re-run replace the haiku result?

            # AA-736: a rewrite-class hard code (FORBIDDEN_WORD / BRAND_SEO_META_VIOLATION /
            # MISSING_FIELD) that survived haiku's flag_fix forces a one-shot sonnet rewrite, since
            # these block Master now (_is_publishable). This is independent of AA-237's opt-in and of
            # the 8.5 score threshold — a tour can score >=7 and still carry a forbidden word.
            # AA-237 keeps its original opt-in "low score, lift it" behaviour as the other trigger.
            _surviving_rewrite = _surviving_hard_codes(result) & _REWRITE_CLASS_HARD_CODES
            _on_haiku = result.get("status") == "success" and "haiku" in _model.lower()
            _trigger_hard = bool(_surviving_rewrite) and _on_haiku
            _trigger_optin = (
                req.allow_auto_upgrade and result.get("status") == "success"
                and 0 < _score < _UPGRADE_THRESHOLD and "haiku" in _model.lower()
            )
            if _trigger_hard or _trigger_optin:
                logger.info("sonnet_upgrade_triggered", tour_id=req.tour_id,
                            reason="hard_codes" if _trigger_hard else "low_score_optin",
                            surviving_hard=sorted(_surviving_rewrite), score=_score)
                _upgraded = await _sonnet_upgrade(
                    tour, tour_id=req.tour_id,
                    brand_rules=brand_rules,
                    seo=seo_data,
                    subtitle_focus=req.subtitle_focus,
                    seo_mode=effective_seo_mode,
                    on_stage=on_stage,
                    tour_agency=tour_agency,
                    writing_memory=writing_memory,   # AA-919
                )
                # Keep sonnet when it is strictly better: for a hard-code trigger that means fewer
                # surviving hard codes (clearing the blocker is the goal, not a higher score); for the
                # AA-237 opt-in trigger it keeps the original "higher score wins" rule.
                if _upgraded is None:
                    _keep = False
                elif _trigger_hard:
                    _keep = len(_surviving_hard_codes(_upgraded)) < len(_surviving_hard_codes(result))
                else:
                    _keep = _upgraded.get("quality_score", 0.0) > _score
                if _keep:
                    result = _upgraded
                    _auto_upgraded = True
                # AA-736: mark that a sonnet retry happened so the review-queue summary can say so,
                # whichever run we kept. Only meaningful for the hard-code trigger (a refused upgrade never ran).
                if _trigger_hard and _upgraded is not None:
                    result["_sonnet_retried"] = True
                logger.info("sonnet_upgrade_result", tour_id=req.tour_id, kept=_keep,
                            surviving_hard_after=sorted(_surviving_hard_codes(result)))

            # AA-738: the review-queue reason scans the brand's own forbidden words too.
            result["_brand_forbidden_words"] = brand_rules.get("forbidden_words") or []
            if checkpoint is not None and result.get("generated"):
                await checkpoint.save_rewrite(result, auto_upgraded=_auto_upgraded, dataforseo_used=dataforseo_used)

        version_id = None
        _m_final = None
        if result.get("generated") and len(result.get("generated", {})) > 0:
            generated = result["generated"]
            # AA-211: gc.status MUST use the same audit-aware gate as the export decision below
            # (line ~533). Otherwise a flagged-unfixed / manual_check tour scoring >=7 is written
            # status='approved' while being routed to review_queue — and since process_export is
            # gated on gc.status='approved', that bypasses the HITL gate via any other export path.
            status = "approved" if _is_publishable(result) else "hitl"
            is_branded = result.get("is_branded", True)
            og_tags_val = json.dumps({} if is_branded else {"unbranded": True})
            _metadata = _build_generated_metadata(
                result,
                brand_rule_id=brand_rule_id,
                brand_name=brand_name_val,
                seo_mode=req.seo_mode,
                model_used=result.get("model_used", ""),
                llm_cost_usd=float(result.get("cost_usd") or 0.0),
                dataforseo_used=dataforseo_used,
                batch_id=getattr(req, "batch_id", None),
            )
            if checkpoint is not None:
                # AA-993 G2: ties the version to its job, so a resume finds it even when the release fell between the
                # INSERT and its checkpoint write.
                _metadata[_ckpt.JOB_MARKER_KEY] = checkpoint.job_id
            # AA-1047 W1 (root ADR 0020 invariant 4): the graph's REPORT-mode final contract, with the status the
            # gate above gave and the sha of the text stored below (stored == gated, else POST_GATE_EDIT).
            _fc_record = _final_contract_at_store(result, generated, status)
            if _fc_record is not None:
                _metadata["final_contract"] = _fc_record
                _metadata["final_contract_log"] = [_fcon.compact(_fc_record)]
            # AA-1052 W6: the writer-time lineage (policy_sha, route version), next to the contract; a later exit
            # (re-validation, human edit, approve, publish) replaces only metadata.final_contract, never this key.
            _metadata[_lineage.KEY] = _lineage.writer_lineage(_fc_record)
            metadata_val = json.dumps(_metadata, default=str)
            _itin = generated.get("itineraries", "")
            if not isinstance(_itin, str):
                _itin = str(_itin)
            # AA-740: store exactly what validate saw. The old AA-204 write-time trim made the stored
            # meta (e.g. 153) differ from the validated one (> 155), so a review row showed an
            # in-band meta under SEO_META_TOO_LONG. The length fit now runs in the graph
            # (fit_seo_meta_final) and approved rows are in band by the gate; columns are unbounded.
            _m_final = generated.get("seo_meta") or ""
            if _resume_rewritten:
                # AA-993 G2: the same result as the released run, so its version (if the INSERT committed) is this one
                version_id = checkpoint.version_id or await _version_of_job(conn, req.tour_id, checkpoint.job_id)
                if version_id:
                    logger.info("s1_version_reused", tour_id=req.tour_id, version_id=str(version_id),
                                job_id=checkpoint.job_id)
            try:
                version_id = version_id or await conn.fetchval("""
                    INSERT INTO silver_aa_internal.generated_content (
                        tour_id, tenant_id, version_num,
                        aa_name, aa_subtitle, aa_summary,
                        aa_description, aa_highlights, aa_itineraries,
                        seo_title, seo_meta, seo_keywords_used,
                        model_editorial, status, og_tags, metadata,
                        brand_rules_version, requested_tier, fallback_used, satellite_used,
                        satellite_account, revalidate_passed, prompt_version
                    ) VALUES (
                        $1::uuid, $2::uuid,
                        COALESCE((SELECT MAX(version_num) + 1
                        FROM silver_aa_internal.generated_content
                        WHERE tour_id = $1::uuid), 1),
                        $3, $4, $5, $6, $7::jsonb, $8,
                        $9, $10, $11::jsonb, $12, $13::content_status_enum, $14::jsonb, $15::jsonb,
                        $16, $17, $18, $19, $20, $21, $22
                    ) RETURNING id
                """,
                    req.tour_id, tenant_uuid,
                    (generated.get("name") or "")[:500],
                    (generated.get("subtitle") or "")[:500],
                    generated.get("summary"),
                    generated.get("description", ""),
                    json.dumps(generated.get("highlights", [])),
                    _itin,
                    generated.get("seo_title") or "",  # AA-740: no write-time trim (see seo_meta)
                    _m_final,
                    json.dumps(generated.get("seo_keywords_used", [])),
                    result.get("model_used", ""),
                    status,
                    og_tags_val,
                    metadata_val,
                    brand_rule_version,
                    None,                                    # AA-224 requested_tier; AA-1024 A2: never per request
                    bool(result.get("fallback_used", False)),  # AA-224: fallback_used
                    # AA-397: satellite_used (bool) giữ nguyên cho backward-compat, derive từ
                    # satellite_account is not None thay vì đọc field bool cũ (đã xoá khỏi LLMResponse).
                    result.get("satellite_account") is not None,
                    result.get("satellite_account"),         # AA-397: satellite_account (text, 'acc3'/None)
                    # S218 audit: the column existed but was never written (0/681 rows); metadata had it.
                    result.get("revalidate_passed") if result.get("revalidate_ran") else None,
                    # AA-1058: the column was never written (only metadata.prompt_version): 1,219 versions in 30 days
                    # had none. The writer stage's registered version, else the writer's own prompt hash.
                    _s1_prompt_version(result),
                )
                if version_id:
                    logger.info("version_inserted", tour_id=req.tour_id, version_id=str(version_id))
                else:
                    logger.error("version_insert_returned_null", tour_id=req.tour_id)
            except Exception as _ins_err:
                logger.error("version_insert_failed", tour_id=req.tour_id, error=str(_ins_err))
                if (checkpoint is not None and isinstance(_ins_err, _CONTENT_INSERT_ERRORS)
                        and checkpoint.reached("rewritten")):
                    # AA-993 G2 review: the saved content itself does not fit (e.g. a title longer than
                    # seo_title varchar(70)); replaying it would fail every attempt, so the next one rewrites.
                    await checkpoint.forget_rewrite(f"{type(_ins_err).__name__}: {_ins_err}")
                raise
            if version_id:
                await _ckpt.mark(checkpoint, "content_inserted", version_id=str(version_id))
                # AA-1047 (Quality W1): the FactSheet of the raw source the writer read (tour builder;
                # acp_shared.fact_sheet, CIS 386), stamped on the version. Best-effort: a failure changes nothing.
                await _stamp_fact_sheet(conn, row, tenant_uuid, version_id)

        # AA-993 G2: one score row per version — quality_scores has no unique key on generated_content_id (the old
        # ON CONFLICT DO NOTHING never fired), so the insert skips a version that already has its row.
        if version_id and result.get("quality_score") is not None and not _ckpt.done(checkpoint, "scored"):
            _sub = result.get("sub_scores", {})
            _fc  = result.get("failure_codes", [])
            await conn.execute("""
                INSERT INTO silver_aa_internal.quality_scores (
                    generated_content_id, tour_id, tenant_id,
                    score_overall, score_brand, score_seo,
                    score_structure, score_quality,
                    passed_count, failed_count, failure_codes,
                    validator_fn_version, evaluated_at
                )
                SELECT $1::uuid, $2::uuid, $3::uuid,
                       $4, $5, $6, $7, $8, $9, $10, $11::jsonb, 'v2', NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM silver_aa_internal.quality_scores WHERE generated_content_id = $1::uuid
                )
            """,
                version_id, req.tour_id,
                "00000000-0000-0000-0000-000000000001",
                float(result.get("quality_score", 0.0)),
                float(_sub.get("brand", 0.0)),
                float(_sub.get("seo", 0.0)),
                float(_sub.get("structure", 0.0)),
                float(_sub.get("quality", 0.0)),
                int(result.get("passed_count", 0)),
                int(result.get("failed_count", 0)),
                json.dumps(_fc),
            )

        # BƯỚC 7: Persist brand_audit results to quality_scores
        if version_id and result.get("brand_audit_status") and not _ckpt.done(checkpoint, "scored"):
            try:
                await conn.execute("""
                    UPDATE silver_aa_internal.quality_scores
                    SET brand_audit_status = $1,
                        brand_audit_codes  = $2::jsonb,
                        brand_audit_issues = $3::jsonb,
                        brand_audit_fields = $4::jsonb,
                        lessons_extracted  = $5::jsonb
                    WHERE generated_content_id = $6::uuid
                """,
                    result["brand_audit_status"],
                    json.dumps(result.get("brand_audit_codes", [])),
                    json.dumps(result.get("brand_audit_issues", [])),
                    json.dumps(result.get("brand_audit_fields", [])),
                    json.dumps(result.get("lessons_extracted", [])),
                    version_id,
                )
            except Exception as _audit_err:
                logger.warning("brand_audit_persist_failed", error=str(_audit_err))
        if version_id:
            await _ckpt.mark(checkpoint, "scored")

        # BƯỚC 7: Persist fix_pass results to generated_content
        if version_id and result.get("fix_pass_applied") and not _ckpt.done(checkpoint, "fix_pass_saved"):
            try:
                await conn.execute("""
                    UPDATE silver_aa_internal.generated_content
                    SET fix_pass_applied = true,
                        fix_pass_fields  = $1::jsonb
                    WHERE id = $2::uuid
                """,
                    json.dumps(result.get("fix_pass_fields", [])),
                    version_id,
                )
            except Exception as _fix_err:
                logger.warning("fix_pass_persist_failed", error=str(_fix_err))
        if version_id:
            await _ckpt.mark(checkpoint, "fix_pass_saved")

        # AA-211: audit-aware publish gate (see _is_publishable).
        status = "approved" if _is_publishable(result) else "pending"
        _route = bool(version_id) and not _ckpt.done(checkpoint, "routed")   # AA-993 G2: not routed before a release
        if _route and status == "approved":
            from services.export.handler import process_export
            from services.tour_lifecycle.policy import PolicyReviewRequiredError
            try:
                await process_export(str(version_id), contract_exit="auto_publish")
            except PolicyReviewRequiredError as _held:
                # AA-1049 guard: the version offers elephant riding (text or source) and no person approved it. An
                # expected refusal: process_export moved it approved → hitl and filed its pending policy review.
                logger.warning("s1_export_refused_policy_review", tour_id=req.tour_id, version_id=str(version_id),
                               review_id=_held.review_id)
            except SourceNotLiveError as _dead:
                # AA-986: the source was trashed / superseded while S1 ran. An expected refusal, not an error
                # (no Sentry event): process_export already moved the version approved → hitl; file the
                # attempt as a review row, which _enqueue_review enters 'dismissed' for a dead source.
                logger.warning("s1_export_refused_source_not_live", tour_id=req.tour_id,
                               version_id=str(version_id), reason=_dead.state.reason)
                try:
                    await _enqueue_review(conn, req.tour_id, version_id, result, failure_summary=str(_dead))
                except Exception as _rq_err:
                    logger.warning("review_queue_enqueue_failed", tour_id=req.tour_id, error=str(_rq_err))
            except Exception as _exp_err:
                logger.error("export_failed", tour_id=req.tour_id, error=str(_exp_err))
        elif _route and status == "pending":
            # AA-212: route blocked / low-quality tour to HITL review_queue (idempotent enqueue).
            try:
                await _enqueue_review(conn, req.tour_id, version_id, result)
            except Exception as _rq_err:
                logger.warning("review_queue_enqueue_failed", tour_id=req.tour_id, error=str(_rq_err))
        if _route:
            await _ckpt.mark(checkpoint, "routed")

        cost_usd    = float(result.get("cost_usd") or 0.0)
        tokens_in   = int(result.get("input_tokens") or 0)
        tokens_out  = int(result.get("output_tokens") or 0)
        tour_passed = _tour_passed(result)
        model_name  = result.get("model_used") or None
        if isinstance(model_name, str) and not model_name:
            model_name = None
        actual_provider = "openai" if model_name and "gpt" in model_name.lower() else "bedrock"
        _account = not _ckpt.done(checkpoint, "accounted")   # AA-993 G2: not added before a release
        if _account and (cost_usd > 0 or tokens_in > 0) and _is_uuid(req.batch_id):
            await conn.execute("""
                UPDATE shared.pipeline_runs
                SET cost_usd      = COALESCE(cost_usd, 0)      + $1,
                    tokens_input  = COALESCE(tokens_input, 0)  + $2,
                    tokens_output = COALESCE(tokens_output, 0) + $3,
                    tours_failed  = tours_failed + $4,
                    llm_model     = COALESCE($6, llm_model),
                    llm_provider  = $7,
                    step_name     = 'content_generation'
                WHERE batch_id = $5::uuid
            """,
                cost_usd, tokens_in, tokens_out,
                0 if tour_passed else 1,
                req.batch_id, model_name, actual_provider,
            )
        elif _account and (cost_usd > 0 or tokens_in > 0) and not _is_uuid(req.batch_id):
            # AA-210: non-UUID batch_id (e.g. ad-hoc verification run) — skip accounting
            # UPDATE so the uuid cast cannot crash the post-export path. No matching
            # pipeline_runs row exists for such runs anyway.
            logger.info("pipeline_runs_accounting_skipped",
                        batch_id=req.batch_id, reason="batch_id is not a valid uuid")
        if version_id and _account:
            await _ckpt.mark(checkpoint, "accounted")

        return {
            "tour_id":            req.tour_id,
            "batch_id":           req.batch_id,
            "version_id":         str(version_id) if version_id else None,
            "status":             result.get("status"),
            "quality_score":      result.get("quality_score"),
            "generated":          result.get("generated"),
            "failure_codes":      result.get("failure_codes", []),   # AA-204: surface validate codes
            # AA-990 N3: why a run did not publish, as the gate read it (hard_block_codes needs writer_version,
            # which this dict does not carry, so it is computed here on the graph result).
            "hard_block_codes":   sorted(_surviving_hard_codes(result)),
            "publishable":        _is_publishable(result),
            "seo_meta_stored":    _m_final,                           # AA-204: actual value written to DB
            "cost_usd":           result.get("cost_usd"),       # AA-237: reflects upgraded run if any
            "model_used":         result.get("model_used"),     # AA-237: reflects upgraded run if any
            "auto_upgraded":      _auto_upgraded,                # AA-237: True iff sonnet re-run kept
            "brand_audit_status": result.get("brand_audit_status"),
            "brand_audit_codes":  result.get("brand_audit_codes", []),
            "brand_audit_issues": result.get("brand_audit_issues", []),
            "fix_pass_applied":   result.get("fix_pass_applied", False),
            "fix_pass_fields":    result.get("fix_pass_fields", []),
            "fallback_used":    result.get("fallback_used", False),  # AA-233
        }
    finally:
        await conn.close()


async def _sonnet_upgrade(tour: dict, *, tour_id: str, **kwargs) -> Optional[dict]:
    """The AA-237 / AA-736 one-shot sonnet re-run of a tour, or None when it was refused at account level (AA-900).

    The primary result is already paid for, so a refused upgrade (the account blocked mid-run, or the sonnet model not
    enabled) keeps it; the next LLM call meets the same refusal typed."""
    try:
        return await _rewrite_tour(tour, idx=0, total=1, model_tier="sonnet", **kwargs)
    except Exception as exc:
        if not is_llm_access_denied(exc):
            raise
        logger.warning("sonnet_upgrade_llm_access_denied", tour_id=tour_id, error=str(exc)[:300])
        return None


async def _run_tour_safe(tour_req: TourRunRequest, job_id: str | None = None,
                         stage_cb: "Callable[[str], Awaitable[None]] | None" = None,
                         checkpoint: "_ckpt.S1Checkpoint | None" = None) -> dict | None:
    # AA-993 G2: `checkpoint` is shared by the three attempts below, so an attempt after a failed persist step
    # resumes from the last finished step too (no second rewrite, no second version).
    ckpt_kw = {"checkpoint": checkpoint} if checkpoint is not None else {}
    async with _pipeline_semaphore:
        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(2 ** attempt)
            try:
                # AA-223: surface the executor result so _run_tour_job can read
                # version_id. Final-fail path below still swallows and returns None.
                return await _execute_run_tour(tour_req, job_id=job_id, stage_cb=stage_cb, **ckpt_kw)
            except Exception as exc:
                last_exc = exc
                logger.warning("run_tour_attempt_failed", tour_id=tour_req.tour_id,
                               attempt=attempt + 1, error=str(exc))
                if is_llm_access_denied(exc):
                    break  # AA-900: refused at account level; the next attempt would be refused the same way
        logger.error("background_run_tour_failed", tour_id=tour_req.tour_id,
                     batch_id=tour_req.batch_id, error=str(last_exc))
        if not _is_uuid(tour_req.batch_id):
            # AA-210: non-UUID batch_id has no pipeline_runs row to mark failed; skip the
            # uuid-cast UPDATE rather than crash the failure handler.
            logger.info("pipeline_runs_fail_mark_skipped",
                        batch_id=tour_req.batch_id, reason="batch_id is not a valid uuid")
            if is_llm_access_denied(last_exc):
                raise last_exc  # AA-900: see the end of this function
            return
        try:
            conn = await asyncpg.connect(get_database_url())
            await conn.execute(
                "UPDATE shared.pipeline_runs SET status='failed', error_message=$2 "
                "WHERE batch_id=$1::uuid AND status='ingesting'",
                tour_req.batch_id, str(last_exc)[:1000],
            )
            await conn.close()
        except Exception as db_exc:
            logger.error("failed_to_mark_pipeline_failed", error=str(db_exc))
        if is_llm_access_denied(last_exc):
            # AA-900: the one failure the job must see typed (fails at once, reason llm_access_denied) — every other
            # final failure is still swallowed (return None), as before.
            raise last_exc


def _build_s1_progress_sink(redis, job_id: str):
    """AA-723: a WritingProgress sink for the s1_rewrite worker job, reusing the redis client the
    worker already owns (ctx.resources["redis"]) rather than opening a short-lived one like the
    in-process `_build_s1_progress` did. Same key contract (wp:{master}:tour:{job_id}) so
    GET /admin/progress/s1/{job_id} is unchanged. Returns a WritingProgress or None (best-effort —
    a Redis miss must never block the rewrite)."""
    if redis is None:
        return None
    try:
        from services.acp_shared.writing_progress import (
            TOUR_STAGE_STEPS, TOUR_STEPS, WritingProgress,
        )
        return WritingProgress(
            redis, tenant_id=_MASTER_TENANT_ID, kind="tour", job_id=str(job_id),
            steps=TOUR_STEPS, stream_stages={"s1_generate"}, display="tour_json",
            stage_steps=TOUR_STAGE_STEPS,
        )
    except Exception as e:
        logger.warning("s1_progress_sink_build_failed", job_id=job_id, error=str(e))
        return None


def _rowcount(status) -> int:
    """Row count from an asyncpg command status ("UPDATE 3" -> 3); 0 when it is not one."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0
