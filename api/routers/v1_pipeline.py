import re
"""
POST /v1/pipeline/run — Upload Excel → parse → rewrite → return results
Chạy trực tiếp trong ECS, không cần Lambda/Step Functions.
"""
import os
import json
import uuid
import asyncio
import tempfile
import structlog

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Request
from api.core.secret_compare import secrets_equal
from api.core.staff_authz import staff_policy_if_staff
from api.core.staff_credential import principal_verified
from fastapi.responses import JSONResponse
from typing import Optional

from shared.tour_liveness import (
    SOURCE_NOT_LIVE, SourceNotLiveError, live_source_exists_sql, live_source_sql, source_not_live_message,
    source_state,
)
from services.ingestion.excel_parser import ExcelParser
from services.content_generation.graph import build_graph
from pydantic import BaseModel
import asyncpg
import boto3 as _boto3
from fastapi.security import HTTPBearer as _HTTPBearer, HTTPAuthorizationCredentials as _Creds
from api.routers.auth import verify_jwt as _verify_jwt

logger = structlog.get_logger()


# ── Auth (AA-799) — every /v1/pipeline route is admin-only ─────────────────────
# These routes act on Adventure Asia's own master content (Excel rewrite, master review queue,
# approve/reject for export, upload history). Before AA-799, /run had no auth at all and the rest
# used _get_tenant, which accepted ANY valid JWT — a tenant token could approve/reject masters.
# Now the backend admin secret (sent only by the staff BFF, server-side) is required. A JWT alone,
# tenant or staff, is authenticated but not authorized → 403; no credentials at all → 401.
# Same secret check as admin.verify_admin_secret; the env is read per call (not at import).
# Applied router-wide (APIRouter dependencies) so a route added later cannot ship open; handlers
# that need the principal also declare it (FastAPI runs the dependency once per request).

_MASTER_PRINCIPAL = {"sub": "00000000-0000-0000-0000-000000000001", "role": "admin"}


def _require_admin(
    request: Request,
    credentials: Optional[_Creds] = Depends(_HTTPBearer(auto_error=False)),
) -> dict:
    # AA-816 C9: a principal the staff bearer middleware verified (Cognito Bearer, or the Preview credential
    # capped at viewer) needs no secret; staff_policy_if_staff then applies POLICY to its role.
    if principal_verified():
        return dict(_MASTER_PRINCIPAL)
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    x_admin = request.headers.get("x-admin-secret")
    if x_admin:
        if not admin_secret:
            raise HTTPException(status_code=503, detail="Admin secret not configured")
        if secrets_equal(x_admin, admin_secret):
            return dict(_MASTER_PRINCIPAL)
        raise HTTPException(status_code=403, detail="Invalid admin secret")
    if credentials:
        _verify_jwt(credentials.credentials)  # 401 on a bad/expired token
        raise HTTPException(status_code=403, detail="Admin access required")
    raise HTTPException(status_code=401, detail="Not authenticated")


# AA-882 (GĐ3): staff_policy_if_staff narrows the staff calls (X-Admin-Secret) by role, per
# api/core/staff_authz.py POLICY; the routes' own guard still decides who else gets in.
router = APIRouter(prefix="/v1/pipeline", tags=["pipeline"],
                   dependencies=[Depends(_require_admin), Depends(staff_policy_if_staff)])


def _normalize_generated(generated: dict, tour: dict) -> dict:
    """Post-process LLM output: title-case name, strip forbidden words from name."""
    if not generated:
        return generated
    # Title-case name if ALL-CAPS (preserve if already mixed case)
    name = generated.get("name", "")
    if name and name == name.upper():
        generated["name"] = name.title()
    # Strip markdown bold from itineraries — ensure string type (GPT-4.1 may return dict)
    itin = generated.get("itineraries")
    if itin:
        if isinstance(itin, dict):
            parts = []
            for k, v in itin.items():
                parts.append(f"{k} -- {v}" if isinstance(v, str) else str(v))
            itin = "\n\n".join(parts)
        elif isinstance(itin, list):
            parts = []
            for item in itin:
                if isinstance(item, dict):
                    day   = item.get("day", "")
                    title = item.get("title", "")
                    desc  = item.get("description", "")
                    acts  = item.get("activities", [])
                    day_str = f"Day {day}"
                    if title:
                        day_str += f" — {title}"
                    if desc:
                        day_str += f"\n{desc}"
                    if acts:
                        act_list = ", ".join(str(a) for a in acts) if isinstance(acts, list) else str(acts)
                        day_str += f"\n*Activities: {act_list}*"
                    parts.append(day_str)
                else:
                    parts.append(str(item))
            itin = "\n\n---\n\n".join(parts)
        generated["itineraries"] = clean_itinerary(str(itin))
    # Same for highlights — ensure list of strings
    highlights = generated.get("highlights")
    if highlights and isinstance(highlights, list):
        generated["highlights"] = [str(h) if not isinstance(h, str) else h for h in highlights]
    return generated


def clean_itinerary(text: str) -> str:
    """Strip markdown bold markers (**) from itinerary — LLM sometimes adds them."""
    if not text:
        return text
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = text.replace("**", "")
    return text.strip()


def initial_state_for(
    tour: dict, *,
    brand_rules: Optional[dict] = None,
    seo: Optional[dict] = None,
    model_tier: Optional[str] = None,
    is_tenant_rewrite: bool = False,
    tenant_id: Optional[str] = None,
    generate_stage: str = "s1_generate",
    subtitle_focus: str = "standard",
    seo_mode: str = "dataforseo",
    feedback: str = "",
    previous_draft: Optional[dict] = None,
    tour_agency: Optional[dict] = None,
    batch_text: Optional[str] = None,
    batch_model_used: Optional[str] = None,
    batch_account: Optional[str] = None,
    writing_memory: Optional[dict] = None,
) -> dict:
    """The graph state of one rewrite (moved out of `_rewrite_tour`, AA-919): the live rewrite and the WM-20
    release-0 replay (scripts/writing_memory/wm20_release0_replay.py) build it with this one function, so the replay
    prompts are the live prompts. `writing_memory` (AA-919): the release view the writers' insertion path reads
    (services/writing_memory/insertion.resolve); None leaves the state exactly as before."""
    # P3-S3: Merge brand_rules into initial_state
    _br = brand_rules or {}
    initial_state = {
        "tour": tour,
        "seo": seo or {},
        "model_tier": model_tier,
        "is_tenant_rewrite": is_tenant_rewrite,
        "tenant_id": tenant_id,           # AA-620: real tenant for T2 (None for A1) -> record_call
        "generate_stage": generate_stage,  # AA-620: t2_generate (T2) vs s1_generate (A1)
        "few_shots": [],
        "generated": {},
        "quality_score": 0.0,
        "retry_count": 0,
        "feedback": feedback,
        "error": "",
        "cost_usd": 0.0,
        "model_used": "",
        "brand_system_prompt":  _br.get("system_prompt", ""),
        "brand_style_guide":    _br.get("style_guide", ""),
        "brand_forbidden_words": _br.get("forbidden_words", []),
        "rewrite_language":     _br.get("rewrite_language", "en-US"),
        # AA-202: brand differentiation fields
        "brand_core_idea":        _br.get("core_idea", ""),
        "brand_customer_segment": _br.get("customer_segment", ""),
        "brand_customer_mindset": _br.get("customer_mindset", ""),
        "brand_voice_examples":   _br.get("voice_examples", []),
        "brand_good_examples":    _br.get("good_examples", ""),
        # AA-792: brand pack identity for the T2 CLIENT BRAND block (empty for A1).
        "brand_name":             _br.get("brand_name", ""),
        "brand_type":             _br.get("brand_type", ""),
        "brand_default_cta":      _br.get("default_cta", ""),
        # AA-800: the tenant's explicit signature words (custom_validators); T2 only.
        "brand_signature_words":  _br.get("signature_words", []),
        "subtitle_focus":       subtitle_focus,
        "seo_mode":             seo_mode,
        # brand audit defaults
        "brand_audit_status": "",
        "brand_audit_codes":  [],
        "brand_audit_issues": [],
        "brand_audit_fields": [],
        "lessons_extracted":  [],
        "fix_pass_applied":   False,
        "fix_pass_fields":    [],
        "quality_shadow":     [],   # AA-925
        "degraded":           [],   # AA-925
    }
    if previous_draft and is_tenant_rewrite:
        initial_state["previous_draft"] = previous_draft   # AA-793
    if tour_agency and not is_tenant_rewrite:
        initial_state["tour_agency"] = tour_agency         # AA-645 step 0 (S1 writer v2 fact sheet)
    # AA-606: seed Bedrock Batch attempt-1 output for build_graph_from_generated's seed node.
    if batch_text is not None:
        initial_state["batch_text"] = batch_text
        initial_state["model_used"] = batch_model_used or "satellite-haiku-4-5"
        initial_state["satellite_account"] = batch_account
    if writing_memory is not None:
        initial_state["writing_memory"] = writing_memory   # AA-919 (WM-18 / WM-20)
    return initial_state


async def _rewrite_tour(
    tour: dict, idx: int, total: int,
    brand_rules: dict = None,
    seo: dict = None,
    model_tier: Optional[str] = None,  # AA-518 — see admin_pipeline.py's RewriteRequest comment
    is_tenant_rewrite: bool = False,
    subtitle_focus: str = "standard",
    seo_mode: str = "dataforseo",
    on_stage=None,  # AA-250 B2: optional async callback(node_name: str), fired after each node completes
    batch_text: Optional[str] = None,  # AA-606: pre-generated writer text (Bedrock Batch attempt-1)
    batch_model_used: Optional[str] = None,  # AA-606: model label to record for the batch write
    batch_account: Optional[str] = None,  # AA-606: satellite account the batch ran on (acc1/acc3)
    tenant_id: Optional[str] = None,  # AA-620: real tenant UUID -> logged in llm_call_log (T2)
    generate_stage: str = "s1_generate",  # AA-620: "t2_generate" for tenant T2, "s1_generate" for A1
    feedback: str = "",  # AA-639: seeds generate_node's PREVIOUS ATTEMPT FEEDBACK (T3 repair rounds)
    previous_draft: Optional[dict] = None,  # AA-793: a T3 repair round revises this draft (T2 only)
    tour_agency: Optional[dict] = None,  # AA-645: raw_tours group_size / best_time_to_go for the S1 writer v2 (A1)
    writing_memory: Optional[dict] = None,  # AA-919: the writing-memory release view (None = memory off)
    resume_state: Optional[dict] = None,  # AA-993 G2: saved writer state of a released s1_rewrite job (A1 only)
    on_writer_state=None,  # AA-993 G2: optional async callback(delta: dict) after each writer node (A1 only)
) -> dict:
    """Rewrite single tour using LangGraph.

    AA-993 G2: `resume_state` (an A1 rewrite, not a batch seed) enters build_graph_resumed() after the writer with that
    state, so a re-claimed s1_rewrite job does not pay the writer again; `on_writer_state` receives the state a writer
    node produced (s1_checkpoint.writer_state_delta), which is what the job saves for that resume.

    AA-606: when batch_text is set, entry is build_graph_from_generated() — it seeds the pre-written
    Bedrock Batch attempt-1 output instead of calling the writer live. A gate failure still falls
    into the on-demand retry loop (generate_node attempt-2/3), so behaviour past the gate is identical.
    """
    logger.info("rewriting_tour", idx=idx, total=total, name=tour.get("name", ""),
                via_batch=batch_text is not None)

    try:
        from services.content_generation.graph import build_graph_from_generated
        # AA-793: a tenant rewrite runs the draft-tracking variant (best draft wins, retries revise
        # the best draft) unless both T2_BEST_DRAFT and T2_TARGETED_REPAIR are off. A1 is unchanged.
        from services.content_generation.t2_repair import t2_drafts_graph_enabled
        a1_resumable = batch_text is None and not is_tenant_rewrite
        if batch_text is not None:
            graph = build_graph_from_generated()
        elif resume_state and a1_resumable:
            from services.content_generation.graph import build_graph_resumed
            graph = build_graph_resumed()
        elif is_tenant_rewrite and t2_drafts_graph_enabled():
            graph = build_graph(t2_drafts=True)
        else:
            graph = build_graph()
        initial_state = initial_state_for(
            tour, brand_rules=brand_rules, seo=seo, model_tier=model_tier, is_tenant_rewrite=is_tenant_rewrite,
            tenant_id=tenant_id, generate_stage=generate_stage, subtitle_focus=subtitle_focus, seo_mode=seo_mode,
            feedback=feedback, previous_draft=previous_draft, tour_agency=tour_agency, batch_text=batch_text,
            batch_model_used=batch_model_used, batch_account=batch_account, writing_memory=writing_memory,
        )
        if resume_state and a1_resumable:
            initial_state["resume_state"] = resume_state
        report_writer = on_writer_state is not None and a1_resumable
        # AA-993 G2: the delta is taken against a deep copy, so a node that mutates a nested value of the state in
        # place (a list it appends to) still shows that value as changed.
        writer_baseline = None
        if report_writer:
            from services.content_generation.s1_checkpoint import state_baseline
            writer_baseline = state_baseline(initial_state)

        # AA-250 B2: stream node-by-node (instead of graph.invoke()) so on_stage can report
        # live progress to shared.pipeline_jobs.current_stage. stream_mode="updates" emits
        # {node_name: node_output} per completed step; every node in graph.py returns
        # `{**state, ...}` (verified: generate_node/validate_node/judge_node/brand_audit_node/
        # flag_fix_node/increment_retry/revalidate_node all spread the full incoming state), so
        # node_output is always the COMPLETE state at that point, not a partial delta — tracking
        # the most recent node_output reconstructs the same final state graph.invoke() used to
        # return. Sync node functions are auto-dispatched to a thread-pool executor internally by
        # LangGraph's Runnable wrapping (langgraph._internal._runnable: iscoroutinefunction check
        # -> run_in_executor) when driven via astream, so this does not block the event loop —
        # the manual run_in_executor(None, run_graph) wrapping the old sync invoke() is no longer
        # needed.
        result = dict(initial_state)
        async for event in graph.astream(initial_state, stream_mode="updates"):
            for node_name, node_output in event.items():
                result = node_output
                if report_writer and node_name == "generate":
                    from services.content_generation.s1_checkpoint import writer_state_delta
                    delta = writer_state_delta(writer_baseline, node_output)
                    if delta is not None:
                        await on_writer_state(delta)
                if on_stage is not None:
                    await on_stage(node_name)

        return {
            "idx": idx,
            "src_name": tour.get("name", ""),
            "country": tour.get("country", ""),
            "duration": tour.get("duration", ""),
            "generated": _normalize_generated(result.get("generated", {}), tour),
            "quality_score": result.get("quality_score", 0.0),
            "model_used": result.get("model_used", ""),
            "cost_usd": result.get("cost_usd", 0.0),
            "retry_count": result.get("retry_count", 0),
            "error":         result.get("error", ""),
            "is_branded":    result.get("is_branded", True),
            "failure_codes": result.get("failure_codes", []),
            "sub_scores":    result.get("sub_scores", {}),
            "passed_count":  result.get("passed_count", 0),
            "failed_count":  result.get("failed_count", 0),
            "brand_audit_status": result.get("brand_audit_status", ""),
            "brand_audit_codes":  result.get("brand_audit_codes", []),
            "brand_audit_issues": result.get("brand_audit_issues", []),
            "brand_audit_fields": result.get("brand_audit_fields", []),
            "lessons_extracted":  result.get("lessons_extracted", []),
            "fix_pass_applied":   result.get("fix_pass_applied", False),
            "fix_pass_fields":    result.get("fix_pass_fields", []),
            # AA-209: propagate judge_* from graph state so _build_generated_metadata can persist
            # metadata.judge. Without these the guard result.get("judge_brand_fit") is not None never
            # fires → PART 2/3 were a prod no-op. None when the judge didn't run (guard then skips).
            "judge_brand_fit":            result.get("judge_brand_fit"),
            "judge_cross_brand_distinct": result.get("judge_cross_brand_distinct"),
            "judge_mission_present":      result.get("judge_mission_present"),
            "judge_feedback":             result.get("judge_feedback"),
            "judge_score":                result.get("judge_score"),
            # AA-645 (S249): rubric-v2 verdict details (None on a v1 verdict; the metadata guard skips them).
            "judge_rubric":               result.get("judge_rubric"),
            "judge_unsupported":          result.get("judge_unsupported"),
            "judge_caps":                 result.get("judge_caps"),
            # AA-213: propagate fallback_used so metadata.fallback_used isn't always-False (graph
            # declares it but _rewrite_tour rebuilds the dict — same strip class as the judge_* fix).
            "fallback_used":   result.get("fallback_used", False),
            # AA-296/397: same strip class as fallback_used above — propagate satellite_account so it
            # isn't always-None (graph declares it but _rewrite_tour rebuilds the dict).
            "satellite_account": result.get("satellite_account"),
            # AA-289/AA-288: prompt_version + cache token counts, same strip class as fallback_used/
            # satellite_account above — graph.py declares these in ContentState but _rewrite_tour
            # rebuilds the dict from scratch, so anything not explicitly re-listed here is lost.
            "prompt_version":      result.get("prompt_version", ""),
            "cache_read_tokens":   result.get("cache_read_tokens", 0),
            "cache_write_tokens":  result.get("cache_write_tokens", 0),
            # AA-215: propagate revalidate outcome for observability/persist (brand_audit_status
            # POST-fix already propagated above on line ~159).
            "revalidate_ran":    result.get("revalidate_ran", False),
            "revalidate_passed": result.get("revalidate_passed", False),
            # AA-353: same strip class as judge_*/fallback_used above — propagate the itinerary
            # per-day ratio records so _build_generated_metadata can persist metadata.itinerary_compression.
            "itinerary_day_ratios": result.get("itinerary_day_ratios", []),
            # AA-691: same strip class — A1 grounding outcome for metadata.grounding.
            "grounding_ran":             result.get("grounding_ran", False),
            "grounding_found":           result.get("grounding_found", []),
            "grounding_repaired_fields": result.get("grounding_repaired_fields", []),
            "grounding_violations":      result.get("grounding_violations", []),
            "grounding_notes":           result.get("grounding_notes", []),
            # AA-793: per-attempt judged scores + whether the hitl exit restored an earlier draft (T2).
            "draft_scores":        result.get("draft_scores", []),
            "best_draft_restored": result.get("best_draft_restored", False),
            # AA-925: same strip class — validate's shadow findings and the fail-open paths that ran.
            "quality_shadow":      result.get("quality_shadow", []),
            "degraded":            result.get("degraded", []),
            # AA-645 step 0: same strip class — "v2" when the S1 writer v2 wrote the kept draft.
            "writer_version":      result.get("writer_version"),
            # AA-919: same strip class — the writing-memory release the kept draft was written with (A1 v2 stamp).
            "writing_release":     result.get("writing_release"),
            "status": "success" if result.get("generated") and len(result.get("generated", {})) > 0 else "failed",
        }

    except Exception as e:
        logger.error("rewrite_failed", idx=idx, error=str(e))
        return {
            "idx": idx,
            "src_name": tour.get("name", ""),
            "country": tour.get("country", ""),
            "status": "failed",
            "error": str(e),
        }


@router.post("/run")
async def run_pipeline(
    file: UploadFile = File(...),
    max_tours: int = 5,
):
    """
    Upload Excel file → parse → rewrite up to max_tours tours.
    Returns before/after comparison with quality scores.

    Args:
        file: Excel file (.xlsx)
        max_tours: Max number of tours to process (default 5, max 20)
    """
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx/.xls files supported")

    max_tours = min(max_tours, 20)  # Hard cap at 20

    # Save upload to temp file
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    logger.info("pipeline_started", filename=file.filename, max_tours=max_tours)

    try:
        # Parse Excel
        parser = ExcelParser(tmp_path, source_file=file.filename)
        records = parser.parse()

        if not records:
            raise HTTPException(400, "No valid tour records found in Excel file")

        # Limit tours
        records = records[:max_tours]
        logger.info("tours_parsed", total=len(records))

        # Rewrite tours concurrently (max 3 at a time to avoid rate limits)
        semaphore = asyncio.Semaphore(3)

        async def bounded_rewrite(tour, idx):
            async with semaphore:
                return await _rewrite_tour(tour, idx + 1, len(records))

        tasks = [bounded_rewrite(tour, i) for i, tour in enumerate(records)]
        results = await asyncio.gather(*tasks)

        # Summary stats
        successful = [r for r in results if r.get("status") == "success"]
        failed = [r for r in results if r.get("status") == "failed"]
        total_cost = sum(r.get("cost_usd", 0) for r in results)
        avg_quality = (
            sum(r.get("quality_score", 0) for r in successful) / len(successful)
            if successful else 0
        )

        return JSONResponse({
            "batch_id": str(uuid.uuid4()),
            "filename": file.filename,
            "summary": {
                "total": len(records),
                "successful": len(successful),
                "failed": len(failed),
                "avg_quality_score": round(avg_quality, 2),
                "total_cost_usd": round(total_cost, 4),
            },
            "results": results,
        })

    finally:
        os.unlink(tmp_path)


# ── SF per-tour endpoint — MOVED TO /admin/run-tour (admin_pipeline.py) ──────
# Kept as stub so Step Functions ARN references compile; real logic in admin_pipeline.py

class TourRunRequest(BaseModel):
    tour_id: str
    batch_id: str
    tenant_id: str
    retry_count: int = 0
    validation_feedback: list = []
    seo_mode: str = "dataforseo"
    rewrite_language: str = "en-US"
    model_tier: Optional[str] = None  # AA-518 — dead-stub class (see comment above), kept consistent
    subtitle_focus: str = "standard"


# ── Step Functions Execution Status ──────────────────────────────────────────

class ExecutionStatus(BaseModel):
    execution_id: str
    status: str
    start_date: str | None = None
    stop_date:  str | None = None
    tours_processed: int | None = None


@router.get("/execution/{execution_id:path}", response_model=ExecutionStatus)
async def get_execution_status(
    execution_id: str,
    tenant=Depends(_require_admin),
):
    """Poll Step Functions execution status by ARN."""
    import json as _json
    sf = _boto3.client("stepfunctions", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    try:
        resp   = sf.describe_execution(executionArn=execution_id)
        output = None
        if resp.get("output"):
            output = _json.loads(resp["output"])
        return ExecutionStatus(
            execution_id=execution_id,
            status=resp["status"],
            start_date=str(resp.get("startDate", "")),
            stop_date=str(resp.get("stopDate", "")) if resp.get("stopDate") else None,
            tours_processed=output.get("tours_processed") if output else None,
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Review Queue ──────────────────────────────────────────────────────────────

@router.get("/review-queue")
async def get_review_queue(
    request: Request,
    tenant=Depends(_require_admin),
    page: int = 1,
    page_size: int = 20,
):
    """Get tours pending HITL review from review_queue table."""
    pool = request.app.state.pool
    # AA-229: CIS is single-tenant; review_queue rows are enqueued under master
    # (_MASTER_TENANT_ID). Pin fetch to master so the admin UI sees them. Multi-tenant
    # review fetch is deferred to ACP.
    tenant_id = "00000000-0000-0000-0000-000000000001"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT rq.id, rq.tour_id, rq.generated_content_id,
                   rq.review_status, rq.score_overall, rq.failure_summary, rq.created_at,
                   gc.aa_name, gc.aa_subtitle, gc.aa_summary,
                   gc.seo_title, gc.seo_meta,
                   rt.src_name, rt.src_subtitle, rt.src_summary,
                   rt.country, rt.duration
            FROM silver_aa_internal.review_queue rq
            JOIN silver_aa_internal.generated_content gc ON gc.id = rq.generated_content_id
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = rq.tour_id
            WHERE rq.tenant_id = $1::uuid
              AND rq.review_status = 'pending'
              AND {live_source_sql("rt")}  -- AA-986 L1: a deleted / replaced source cannot be approved
            ORDER BY rq.created_at DESC
            LIMIT $2 OFFSET $3
        """, tenant_id, page_size, offset)

        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM silver_aa_internal.review_queue rq
            WHERE rq.tenant_id = $1::uuid AND rq.review_status = 'pending'
              AND {live_source_exists_sql("rq.tour_id")}
        """, tenant_id)

    return {
        "data": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total},
    }


@router.post("/review-queue/{review_id}/approve")
async def approve_review(
    review_id: str,
    request: Request,
    tenant=Depends(_require_admin),
):
    """Approve a tour in review queue.

    AA-212: atomic-claim the pending row so a double-click cannot double-export. The claim
    only succeeds while review_status='pending'; a second call claims nothing → 409, and
    process_export (which is NOT idempotent against its EventBridge re-fire) runs at most once.
    Token branch: a legacy Step Functions token → send_task_success; NULL token (admin/direct
    enqueue path) → forward export via process_export.
    """
    pool = request.app.state.pool
    # AA-229: CIS is single-tenant; review_queue rows are enqueued under master
    # (_MASTER_TENANT_ID). Pin fetch to master so the admin UI sees them. Multi-tenant
    # review fetch is deferred to ACP.
    tenant_id = "00000000-0000-0000-0000-000000000001"

    async with pool.acquire() as conn:
        # Atomic claim: flip pending→approved in one statement. Only the first caller wins.
        # AA-234: the claim ALSO gates on the linked version — a human-edited version may be
        # claimed ONLY after a passing re-validation. The gate lives in the UPDATE WHERE (one
        # query, no extra round-trip): block when human_edited AND revalidate_passed is not TRUE.
        # AA-986 L1: the claim also requires a live source version (not trashed / superseded / deleted at
        # S0/A0), so approving a review of a deleted tour can no longer publish it back as an active Master.
        claimed = await conn.fetchrow(f"""
            UPDATE silver_aa_internal.review_queue rq
            SET review_status = 'approved', reviewed_at = NOW()
            FROM silver_aa_internal.generated_content gc
            WHERE rq.id = $1::uuid AND rq.tenant_id = $2::uuid
              AND rq.review_status = 'pending'
              AND gc.id = rq.generated_content_id
              AND NOT (COALESCE(gc.human_edited, false) AND gc.revalidate_passed IS NOT TRUE)
              AND {live_source_exists_sql("rq.tour_id")}
            RETURNING rq.generated_content_id, rq.step_fn_task_token
        """, review_id, tenant_id)

        if not claimed:
            # Disambiguate: a still-pending row that failed to claim has a source that is not live
            # (AA-986) or was blocked by the re-validation gate; otherwise it was already processed
            # (or not found).
            pending = await conn.fetchrow("""
                SELECT rq.tour_id::text AS tour_id,
                       (gc.human_edited = true AND gc.revalidate_passed IS NOT TRUE) AS blocked
                FROM silver_aa_internal.review_queue rq
                JOIN silver_aa_internal.generated_content gc ON gc.id = rq.generated_content_id
                WHERE rq.id = $1::uuid AND rq.tenant_id = $2::uuid
                  AND rq.review_status = 'pending'
            """, review_id, tenant_id)
            if pending:
                src = await source_state(conn, pending["tour_id"])
                if not src.live:
                    raise HTTPException(
                        status_code=409,
                        detail=source_not_live_message(src, "approve this review"),
                        headers={"X-Error-Code": SOURCE_NOT_LIVE},
                    )
            if pending and pending["blocked"]:
                raise HTTPException(
                    status_code=409,
                    detail="Human-edited content must pass re-validation before approve "
                           "(revalidate_passed is not TRUE). Run re-validate and retry.",
                )
            raise HTTPException(status_code=409, detail="Review already processed or not found")

        generated_content_id = str(claimed["generated_content_id"])
        task_token = claimed["step_fn_task_token"]

        await conn.execute("""
            UPDATE silver_aa_internal.generated_content
            SET status = 'approved'
            WHERE id = $1::uuid
        """, generated_content_id)

    # Side effects outside the DB transaction. Branch on the claimed token.
    # NULL token (admin/direct enqueue path) → forward export. Use `is not None`, not a falsy
    # check: an empty-string token is a real SF token and must NOT fall into the export branch.
    exported = False
    if task_token is not None:
        # Legacy Step Functions path — unchanged.
        try:
            sfn = _boto3.client(
                "stepfunctions",
                region_name=os.environ.get("AWS_REGION", "us-west-1"),
            )
            sfn.send_task_success(
                taskToken=task_token,
                output=json.dumps({
                    "decision": "approved",
                    "review_id": review_id,
                    "version_id": generated_content_id,
                }),
            )
        except Exception as e:
            import structlog as _sl
            _sl.get_logger().warning("send_task_success_failed", error=str(e))
    else:
        # Admin-path forward export (no SF token). Runs once per claim.
        from services.export.handler import process_export
        try:
            await process_export(str(generated_content_id))
            exported = True
        except SourceNotLiveError as e:
            # The source was trashed between the claim and the export (race). Nothing is published and
            # process_export already moved the version approved → hitl; close the claimed review as
            # 'dismissed' (the _enqueue_review state for a dead source) so no row says "approved" without a
            # Master, and give the reviewer the same 409 as a dead source caught before the claim.
            import structlog as _sl
            _sl.get_logger().warning(
                "approve_export_source_not_live", review_id=review_id,
                version_id=generated_content_id, reason=e.state.reason,
            )
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE silver_aa_internal.review_queue
                    SET review_status = 'dismissed', reviewed_at = NOW()
                    WHERE id = $1::uuid AND review_status = 'approved'
                """, review_id)
            raise HTTPException(
                status_code=409,
                detail=source_not_live_message(e.state, "approve this review"),
                headers={"X-Error-Code": SOURCE_NOT_LIVE},
            )
        except Exception as e:
            import structlog as _sl
            _sl.get_logger().error(
                "approve_export_failed", review_id=review_id,
                version_id=generated_content_id, error=str(e),
            )

    return {
        "status": "approved",
        "review_id": review_id,
        "exported": exported,
        "sf_notified": task_token is not None,
    }


@router.post("/review-queue/{review_id}/reject")
async def reject_review(
    review_id: str,
    request: Request,
    tenant=Depends(_require_admin),
):
    """Reject a tour → send_task_failure → SF goes to TourRejected.

    AA-212: atomic-claim the pending row (pending→rejected in one statement). A second call
    claims nothing → 409. No export on the reject path.

    AA-441 (bug #5, AA-438-00-SUMMARY #5): this used to only flip review_queue.review_status,
    never generated_content.status — a rejected tour stayed stuck at 'hitl' forever, invisible
    to the Review Queue's default "pending" filter (it's neither pending nor approved) while
    also not reading as rejected anywhere. Mirrors approve_review()'s own pattern just above
    (which does update generated_content.status = 'approved' on its claim) — content_status_enum
    already has a 'rejected' value (migration 002) for exactly this.
    """
    pool = request.app.state.pool
    # AA-229: CIS is single-tenant; review_queue rows are enqueued under master
    # (_MASTER_TENANT_ID). Pin fetch to master so the admin UI sees them. Multi-tenant
    # review fetch is deferred to ACP.
    tenant_id = "00000000-0000-0000-0000-000000000001"

    async with pool.acquire() as conn:
        claimed = await conn.fetchrow("""
            UPDATE silver_aa_internal.review_queue
            SET review_status = 'rejected', reviewed_at = NOW()
            WHERE id = $1::uuid AND tenant_id = $2::uuid AND review_status = 'pending'
            RETURNING step_fn_task_token, generated_content_id
        """, review_id, tenant_id)

        if not claimed:
            raise HTTPException(status_code=409, detail="Review already processed or not found")

        task_token = claimed["step_fn_task_token"]
        generated_content_id = str(claimed["generated_content_id"])

        await conn.execute("""
            UPDATE silver_aa_internal.generated_content
            SET status = 'rejected'
            WHERE id = $1::uuid
        """, generated_content_id)

        # AA-476: previously nothing here ever touched raw_tours.pipeline_status — a rejected
        # tour stayed 'ingested' forever, which made the batch's pipeline_runs.status stick at
        # 'ingesting' indefinitely (sync_batch_completion counted it as still-pending forever,
        # even once every other tour in the batch was long done). See services/export/handler.py.
        tour_row = await conn.fetchrow("""
            SELECT tour_id FROM silver_aa_internal.generated_content WHERE id = $1::uuid
        """, generated_content_id)
        if tour_row and tour_row["tour_id"]:
            from services.export.handler import mark_tour_rejected
            await mark_tour_rejected(conn, str(tour_row["tour_id"]))

    # Send task failure to Step Functions only when a real token exists (is None → no SF, no export).
    if task_token is not None:
        try:
            sfn = _boto3.client(
                "stepfunctions",
                region_name=os.environ.get("AWS_REGION", "us-west-1"),
            )
            sfn.send_task_failure(
                taskToken=task_token,
                error="TourRejectedByReviewer",
                cause="Human reviewer rejected the tour content",
            )
        except Exception as e:
            import structlog as _sl
            _sl.get_logger().warning("send_task_failure_failed", error=str(e))

    return {"status": "rejected", "review_id": review_id, "sf_notified": task_token is not None}


# =============================================================================
# GET /sources — Upload history (TD-2 UI, added 29/04/2026)
# =============================================================================
@router.get("/sources")
async def get_sources(
    request: Request,
    limit: int = 20,
    tenant=Depends(_require_admin),
):
    """Return upload history for current tenant."""
    pool = request.app.state.pool
    tenant_id = tenant.get("sub", "00000000-0000-0000-0000-000000000001")

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                id,
                filename,
                s3_path,
                file_hash,
                file_size_kb,
                row_count,
                parsed_at
            FROM silver_aa_internal.raw_sources
            WHERE tenant_id = $1::uuid
            ORDER BY parsed_at DESC
            LIMIT $2
        """, tenant_id, limit)

    return {
        "sources": [
            {
                "id":           str(r["id"]),
                "filename":     r["filename"],
                "s3_path":      r["s3_path"],
                "file_hash":    r["file_hash"][:12] + "..." if r["file_hash"] else None,
                "file_size_kb": r["file_size_kb"],
                "row_count":    r["row_count"],
                "parsed_at":    r["parsed_at"].isoformat() if r["parsed_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }
