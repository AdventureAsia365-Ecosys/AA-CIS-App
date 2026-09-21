# api/routers/admin_pipeline.py
# Admin-only pipeline endpoints — mounted at /admin/* (no Lambda Authorizer at API GW)
# Auth: x-admin-secret header only (no tenant JWT accepted) — EXCEPT /brand-identity GET/POST
# (see _resolve_brand_tenant_id, AA-424): those two accept a tenant JWT too, real tenant_id first.

import asyncio
import base64
import datetime
import json
import os
from uuid import UUID

import asyncpg
import boto3 as _boto3
import structlog
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from typing import Optional, List

from api.routers.admin import verify_admin_secret
from api.routers.auth import verify_jwt
from api.routers.v1_pipeline import _rewrite_tour
from shared.validators.prompt_sanitize import (
    sanitize_text, sanitize_list, MAX_LONG_FIELD_LEN, MAX_SHORT_FIELD_LEN, MAX_SYSTEM_PROMPT_LEN,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/admin", tags=["admin-pipeline"])

_pipeline_semaphore = asyncio.Semaphore(2)

# AA-223: strong refs to fire-and-forget run-tour jobs. asyncio only keeps a weak
# ref to scheduled tasks — without this the task can be GC'd mid-flight, leaving the
# job stuck 'running' forever. add_done_callback(discard) releases the ref on finish.
_background_tasks: set = set()


def _is_uuid(value) -> bool:
    """True when value is a valid UUID (shared.pipeline_runs.batch_id is uuid-typed).

    AA-210: guards the `WHERE batch_id = $N::uuid` accounting UPDATEs against non-UUID
    batch_ids (e.g. ad-hoc verification labels) that would otherwise raise asyncpg DataError.
    """
    try:
        UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def _as_list(v):
    """Guarantee a list. Handles list, JSON-string, dict, None — AA-235 shape guard.

    jsonb columns arrive as str OR list depending on driver path. A JSON *object*
    (e.g. empty DataForSEO `{seed: null}`) must collapse to [] — never pass a dict
    downstream, or FE [...spread] / BE slice[:25] crash.
    """
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


# AA-211/AA-212: master tenant for the aa_internal pipeline (same constant used inline elsewhere).
_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _is_publishable(result: dict) -> bool:
    """AA-211: audit-aware publish gate (mirror catalog readiness + v5 spec).

    quality_score >= 7.0 is the floor; additionally block manual_check, and block brand-flagged
    tours the fix pass did not repair (flagged + not fix_pass_applied). Everything else — a clean
    pass, or flagged-but-fixed (Terra case), or no brand profile at all — is publishable.
    """
    audit = result.get("brand_audit_status")
    return (
        result.get("quality_score", 0.0) >= 7.0
        and audit != "manual_check"
        and not (audit == "flagged" and not result.get("fix_pass_applied"))
    )


def _build_failure_summary(result: dict) -> str:
    """AA-212: human-readable reason a tour was routed to the HITL review_queue."""
    audit = result.get("brand_audit_status")
    score = float(result.get("quality_score") or 0.0)
    codes = list(result.get("failure_codes", [])) + list(result.get("brand_audit_codes", []))
    parts = []
    if audit:
        parts.append(f"brand_audit={audit}")
    if codes:
        parts.append("codes=" + ",".join(str(c) for c in codes))
    if score < 7.0:
        parts.append(f"low_quality(score={score:.1f}<7.0)")
    return "; ".join(parts) or "blocked: not publishable"


async def _enqueue_review(conn, tour_id, generated_content_id, result) -> None:
    """AA-212: enqueue a blocked / low-quality tour into review_queue for HITL review.

    Idempotent: the NOT EXISTS guard skips re-insert when a pending row already exists for this
    generated_content_id (guards pipeline re-runs against double-enqueue). SF columns are left
    NULL — that NULL is what marks the admin/direct-export path picked up by approve_review.
    """
    await conn.execute("""
        INSERT INTO silver_aa_internal.review_queue (
            tour_id, generated_content_id, tenant_id,
            failure_summary, score_overall, review_status
        )
        SELECT $1::uuid, $2::uuid, $3::uuid, $4, $5, 'pending'
        WHERE NOT EXISTS (
            SELECT 1 FROM silver_aa_internal.review_queue
            WHERE generated_content_id = $2::uuid
              AND review_status = 'pending'
        )
    """,
        tour_id,
        generated_content_id,
        _MASTER_TENANT_ID,
        _build_failure_summary(result),
        float(result.get("quality_score") or 0.0),
    )


def _trim_to_word_boundary(text, limit, sentence=False):
    """Trim text to <= limit chars without splitting a word.

    Backs up to the nearest space at or before `limit` so a word is never
    cut mid-token. When sentence=True, prefers the last sentence-terminating
    punctuation (. ! ?) within the limit so trimmed meta still ends on a
    complete sentence (avoids META_INCOMPLETE_SENTENCE).
    """
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    if sentence:
        cut = max(window.rfind("."), window.rfind("!"), window.rfind("?"))
        if cut != -1:
            return window[:cut + 1].rstrip()
    space = window.rfind(" ")
    if space != -1:
        return window[:space].rstrip()
    # single over-long token with no space — hard cut at limit
    return window.rstrip()


def _build_generated_metadata(result, *, brand_rule_id, brand_name, seo_mode,
                              model_used, llm_cost_usd, dataforseo_used, batch_id=None):
    """Assemble the generated_content.metadata dict for a new version row.

    AA-209: merge a ``judge`` object (brand_fit / distinct / mission_present / feedback /
    judge_score) when the GPT-4.1 judge ran. The judge only runs for branded tours with a
    differentiation profile; for legacy/no-profile brands judge_node returns no ``judge_*`` keys,
    so ``metadata.judge`` is simply omitted (absent, not null) — no crash, no clobber of the base
    keys below.

    AA-289/AA-288: prompt_version + cache_read_tokens/cache_write_tokens go here (JSONB), not a
    new pipeline_runs column — this is the only place quality_score (score_overall, right below)
    is already recorded per-tour, so it's the only place that can actually answer "quality by
    prompt_version". pipeline_runs is a batch/ingestion aggregate with no per-tour quality signal
    to join against (STEP 0 finding) — deliberately not duplicated there.

    AA-353: same reasoning for ``itinerary_compression`` — per-day actual/source ratio + nudge
    outcome, keyed by this same result dict. shared.pipeline_runs.ingest_details (migration 091)
    was considered and rejected: that column is deliberately scoped to ingest-level diagnostics
    (091's own comment), not per-tour quality signals — reusing it would recreate the exact
    collision 091 was written to avoid. Omitted (not null) when itineraries wasn't the structured
    array contract (empty list) — no crash, no clobber, mirrors the ``judge`` guard below.
    """
    metadata = {
        "brand_rule_id":   brand_rule_id,
        "brand_name":      brand_name,
        "seo_mode":        seo_mode,
        "model_used":      model_used,
        "llm_cost_usd":    llm_cost_usd,
        "dataforseo_used": dataforseo_used,
        "generated_at":    datetime.datetime.utcnow().isoformat() + "Z",
        "pipeline_version": "v2",
        "score_overall":   result.get("quality_score"),   # AA-213: was unused/null
        "fallback_used":   result.get("fallback_used", False),  # AA-213
        "batch_id":        batch_id,                       # AA-213: traceability
        "revalidate_ran":    result.get("revalidate_ran", False),
        "revalidate_passed": result.get("revalidate_passed", False),
        "prompt_version":      result.get("prompt_version", ""),
        "cache_read_tokens":   result.get("cache_read_tokens", 0),
        "cache_write_tokens":  result.get("cache_write_tokens", 0),
    }
    if result.get("judge_brand_fit") is not None:
        metadata["judge"] = {
            "brand_fit":       result.get("judge_brand_fit"),
            "distinct":        result.get("judge_cross_brand_distinct"),
            "mission_present": result.get("judge_mission_present"),
            "feedback":        result.get("judge_feedback"),
            "judge_score":     result.get("judge_score"),
        }
    _day_ratios = result.get("itinerary_day_ratios") or []
    if _day_ratios:
        from services.content_generation.graph import ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX
        _violations = [
            d for d in _day_ratios
            if d.get("ratio") is not None and not (
                ITINERARY_CLAMP_MIN <= d["ratio"] <= ITINERARY_CLAMP_MAX
            )
        ]
        metadata["itinerary_compression"] = {
            "day_ratios":          _day_ratios,
            "nudged_day_count":    sum(1 for d in _day_ratios if d.get("nudged")),
            "violation_day_count": len(_violations),
            "still_violating_after_nudge": [
                d["day"] for d in _violations
                if d.get("ratio_after_nudge") is not None and not (
                    ITINERARY_CLAMP_MIN <= d["ratio_after_nudge"] <= ITINERARY_CLAMP_MAX
                )
            ],
        }
    return metadata


def _extract_judge_score(metadata):
    """AA-220 (H1): pull metadata.judge.judge_score from a generated_content metadata blob.

    Mirrors get_tour_version_detail's canonical metadata handling — the jsonb column arrives as a
    str OR a dict depending on the asyncpg codec path. Returns None for any missing/malformed level
    (no metadata, unparseable, no judge object) so the list endpoint degrades to "—" in the UI.
    """
    if not metadata:
        return None
    try:
        m = json.loads(metadata) if isinstance(metadata, str) else dict(metadata)
    except Exception:
        return None
    j = m.get("judge") if isinstance(m, dict) else None
    return j.get("judge_score") if isinstance(j, dict) else None


# ── Pydantic models ───────────────────────────────────────────────────────────

class TourRunRequest(BaseModel):
    tour_id: str
    batch_id: str
    tenant_id: str
    retry_count: int = 0
    validation_feedback: list = []
    seo_mode: str = "standard"
    rewrite_language: str = "en-US"
    # AA-518: was "haiku" — None now means "no explicit per-request choice, defer to the admin's
    # s1_generate stage config" (LLMClient.generate()'s own new fallback chain). An explicit
    # value (including AA-237's "sonnet" auto-upgrade re-run below) still wins, unchanged.
    model_tier: Optional[str] = None
    subtitle_focus: str = "standard"
    brand_rules_version: Optional[int] = None
    brand_name: Optional[str] = None
    brand_identity_id: Optional[str] = None
    allow_auto_upgrade: bool = False  # AA-237: opt-in haiku→sonnet auto-upgrade (default OFF)


class UploadUrlRequest(BaseModel):
    filename: str
    content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    seo_mode: str = "dataforseo"


class UploadUrlResponse(BaseModel):
    upload_url: str
    s3_key: str
    bucket: str


class IngestS3Request(BaseModel):
    s3_key: str
    bucket: str = ""
    seo_mode: str = "dataforseo"
    model_tier: Optional[str] = None  # AA-518 — see RewriteRequest's own comment above
    subtitle_focus: str = "standard"
    dry_run: bool = False
    tenant_id: str = "00000000-0000-0000-0000-000000000001"
    max_tours: int = 500


class BrandIdentityUpdate(BaseModel):
    system_prompt: Optional[str] = None
    style_guide:   Optional[str] = None
    forbidden_words: Optional[List[str]] = None
    # AA-557 J.23 — extended to the same full field set the Admin-only `/admin/brands` CRUD
    # (BrandCreateRequest above) already writes, so the tenant portal's own Brand Identity page
    # can carry the same structure Admin's `/admin/brand` page does, not just 3 of its fields.
    brand_name:        Optional[str] = None
    brand_type:        Optional[str] = None
    core_idea:         Optional[str] = None
    customer_segment:  Optional[str] = None
    customer_mindset:  Optional[str] = None
    tone_of_voice:     Optional[List[str]] = None
    writing_style:     Optional[str] = None
    good_examples:     Optional[str] = None
    target_markets:    Optional[List[str]] = None


class CountryUpdateRequest(BaseModel):
    country: str


class ForceFailRequest(BaseModel):
    reason: str


# ── POST /admin/run-tour ──────────────────────────────────────────────────────

_BRAND_RULE_COLS = (
    "id, brand_name, system_prompt, style_guide, forbidden_words, version, "
    "core_idea, customer_segment, customer_mindset, voice_examples, good_examples, brand_type"
)


async def _resolve_brand_rule(conn, tenant_uuid, brand_identity_id, brand_name):
    """Select exactly one brand rule. Priority: explicit id → named active brand → 'default'.

    AA-198: replaces the old cross-brand `is_active ORDER BY version DESC` fallback that
    always returned the highest-version brand (Terra Family v2) regardless of intent.
    """
    if brand_identity_id:
        return await conn.fetchrow(
            f"SELECT {_BRAND_RULE_COLS} FROM shared.tenant_brand_rules"
            " WHERE id = $1::uuid AND tenant_id = $2::uuid",
            brand_identity_id, tenant_uuid,
        )
    if brand_name:
        return await conn.fetchrow(
            f"SELECT {_BRAND_RULE_COLS} FROM shared.tenant_brand_rules"
            " WHERE tenant_id = $1::uuid AND brand_name = $2 AND is_active = true"
            " ORDER BY version DESC LIMIT 1",
            tenant_uuid, brand_name,
        )
    return await conn.fetchrow(
        f"SELECT {_BRAND_RULE_COLS} FROM shared.tenant_brand_rules"
        " WHERE tenant_id = $1::uuid AND brand_name = 'default' AND is_active = true"
        " LIMIT 1",
        tenant_uuid,
    )


async def _execute_run_tour(
    req: TourRunRequest,
    job_id: str | None = None,
    *,
    batch_text: str | None = None,       # AA-606: Bedrock Batch attempt-1 writer text (skip live writer)
    batch_model_used: str | None = None,
    batch_account: str | None = None,
) -> dict:
    """Core tour rewrite — called by HTTP endpoint and background retry task.

    AA-606: when batch_text is set, the writer's attempt-1 came from a Bedrock Batch job, so
    _rewrite_tour seeds it (build_graph_from_generated) instead of calling the writer live. All
    brand-resolve / SEO / persist / export / review-queue logic below is reused unchanged.

    AA-250 B2: job_id, when set (async job path only — _run_tour_job), builds an on_stage
    callback that persists each completed LangGraph node name to
    shared.pipeline_jobs.current_stage via jobs_repo.update_stage(), so the S1 async job poll
    can drive a stage-progress bar. None for the sync /admin/run-tour endpoint (no job row to
    update) — on_stage stays None and _rewrite_tour behaves exactly as before.
    """
    on_stage = None
    if job_id is not None:
        from .jobs_repo import update_stage as _update_stage

        async def _on_stage(node_name: str) -> None:
            await _update_stage(job_id, node_name)

        on_stage = _on_stage

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
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

        lessons_text = ""
        try:
            lesson_rows = await conn.fetch("""
                SELECT what_to_do, field, pattern
                FROM shared.pipeline_lessons
                WHERE stage IN ('S1_rewrite', 'S1_editor')
                  AND is_active = true
                ORDER BY id
            """)
            if lesson_rows:
                lessons_lines = []
                for lr in lesson_rows:
                    field_prefix = f"[{lr['field']}] " if lr['field'] else ""
                    lessons_lines.append(f"- {field_prefix}{lr['what_to_do']}")
                lessons_text = (
                    "\n\n## LESSONS FROM PREVIOUS BATCHES\n"
                    "Apply these rules strictly — they come from real batch failures:\n"
                    + "\n".join(lessons_lines)
                )
        except Exception as _le:
            logger.warning("lessons_load_failed", error=str(_le))

        if lessons_text:
            brand_rules["system_prompt"] = (brand_rules.get("system_prompt") or "") + lessons_text

        seo_data: dict = {}
        dataforseo_used: bool = False
        _SEO_MODE_MAP = {"standard": "dataforseo", "aggressive": "dataforseo", "minimal": "disabled"}
        effective_seo_mode = _SEO_MODE_MAP.get(req.seo_mode, req.seo_mode)
        try:
            from services.seo_intelligence.handler import process_seo
            from services.seo_intelligence.seed_builder import build_seed
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
                seo_result = await process_seo(
                    tour_id=req.tour_id, destination=seed, seed=seed,
                    tenant_id=tenant_uuid, seo_mode=effective_seo_mode,
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

        result = await _rewrite_tour(
            tour, idx=0, total=1,
            brand_rules=brand_rules,
            seo=seo_data,
            model_tier=req.model_tier,
            subtitle_focus=req.subtitle_focus,
            seo_mode=effective_seo_mode,
            on_stage=on_stage,
            batch_text=batch_text,               # AA-606
            batch_model_used=batch_model_used,   # AA-606
            batch_account=batch_account,         # AA-606
        )

        _UPGRADE_THRESHOLD = float(os.environ.get("AUTO_UPGRADE_THRESHOLD", "8.5"))
        _score = result.get("quality_score", 0.0)
        _model = result.get("model_used", "")
        _auto_upgraded = False  # AA-237: did the opt-in sonnet re-run replace the haiku result?
        if (
            req.allow_auto_upgrade  # AA-237: gate the silent sonnet re-run behind explicit opt-in
            and result.get("status") == "success"
            and 0 < _score < _UPGRADE_THRESHOLD
            and "haiku" in _model.lower()
        ):
            _upgraded = await _rewrite_tour(
                tour, idx=0, total=1,
                brand_rules=brand_rules,
                seo=seo_data,
                model_tier="sonnet",
                subtitle_focus=req.subtitle_focus,
                seo_mode=effective_seo_mode,
                on_stage=on_stage,
            )
            if _upgraded.get("quality_score", 0.0) > _score:
                result = _upgraded
                _auto_upgraded = True

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
            metadata_val = json.dumps(
                _build_generated_metadata(
                    result,
                    brand_rule_id=brand_rule_id,
                    brand_name=brand_name_val,
                    seo_mode=req.seo_mode,
                    model_used=result.get("model_used", ""),
                    llm_cost_usd=float(result.get("cost_usd") or 0.0),
                    dataforseo_used=dataforseo_used,
                    batch_id=getattr(req, "batch_id", None),
                ),
                default=str,
            )
            _itin = generated.get("itineraries", "")
            if not isinstance(_itin, str):
                _itin = str(_itin)
            # AA-204: sentence-aware backstop trim; if a whole-sentence cut would drop the meta
            # below the 140 floor, fall back to the longer word-boundary cut (repair is the
            # primary fix; this is only the final DB-write net).
            _m = generated.get("seo_meta") or ""
            _m_sent = _trim_to_word_boundary(_m, 155, sentence=True)
            _m_final = _m_sent if len(_m_sent) >= 140 else _trim_to_word_boundary(_m, 155)
            try:
                version_id = await conn.fetchval("""
                    INSERT INTO silver_aa_internal.generated_content (
                        tour_id, tenant_id, version_num,
                        aa_name, aa_subtitle, aa_summary,
                        aa_description, aa_highlights, aa_itineraries,
                        seo_title, seo_meta, seo_keywords_used,
                        model_editorial, status, og_tags, metadata,
                        brand_rules_version, requested_tier, fallback_used, satellite_used,
                        satellite_account
                    ) VALUES (
                        $1::uuid, $2::uuid,
                        COALESCE((SELECT MAX(version_num) + 1
                        FROM silver_aa_internal.generated_content
                        WHERE tour_id = $1::uuid), 1),
                        $3, $4, $5, $6, $7::jsonb, $8,
                        $9, $10, $11::jsonb, $12, $13::content_status_enum, $14::jsonb, $15::jsonb,
                        $16, $17, $18, $19, $20
                    ) RETURNING id
                """,
                    req.tour_id, tenant_uuid,
                    (generated.get("name") or "")[:500],
                    (generated.get("subtitle") or "")[:500],
                    generated.get("summary"),
                    generated.get("description", ""),
                    json.dumps(generated.get("highlights", [])),
                    _itin,
                    _trim_to_word_boundary(generated.get("seo_title"), 60),
                    _m_final,
                    json.dumps(generated.get("seo_keywords_used", [])),
                    result.get("model_used", ""),
                    status,
                    og_tags_val,
                    metadata_val,
                    brand_rule_version,
                    req.model_tier,                          # AA-224: requested_tier
                    bool(result.get("fallback_used", False)),  # AA-224: fallback_used
                    # AA-397: satellite_used (bool) giữ nguyên cho backward-compat, derive từ
                    # satellite_account is not None thay vì đọc field bool cũ (đã xoá khỏi LLMResponse).
                    result.get("satellite_account") is not None,
                    result.get("satellite_account"),         # AA-397: satellite_account (text, 'acc1'/'acc3'/None)
                )
                if version_id:
                    logger.info("version_inserted", tour_id=req.tour_id, version_id=str(version_id))
                else:
                    logger.error("version_insert_returned_null", tour_id=req.tour_id)
            except Exception as _ins_err:
                logger.error("version_insert_failed", tour_id=req.tour_id, error=str(_ins_err))
                raise

        if version_id and result.get("quality_score") is not None:
            _sub = result.get("sub_scores", {})
            _fc  = result.get("failure_codes", [])
            await conn.execute("""
                INSERT INTO silver_aa_internal.quality_scores (
                    generated_content_id, tour_id, tenant_id,
                    score_overall, score_brand, score_seo,
                    score_structure, score_quality,
                    passed_count, failed_count, failure_codes,
                    validator_fn_version, evaluated_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid,
                    $4, $5, $6, $7, $8, $9, $10, $11::jsonb, 'v2', NOW()
                ) ON CONFLICT DO NOTHING
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
        if version_id and result.get("brand_audit_status"):
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

        # BƯỚC 7: Persist fix_pass results to generated_content
        if version_id and result.get("fix_pass_applied"):
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

        # AA-211: audit-aware publish gate (see _is_publishable).
        status = "approved" if _is_publishable(result) else "pending"
        if version_id and status == "approved":
            from services.export.handler import process_export
            try:
                await process_export(str(version_id))
            except Exception as _exp_err:
                logger.error("export_failed", tour_id=req.tour_id, error=str(_exp_err))
        elif version_id and status == "pending":
            # AA-212: route blocked / low-quality tour to HITL review_queue (idempotent enqueue).
            try:
                await _enqueue_review(conn, req.tour_id, version_id, result)
            except Exception as _rq_err:
                logger.warning("review_queue_enqueue_failed", tour_id=req.tour_id, error=str(_rq_err))

        cost_usd    = float(result.get("cost_usd") or 0.0)
        tokens_in   = int(result.get("input_tokens") or 0)
        tokens_out  = int(result.get("output_tokens") or 0)
        tour_passed = float(result.get("quality_score") or 0.0) >= 7.0
        model_name  = result.get("model_used") or None
        if isinstance(model_name, str) and not model_name:
            model_name = None
        actual_provider = "openai" if model_name and "gpt" in model_name.lower() else "bedrock"
        if (cost_usd > 0 or tokens_in > 0) and _is_uuid(req.batch_id):
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
        elif (cost_usd > 0 or tokens_in > 0) and not _is_uuid(req.batch_id):
            # AA-210: non-UUID batch_id (e.g. ad-hoc verification run) — skip accounting
            # UPDATE so the uuid cast cannot crash the post-export path. No matching
            # pipeline_runs row exists for such runs anyway.
            logger.info("pipeline_runs_accounting_skipped",
                        batch_id=req.batch_id, reason="batch_id is not a valid uuid")

        return {
            "tour_id":            req.tour_id,
            "batch_id":           req.batch_id,
            "version_id":         str(version_id) if version_id else None,
            "status":             result.get("status"),
            "quality_score":      result.get("quality_score"),
            "generated":          result.get("generated"),
            "failure_codes":      result.get("failure_codes", []),   # AA-204: surface validate codes
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


async def _run_tour_safe(tour_req: TourRunRequest, job_id: str | None = None) -> dict | None:
    async with _pipeline_semaphore:
        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(2 ** attempt)
            try:
                # AA-223: surface the executor result so _run_tour_job can read
                # version_id. Final-fail path below still swallows and returns None.
                return await _execute_run_tour(tour_req, job_id=job_id)
            except Exception as exc:
                last_exc = exc
                logger.warning("run_tour_attempt_failed", tour_id=tour_req.tour_id,
                               attempt=attempt + 1, error=str(exc))
        logger.error("background_run_tour_failed", tour_id=tour_req.tour_id,
                     batch_id=tour_req.batch_id, error=str(last_exc))
        if not _is_uuid(tour_req.batch_id):
            # AA-210: non-UUID batch_id has no pipeline_runs row to mark failed; skip the
            # uuid-cast UPDATE rather than crash the failure handler.
            logger.info("pipeline_runs_fail_mark_skipped",
                        batch_id=tour_req.batch_id, reason="batch_id is not a valid uuid")
            return
        try:
            conn = await asyncpg.connect(os.environ["DATABASE_URL"])
            await conn.execute(
                "UPDATE shared.pipeline_runs SET status='failed', error_message=$2 "
                "WHERE batch_id=$1::uuid AND status='ingesting'",
                tour_req.batch_id, str(last_exc)[:1000],
            )
            await conn.close()
        except Exception as db_exc:
            logger.error("failed_to_mark_pipeline_failed", error=str(db_exc))


async def _run_tour_job(job_id: str, tour_req: TourRunRequest) -> None:
    """AA-223: pipeline_jobs lifecycle wrapper around the existing executor.

    _run_tour_safe (semaphore + 3x retry) SWALLOWS the final failure and returns
    None — so a None result means retries were exhausted: mark the job failed.
    A returned dict means the run completed (version_id may still be None for a
    soft-fail, which we surface as succeeded + result_version_id NULL).
    This is a SEPARATE tier from _run_tour_safe's own pipeline_runs fail-mark.
    """
    from .jobs_repo import mark_failed, mark_interrupted, mark_running, mark_succeeded

    try:
        await mark_running(job_id)
        result = await _run_tour_safe(tour_req, job_id=job_id)
        if result is None:
            await mark_failed(job_id, "run_tour failed after retries (see logs)")
        else:
            await mark_succeeded(job_id, result.get("version_id"), None)
    except asyncio.CancelledError:
        # AA-295: rolling-deploy SIGTERM (or any explicit task.cancel()) lands here — mark the
        # job interrupted immediately instead of leaving it 'running' until the next boot's
        # sweep_interrupted() (which only fires once per boot and only once heartbeat_at is
        # already >5min stale). Re-raise: never swallow CancelledError, asyncio needs it to
        # propagate for correct task/loop cancellation semantics.
        logger.warning("run_tour_job_cancelled", job_id=job_id, tour_id=tour_req.tour_id)
        await mark_interrupted(job_id, "cancelled (deploy/shutdown)")
        raise
    except Exception as e:
        await mark_failed(job_id, repr(e))


# ── AA-234 Phần A: re-validate a human-edited generated_content version ────────
async def _revalidate_tour(content_id: str) -> dict:
    """Re-validate a single human-edited generated_content version in place.

    Reads the edited content + its tour + seo_context (by tour_id, same keywords — NO
    DFS refetch) + brand rule, runs build_revalidation_graph, then overwrites the
    version's quality_scores rows and sets generated_content.revalidate_passed.
    """
    from services.content_generation.graph import build_revalidation_graph

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        gc = await conn.fetchrow("""
            SELECT gc.id, gc.tour_id, gc.tenant_id, gc.version_num,
                   gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description,
                   gc.aa_highlights, gc.aa_itineraries, gc.mobile_card_text,
                   gc.seo_title, gc.seo_meta, gc.seo_keywords_used, gc.og_tags,
                   gc.brand_rules_version, gc.metadata,
                   rt.src_name, rt.country, rt.duration
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
        tour = {"name": gc["src_name"], "country": gc["country"], "duration": gc["duration"]}

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
        }

        graph = build_revalidation_graph()

        def _run():
            import asyncio as _a
            loop = _a.new_event_loop()
            _a.set_event_loop(loop)
            try:
                return graph.invoke(initial_state)
            finally:
                loop.close()

        result = await asyncio.get_event_loop().run_in_executor(None, _run)

        passed = bool(result.get("revalidate_passed", False))
        q_score = float(result.get("quality_score", 0.0))
        _sub = result.get("sub_scores", {})
        _fc = result.get("failure_codes", [])

        await conn.execute(
            "DELETE FROM silver_aa_internal.quality_scores WHERE generated_content_id = $1::uuid",
            content_id)
        await conn.execute("""
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
        await conn.execute(
            "UPDATE silver_aa_internal.generated_content SET revalidate_passed = $1 WHERE id = $2::uuid",
            passed, content_id)

        logger.info("revalidate_tour_done", content_id=content_id,
                    revalidate_passed=passed, quality_score=q_score,
                    brand_audit_status=result.get("brand_audit_status", ""))
        return {
            "content_id": content_id, "revalidate_passed": passed,
            "quality_score": q_score, "brand_audit_status": result.get("brand_audit_status", ""),
            "failure_codes": _fc,
        }
    finally:
        await conn.close()


async def _revalidate_job(job_id: str, content_id: str) -> None:
    """AA-234: pipeline_jobs lifecycle wrapper around _revalidate_tour."""
    from .jobs_repo import mark_failed, mark_interrupted, mark_running, mark_succeeded
    try:
        await mark_running(job_id)
        result = await _revalidate_tour(content_id)
        await mark_succeeded(job_id, content_id, None)
        logger.info("revalidate_job_done", job_id=job_id,
                    revalidate_passed=result["revalidate_passed"])
    except asyncio.CancelledError:
        # AA-295: same bug/fix as _run_tour_job — see comment there.
        logger.warning("revalidate_job_cancelled", job_id=job_id, content_id=content_id)
        await mark_interrupted(job_id, "cancelled (deploy/shutdown)")
        raise
    except Exception as e:
        await mark_failed(job_id, repr(e))



@router.post("/run-tour")
async def run_tour(req: TourRunRequest, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    return await _execute_run_tour(req)


@router.post("/run-tour-async")
async def run_tour_async(req: TourRunRequest, x_admin_secret: str = Header(None)):
    """AA-223: 202 + job poll. Returns immediately; _run_tour_job runs the existing
    executor in the background and tracks lifecycle in shared.pipeline_jobs."""
    verify_admin_secret(x_admin_secret)
    from fastapi.responses import JSONResponse

    from .jobs_repo import create_job, find_active_duplicate

    payload = req.model_dump()

    dup = await find_active_duplicate(payload)
    if dup:
        return JSONResponse(status_code=202, content={
            "job_id": dup, "status": "running", "dedup": True,
            "poll_url": f"/admin/jobs/{dup}",
        })

    job_id = await create_job(payload, payload.get("tenant_id"))
    task = asyncio.create_task(_run_tour_job(job_id, req))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "queued",
        "poll_url": f"/admin/jobs/{job_id}",
    })


@router.get("/jobs/{job_id}")
async def get_run_tour_job(job_id: str, x_admin_secret: str = Header(None)):
    """AA-223: poll a run-tour job's lifecycle row."""
    verify_admin_secret(x_admin_secret)
    from .jobs_repo import get_job

    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# ── AA-606: S1 rewrite via Bedrock Batch Inference ─────────────────────────────

class S1BatchSubmitRequest(BaseModel):
    """Submit an S1-rewrite Bedrock Batch job for a set of tours (writer attempt-1 only)."""
    tour_ids: List[str]
    batch_id: str                              # UUID → pipeline_runs accounting (like sync ingest)
    account: str = "acc3"                      # satellite account owning Batch perms (AA-399)
    seo_mode: str = "standard"
    model_tier: Optional[str] = "haiku"
    enforce_min: bool = True                   # reject sub-minimum batches (Bedrock rejects them)


async def _s1_batch_ingest_task(job_id: str, submit: dict, batch_id: str, seo_mode: str,
                                model_tier: Optional[str]) -> None:
    """Background: poll the batch job to completion, then ingest each tour through the existing
    persist path. Tracks lifecycle in shared.pipeline_jobs (reusing jobs_repo) so the admin UI
    can poll GET /admin/s1-batch/{job_id}. Runs asyncio.to_thread for the blocking poll so it
    never blocks the event loop."""
    from .jobs_repo import mark_running, mark_succeeded, mark_failed
    from services.content_generation.s1_batch import ingest_s1_batch

    try:
        await mark_running(job_id)
        summary = await ingest_s1_batch(
            job_arn=submit["job_arn"],
            output_uri=submit["output_uri"],
            account=submit["account"],
            tour_ids=submit["tour_ids"],
            batch_id=batch_id,
            seo_mode=seo_mode,
            model_tier=model_tier,
            poll=True,
        )
        # No single result_version_id for a batch — record the run summary in error col (free text,
        # reused; schema has no summary column). status=succeeded means the batch ingest finished.
        await mark_succeeded(job_id, None, None)
        logger.info("s1_batch_ingest_done", job_id=job_id,
                    ingested=summary.get("ingested"), gate_fail_retry=summary.get("gate_fail_retry"),
                    batch_write_failed=summary.get("batch_write_failed"))
    except asyncio.CancelledError:
        from .jobs_repo import mark_interrupted
        await mark_interrupted(job_id, "cancelled (deploy/shutdown)")
        raise
    except Exception as e:
        await mark_failed(job_id, repr(e)[:1000])
        logger.error("s1_batch_ingest_failed", job_id=job_id, error=str(e))


@router.post("/s1-batch/submit")
async def submit_s1_batch_endpoint(req: S1BatchSubmitRequest, x_admin_secret: str = Header(None)):
    """AA-606: materialize + submit an S1-rewrite Bedrock Batch job, then kick off a background
    poll+ingest task. Returns 202 with job_id (poll GET /admin/s1-batch/{job_id}) and the batch ARN.

    The writer attempt-1 runs on Bedrock Batch (Haiku); every tour is then validated/judged/gated/
    persisted by the existing _execute_run_tour path (gate misses fall to the on-demand retry loop)."""
    verify_admin_secret(x_admin_secret)
    from fastapi.responses import JSONResponse
    from .jobs_repo import create_job
    from services.content_generation.s1_batch import submit_s1_batch
    from shared.llm_client.bedrock_batch import BatchUnavailable

    try:
        submit = await submit_s1_batch(
            req.tour_ids, account=req.account, enforce_min=req.enforce_min,
        )
    except BatchUnavailable as e:
        raise HTTPException(status_code=400, detail=f"batch submit failed: {e}")

    job_id = await create_job(
        {"job_type": "s1_batch", "batch_id": req.batch_id, "job_arn": submit["job_arn"],
         "record_count": submit["record_count"], "account": submit["account"]},
        _MASTER_TENANT_ID,
    )
    task = asyncio.create_task(
        _s1_batch_ingest_task(job_id, submit, req.batch_id, req.seo_mode, req.model_tier)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "submitted", "job_arn": submit["job_arn"],
        "record_count": submit["record_count"], "account": submit["account"],
        "poll_url": f"/admin/s1-batch/{job_id}",
    })


@router.get("/s1-batch/{job_id}")
async def get_s1_batch_job(job_id: str, x_admin_secret: str = Header(None)):
    """AA-606: poll an S1 batch job's lifecycle row (queued→running→succeeded|failed|interrupted)."""
    verify_admin_secret(x_admin_secret)
    from .jobs_repo import get_job

    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# ── GET /admin/brand-rules ────────────────────────────────────────────────────

@router.get("/brand-rules")
async def list_brand_rules(
    tenant_id: str = Query(...),
    x_admin_secret: str = Header(None),
):
    """List active brand rules for a tenant (id/name/version only — no prompt content)."""
    verify_admin_secret(x_admin_secret)
    TENANT_SLUG_MAP = {"aa_internal": "00000000-0000-0000-0000-000000000001"}
    tenant_uuid = TENANT_SLUG_MAP.get(tenant_id, tenant_id)
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch("""
            SELECT id, brand_name, version, is_active
            FROM shared.tenant_brand_rules
            WHERE tenant_id = $1::uuid AND is_active = true
            ORDER BY brand_name
        """, tenant_uuid)
    finally:
        await conn.close()
    return [
        {
            "id": str(r["id"]),
            "brand_name": r["brand_name"],
            "version": r["version"],
            "is_active": r["is_active"],
        }
        for r in rows
    ]


# ── POST /admin/upload-url ────────────────────────────────────────────────────

@router.post("/upload-url", response_model=UploadUrlResponse)
async def get_upload_url(body: UploadUrlRequest, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    import uuid as _uuid
    import re as _re
    tenant_id = "00000000-0000-0000-0000-000000000001"
    bucket    = os.environ.get("BRONZE_BUCKET", "aa-cis-bronze-867490540162")
    safe_filename = _re.sub(r'[^a-zA-Z0-9._-]', '_', body.filename)
    s3_key    = f"raw-inbox/{tenant_id}/{_uuid.uuid4()}_{safe_filename}"
    s3 = _boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": s3_key, "ContentType": body.content_type,
                "Metadata": {"seo-mode": body.seo_mode}},
        ExpiresIn=300,
    )
    return UploadUrlResponse(upload_url=upload_url, s3_key=s3_key, bucket=bucket)


# ── POST /admin/ingest-s3 ─────────────────────────────────────────────────────

@router.post("/ingest-s3")
async def ingest_s3(
    req: IngestS3Request,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)

    _bucket = req.bucket or os.environ.get("BRONZE_BUCKET", "aa-cis-bronze-867490540162")

    # ── dry_run=True: parse-only preview, no DB write ─────────────────────────
    if req.dry_run:
        import tempfile as _tempfile
        import hashlib as _hashlib
        from services.ingestion.excel_parser import ExcelParser as _ExcelParser
        import uuid as _uuid

        s3_client = _boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))
        with _tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            s3_client.download_fileobj(_bucket, req.s3_key, tmp)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as fh:
            file_bytes = fh.read()
        file_hash = _hashlib.sha256(file_bytes).hexdigest()

        import re as _re

        def _clean_filename(path: str) -> str:
            """Strip UUID prefix and path prefix from s3 key or filename."""
            basename = path.split("/")[-1]
            return _re.sub(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_', '', basename)

        pool = request.app.state.pool
        async with pool.acquire() as conn:
            existing_hash = await conn.fetchrow(
                "SELECT id, filename FROM silver_aa_internal.raw_sources WHERE file_hash = $1",
                file_hash,
            )
            if existing_hash and existing_hash["filename"]:
                incoming_name = _clean_filename(req.s3_key)
                stored_name   = _clean_filename(existing_hash["filename"])
                if incoming_name == stored_name:
                    return {
                        "status":  "blocked",
                        "reason":  "duplicate_file_hash",
                        "dry_run": True,
                        "message": "This file was already uploaded before — content and filename are an exact match.",
                    }

            parser = _ExcelParser(tmp_path, source_file=req.s3_key)
            records = parser.parse()

            all_names = [r["src_name"] for r in records if r.get("src_name")]
            duplicate_names: set = set()
            if all_names:
                norm_names = [n.lower().strip() for n in all_names]
                dup_rows = await conn.fetch(
                    "SELECT DISTINCT lower(trim(src_name)) AS n "
                    "FROM silver_aa_internal.raw_tours "
                    "WHERE lower(trim(src_name)) = ANY($1::text[])",
                    norm_names,
                )
                duplicate_names = {r["n"] for r in dup_rows}

            # AA-490 (follow-up AA-488 Gap 1): the real Commit path (process_file() ->
            # services/ingestion/handler.py) already drops in-file duplicates (two rows in the
            # SAME uploaded file sharing normalize_group_key(src_name, provider), neither yet
            # in the DB) via a seen_keys set -- correct behavior, but this preview branch never
            # ran that check, so the preview showed BOTH rows as "ready" and the admin only
            # discovered the drop after clicking Commit, with no explanation at preview time.
            # Mirrors handler.py's exact check order (in-file dedup BEFORE the DB-existence
            # check) so preview and commit report the identical outcome for the identical file.
            from services.ingestion.handler import normalize_group_key as _normalize_group_key

            ready_tours = []
            blocked_tours = []
            seen_keys: set = set()
            for r in records[:req.max_tours]:
                src_name = r.get("src_name") or "(no name)"
                missing = [f for f in ["src_name", "country", "duration", "price_raw"] if not r.get(f)]
                nname, nprov = _normalize_group_key(src_name, r.get("provider"))
                if (nname, nprov) in seen_keys:
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": "duplicate_in_file",
                        "message": "Another row earlier in this file has the same name + "
                                   "provider — this row will be skipped on Commit.",
                    })
                elif src_name.lower().strip() in duplicate_names:
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": "duplicate_tour", "message": "This tour already exists in the system.",
                    })
                elif missing:
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": "missing_fields", "missing_fields": missing,
                        "message": f"Missing fields: {', '.join(missing)}",
                    })
                elif not (r.get("src_itineraries") or "").strip():
                    # AA-604: a row with no itinerary body cannot be rewritten by S1
                    # (S1 get_all_tours + acp_contract.v_trip_registry require a non-empty
                    # itinerary). Surface it here at preview time instead of silently
                    # committing it as "ingested" and having it vanish between S0 and S1.
                    # Covers POI/attraction rows (museums, rentals, single activities) and
                    # source files that simply lack itinerary content.
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": "empty_itinerary",
                        "message": "No itinerary content — this looks like a point of interest "
                                   "or activity, not a multi-day tour. It cannot be rewritten and "
                                   "will be skipped on Commit.",
                    })
                else:
                    seen_keys.add((nname, nprov))
                    ready_tours.append({
                        "tour_id":         str(_uuid.uuid4()),
                        "src_name":        src_name,
                        "country":         r.get("country"),
                        "duration":        r.get("duration"),
                        "price_raw":       r.get("price_raw"),
                        "group_size":      r.get("group_size"),
                        "period":          r.get("period"),
                        "pipeline_status": "preview",
                        "ingest_at":       "",
                        "src_subtitle":    r.get("src_subtitle"),
                        "src_summary":     r.get("src_summary"),
                        "src_highlights":  r.get("src_highlights"),
                        "src_itineraries": r.get("src_itineraries"),
                        "provider":        r.get("provider"),
                        "activities":      r.get("activities"),
                        "inclusions":      r.get("inclusions"),
                        "exclusions":      r.get("exclusions"),
                        "sku":             r.get("sku"),
                        "src_description": r.get("src_description"),
                        "links":           r.get("links"),
                        "feature":         r.get("feature"),
                        "best_time_to_go": r.get("best_time_to_go"),
                    })

            sources = await conn.fetch("""
                SELECT filename, s3_path, row_count, parsed_at, parse_errors, file_hash
                FROM silver_aa_internal.raw_sources
                WHERE tenant_id = $1::uuid
                ORDER BY parsed_at DESC LIMIT 20
            """, UUID("00000000-0000-0000-0000-000000000001"))

            return {
                "status":         "parsed",
                "dry_run":        True,
                "batch_id":       None,
                "ready_count":    len(ready_tours),
                "blocked_count":  len(blocked_tours),
                "tours":          ready_tours,
                "blocked_tours":  blocked_tours,
                "upload_history": [dict(s) for s in sources],
            }

    # ── dry_run=False: insert raw_sources + raw_tours, no pipeline trigger ────
    from services.ingestion.handler import process_file
    result = await process_file(_bucket, req.s3_key, seo_mode=req.seo_mode)
    if result.get("status") == "skipped_duplicate":
        return {"status": "duplicate", "batch_id": None, "tour_count": 0}

    batch_id     = result.get("source_id")
    tours_staged = result.get("tours_staged", 0)
    staged_ids   = result.get("staged_ids", [])

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tour_id FROM silver_aa_internal.raw_tours WHERE batch_id = $1::uuid",
            batch_id,
        )

    return {
        "status":        "done",
        "batch_id":      str(batch_id),
        "tour_count":    len(rows),
        "tours_written": len(rows),
        "tours_staged":  tours_staged,
        "staged_ids":    staged_ids,
    }


# ── GET /admin/upload-staging/{batch_id} — list staged duplicates ────────────
# NOTE: these routes use /upload-staging/ and /raw-tours/ prefixes (not /tours/)
# so there is no FastAPI greedy-matching conflict with /tours/{tour_id}/...

class DecideRequest(BaseModel):
    decision: str  # bypass | replace | update | keep_both


@router.get("/upload-staging/{batch_id}")
async def list_upload_staging(
    batch_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Return staging rows + existing tour fields for side-by-side comparison."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                us.id::text         AS staging_id,
                us.parsed_payload,
                us.matched_tour_id::text,
                us.matched_source_group_id::text,
                us.decision,
                us.created_at,
                rt.src_name, rt.src_summary, rt.country, rt.price_raw,
                rt.provider, rt.ingest_at
            FROM silver_aa_internal.upload_staging us
            LEFT JOIN silver_aa_internal.raw_tours rt
                ON rt.tour_id = us.matched_tour_id
            WHERE us.batch_id = $1::uuid
              AND us.decision = 'pending'
            ORDER BY us.created_at
        """, batch_id)

    return {
        "batch_id": batch_id,
        "items": [
            {
                "staging_id":              r["staging_id"],
                "matched_tour_id":         r["matched_tour_id"],
                "matched_source_group_id": r["matched_source_group_id"],
                "decision":                r["decision"],
                "created_at":              r["created_at"].isoformat() if r["created_at"] else None,
                "incoming": (
                    json.loads(r["parsed_payload"])
                    if isinstance(r["parsed_payload"], str)
                    else dict(r["parsed_payload"] or {})
                ),
                "existing": {
                    "src_name":  r["src_name"],
                    "src_summary": r["src_summary"],
                    "country":   r["country"],
                    "price_raw": r["price_raw"],
                    "provider":  r["provider"],
                    "ingest_at": r["ingest_at"].isoformat() if r["ingest_at"] else None,
                },
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/upload-staging/{staging_id}/decide")
async def decide_staging(
    staging_id: str,
    body: DecideRequest,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Apply decision for one staged duplicate row.

    bypass     → discard incoming; delete staging row
    replace    → incoming INSERT as active; old active → superseded; delete staging
    update     → same as replace (alias)
    keep_both  → incoming INSERT as superseded; active unchanged; delete staging
    """
    VALID = {"bypass", "replace", "update", "keep_both"}
    if body.decision not in VALID:
        raise HTTPException(status_code=400, detail=f"decision must be one of {VALID}")

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        staging_row = await conn.fetchrow("""
            SELECT id, batch_id, tenant_id, parsed_payload,
                   matched_tour_id, matched_source_group_id
            FROM silver_aa_internal.upload_staging
            WHERE id = $1::uuid AND decision = 'pending'
        """, staging_id)

        if not staging_row:
            raise HTTPException(status_code=404, detail="Staging row not found or already decided")

        tenant_id = str(staging_row["tenant_id"])
        matched_tour_id = str(staging_row["matched_tour_id"]) if staging_row["matched_tour_id"] else None
        _msg = staging_row["matched_source_group_id"]
        matched_source_group_id = str(_msg) if _msg else None
        payload = (
            json.loads(staging_row["parsed_payload"])
            if isinstance(staging_row["parsed_payload"], str)
            else dict(staging_row["parsed_payload"] or {})
        )

        async with conn.transaction():
            if body.decision == "bypass":
                await conn.execute(
                    "DELETE FROM silver_aa_internal.upload_staging WHERE id = $1::uuid",
                    staging_id,
                )
                return {"staging_id": staging_id, "decision": "bypass", "committed": False}

            if body.decision in ("replace", "update"):
                if matched_tour_id:
                    await conn.execute("""
                        UPDATE silver_aa_internal.raw_tours
                        SET source_status = 'superseded'
                        WHERE source_group_id = $1::uuid
                          AND source_status = 'active'
                    """, matched_source_group_id)
                next_version = await conn.fetchval("""
                    SELECT COALESCE(MAX(source_version), 0) + 1
                    FROM silver_aa_internal.raw_tours
                    WHERE source_group_id = $1::uuid
                """, matched_source_group_id) if matched_source_group_id else 1
                new_group_id = matched_source_group_id or str(__import__("uuid").uuid4())
                new_status = "active"

            elif body.decision == "keep_both":
                next_version = await conn.fetchval("""
                    SELECT COALESCE(MAX(source_version), 0) + 1
                    FROM silver_aa_internal.raw_tours
                    WHERE source_group_id = $1::uuid
                """, matched_source_group_id) if matched_source_group_id else 1
                new_group_id = matched_source_group_id or str(__import__("uuid").uuid4())
                new_status = "superseded"

            # Commit parsed_payload → INSERT raw_tours
            import uuid as _uuid_mod
            new_tour_id = await conn.fetchval("""
                INSERT INTO silver_aa_internal.raw_tours (
                    tenant_id, batch_id, source_id,
                    tour_id_external, sku, provider,
                    src_name, src_subtitle, src_summary, src_description,
                    src_highlights, src_itineraries,
                    country, duration, group_size, period,
                    price_raw, inclusions, exclusions, links,
                    activities, feature, best_time_to_go,
                    pipeline_status,
                    source_group_id, source_version, source_status
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid,
                    $4, $5, $6,
                    $7, $8, $9, $10,
                    $11::jsonb, $12,
                    $13, $14, $15, $16,
                    $17, $18, $19, $20::jsonb,
                    $21::jsonb, $22, $23,
                    'ingested',
                    $24::uuid, $25, $26
                )
                RETURNING tour_id::text
            """,
                tenant_id,
                str(staging_row["batch_id"]),
                payload.get("source_id"),
                payload.get("tour_id_external"),
                payload.get("sku"),
                payload.get("provider"),
                payload.get("src_name"),
                payload.get("src_subtitle"),
                payload.get("src_summary"),
                payload.get("src_description"),
                json.dumps(payload.get("src_highlights") or []),
                payload.get("src_itineraries"),
                payload.get("country"),
                payload.get("duration"),
                payload.get("group_size"),
                payload.get("period"),
                payload.get("price_raw"),
                payload.get("inclusions"),
                payload.get("exclusions"),
                json.dumps(payload.get("links") or []),
                json.dumps(payload.get("activities") or []),
                payload.get("feature"),
                payload.get("best_time_to_go"),
                new_group_id,
                next_version,
                new_status,
            )

            await conn.execute(
                "DELETE FROM silver_aa_internal.upload_staging WHERE id = $1::uuid",
                staging_id,
            )

    logger.info("staging_decided", staging_id=staging_id, decision=body.decision,
                new_tour_id=new_tour_id)
    return {
        "staging_id": staging_id,
        "decision":   body.decision,
        "committed":  True,
        "new_tour_id": new_tour_id,
    }


@router.post("/raw-tours/{tour_id}/set-active")
async def set_tour_active(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Set source_status='active' for this tour; supersede all others in same group."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        group_id = await conn.fetchval(
            "SELECT source_group_id FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid",
            tour_id,
        )
        if not group_id:
            raise HTTPException(status_code=404, detail="Tour not found")

        async with conn.transaction():
            await conn.execute("""
                UPDATE silver_aa_internal.raw_tours
                SET source_status = 'superseded'
                WHERE source_group_id = $1 AND source_status = 'active'
                  AND tour_id != $2::uuid
            """, group_id, tour_id)
            await conn.execute("""
                UPDATE silver_aa_internal.raw_tours
                SET source_status = 'active'
                WHERE tour_id = $1::uuid
            """, tour_id)

    logger.info("tour_set_active", tour_id=tour_id, group_id=str(group_id))
    return {"tour_id": tour_id, "source_status": "active"}


@router.delete("/raw-tours/{tour_id}")
async def soft_delete_raw_tour(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
    x_deleted_by: Optional[str] = Header(None),
):
    """Soft-delete raw_tours row (source_status='trashed').
    Returns 409 if tour has an active generated_content FK in published_tours.
    """
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        blocked = await conn.fetchval("""
            SELECT COUNT(*) FROM gold_aa_internal.published_tours
            WHERE tour_id = $1::uuid
        """, tour_id)
        if blocked:
            raise HTTPException(
                status_code=409,
                detail="Tour has published content — cannot delete. Unpublish first.",
            )
        updated = await conn.fetchval("""
            UPDATE silver_aa_internal.raw_tours
            SET source_status = 'trashed',
                deleted_at    = NOW(),
                deleted_by    = $2
            WHERE tour_id = $1::uuid
              AND source_status != 'trashed'
            RETURNING tour_id::text
        """, tour_id, x_deleted_by or "admin")

    if not updated:
        raise HTTPException(status_code=404, detail="Tour not found or already trashed")

    logger.info("tour_soft_deleted", tour_id=tour_id, deleted_by=x_deleted_by)
    return {"tour_id": tour_id, "source_status": "trashed"}


# ── GET /admin/upload-history ─────────────────────────────────────────────────

def _count_parse_errors(parse_errors) -> int:
    if not parse_errors:
        return 0
    if isinstance(parse_errors, list):
        return len(parse_errors)
    if isinstance(parse_errors, dict):
        return sum(len(v) if isinstance(v, list) else 1 for v in parse_errors.values())
    try:
        import json as _j
        parsed = _j.loads(parse_errors)
        return len(parsed) if isinstance(parsed, list) else 0
    except Exception:
        return 0


@router.get("/upload-history")
async def get_upload_history(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # AA-344: LEFT JOIN shared.pipeline_runs on batch_id to surface the real
        # landed/dropped counts (migration 091, AA-343 Part C) alongside row_count (the
        # PARSED count only — the number that misleadingly read "30" while only 16 rows
        # actually landed, AA-343 P1.3). LEFT JOIN (not JOIN): any source ingested before
        # migration 091, or whose batch_id has no matching pipeline_runs row, must still
        # show up in Upload History with rows_landed/rows_dropped simply NULL — degrade
        # gracefully, per the issue's own proposal, not drop the row.
        sources = await conn.fetch("""
            SELECT rs.id::text, rs.filename, rs.file_size_kb, rs.row_count, rs.parsed_at,
                   rs.parse_errors, rs.batch_id::text,
                   pr.ingest_details->>'rows_landed'  AS rows_landed,
                   pr.ingest_details->>'rows_dropped' AS rows_dropped
            FROM silver_aa_internal.raw_sources rs
            LEFT JOIN shared.pipeline_runs pr ON pr.batch_id = rs.batch_id
            WHERE rs.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
            ORDER BY rs.parsed_at DESC LIMIT 20
        """)
    return {
        "sources": [
            {
                "id":                s["id"],
                "filename":          s["filename"],
                "file_size_kb":      float(s["file_size_kb"]) if s["file_size_kb"] else None,
                "row_count":         s["row_count"],
                "rows_landed":       int(s["rows_landed"]) if s["rows_landed"] is not None else None,
                "rows_dropped":      int(s["rows_dropped"]) if s["rows_dropped"] is not None else None,
                "parsed_at":         str(s["parsed_at"]) if s["parsed_at"] else None,
                "parse_error_count": _count_parse_errors(s["parse_errors"]),
                "batch_id":          s["batch_id"],
            }
            for s in sources
        ],
    }


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
              AND (t.source_status IS NULL OR t.source_status::text != 'trashed')
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
                MAX(gc.created_at)   AS last_rewritten_at
            FROM silver_aa_internal.raw_tours rt
            LEFT JOIN silver_aa_internal.raw_sources rs ON rs.id = rt.source_id
            LEFT JOIN silver_aa_internal.generated_content gc ON gc.tour_id = rt.tour_id
            WHERE (rt.source_status IS NULL OR rt.source_status::text <> 'trashed'::text)
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


# ── Review queue (admin alias — no JWT required) ─────────────────────────────

def _derive_field_failures(gc: dict, codes: list) -> list:
    """AA-240: re-derive per-field failure reasons on the CURRENT gc content.

    `codes` = historical failure_codes + brand_audit_codes from quality_scores (snapshot at
    last validate). We re-measure the live field values so a code whose field a reviewer has
    since fixed is NOT surfaced (the UI must mark only fields that still fail right now).
    Multi-field codes (FORBIDDEN_WORD, MISSING_FIELD) are resolved by scanning live content.
    Import is lazy (mirrors build_revalidation_graph at call sites) to avoid pulling the
    LangGraph/boto graph module into router import time.
    """
    import json as _json
    from services.content_generation.graph import _CODE_FIELD_MAP, _VALIDATE_FORBIDDEN
    from services.content_generation.seo_meta_utils import (
        SEO_META_MIN, SEO_META_MAX, SEO_META_FORBIDDEN, meta_complete_sentence,
    )

    seen = set(codes or [])
    meta = (gc.get("seo_meta") or "")
    title = (gc.get("seo_title") or "")
    meta_norm = meta.lower().replace("-", " ")  # AA-238: catch hyphen variants

    hl = gc.get("aa_highlights")
    if isinstance(hl, str):
        try:
            hl = _json.loads(hl)
        except Exception:
            hl = []
    hl = hl if isinstance(hl, list) else []

    # gc column -> required (validate uses name/subtitle/summary/highlights/itineraries/seo_*)
    _required_cols = [
        "aa_name", "aa_subtitle", "aa_summary",
        "aa_highlights", "aa_itineraries", "seo_title", "seo_meta",
    ]

    out = []

    def add(field, code, reason):
        out.append({"field": field, "code": code, "reason": reason})

    # length / sentence — re-measured dynamically
    if "META_TOO_SHORT" in seen and meta.strip() and len(meta) < SEO_META_MIN:
        add("seo_meta", "META_TOO_SHORT", f"meta {len(meta)}<{SEO_META_MIN}")
    if "SEO_META_TOO_LONG" in seen and len(meta) > SEO_META_MAX:
        add("seo_meta", "SEO_META_TOO_LONG", f"meta {len(meta)}>{SEO_META_MAX}")
    if "SEO_TITLE_TOO_LONG" in seen and len(title) > 60:
        add("seo_title", "SEO_TITLE_TOO_LONG", f"title {len(title)}>60")
    if "META_INCOMPLETE_SENTENCE" in seen and meta.strip() and not meta_complete_sentence(meta):
        add("seo_meta", "META_INCOMPLETE_SENTENCE", "meta does not end in a complete sentence")
    if "BRAND_SEO_META_VIOLATION" in seen:
        hit = next((t for t in SEO_META_FORBIDDEN if t in meta_norm), None)
        if hit:
            add("seo_meta", "BRAND_SEO_META_VIOLATION", f"meta contains an off-brand word: '{hit}'")

    # highlights structural
    if "HIGHLIGHTS_NOT_LIST" in seen and not isinstance(gc.get("aa_highlights"), (list, str)):
        add("aa_highlights", "HIGHLIGHTS_NOT_LIST", "highlights is not a list")
    if "HIGHLIGHTS_TOO_FEW" in seen and len(hl) < 3:
        add("aa_highlights", "HIGHLIGHTS_TOO_FEW", f"only {len(hl)} highlight(s), need ≥ 3")

    # MISSING_FIELD — which required field is empty now
    if "MISSING_FIELD" in seen:
        for col in _required_cols:
            if not gc.get(col):
                add(col, "MISSING_FIELD", f"{col} is empty")

    # FORBIDDEN_WORD — scan live text, map to the field that contains it
    if "FORBIDDEN_WORD" in seen:
        for col in ("aa_name", "aa_subtitle", "aa_summary", "seo_title", "seo_meta"):
            txt = (gc.get(col) or "").lower()
            hit = next((w for w in _VALIDATE_FORBIDDEN if w in txt), None)
            if hit:
                add(col, "FORBIDDEN_WORD", f"contains a forbidden word: '{hit}'")

    # static-label codes (no live numeric re-measure available)
    _static = {
        "SUBTITLE_GENERIC": "subtitle generic/off-brand",
        "SUMMARY_OFF_BRAND": "summary off-brand opener",
        "HIGHLIGHTS_TOO_GENERIC": "highlight phrase too generic",
        "ITINERARY_STRUCTURE_WEAK": "itinerary missing day-structure / too short",
        "DFS_INTENT_UNDERUSED": "SEO keyword not yet present in title/meta",
    }
    for code, reason in _static.items():
        if code in seen and code in _CODE_FIELD_MAP:
            add(_CODE_FIELD_MAP[code], code, reason)

    return out


@router.get("/review-queue")
async def admin_review_queue(
    request: Request,
    x_admin_secret: str = Header(None),
    page: int = 1,
    page_size: int = 20,
    status: str = "pending",
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    tenant_id = "00000000-0000-0000-0000-000000000001"
    offset = (page - 1) * page_size
    if status == "all":
        # AA-626: hide 'dismissed' from the "all" view too, same as 'superseded' — both are
        # closed-out-without-a-quality-verdict rows the reviewer intentionally cleared.
        status_clause = "AND rq.review_status NOT IN ('superseded', 'dismissed')"
        status_params: list = []
    else:
        status_clause = "AND rq.review_status = $4"
        status_params = [status]
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT rq.id, rq.tour_id, rq.generated_content_id,
                   rq.review_status, rq.score_overall, rq.failure_summary, rq.created_at,
                   gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description,
                   gc.aa_highlights, gc.aa_itineraries, gc.mobile_card_text,
                   gc.seo_title, gc.seo_meta, gc.seo_keywords_used, gc.og_tags,
                   gc.human_edited, gc.reviewed_by, gc.edited_at, gc.revalidate_passed,
                   gc.requested_tier, gc.status, gc.version_num,
                   qs.failure_codes, qs.brand_audit_codes, qs.brand_audit_status,
                   rt.src_name, rt.country, rt.duration, rt.batch_id AS raw_tours_batch_id
            FROM silver_aa_internal.review_queue rq
            JOIN silver_aa_internal.generated_content gc ON gc.id = rq.generated_content_id
            LEFT JOIN silver_aa_internal.quality_scores qs
                   ON qs.generated_content_id = rq.generated_content_id
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = rq.tour_id
            WHERE rq.tenant_id = $1::uuid
              {status_clause}
            ORDER BY rq.created_at DESC
            LIMIT $2 OFFSET $3
        """, tenant_id, page_size, offset, *status_params)
        if status == "all":
            total_where = "AND review_status NOT IN ('superseded', 'dismissed')"
        else:
            total_where = "AND review_status = $2"
        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM silver_aa_internal.review_queue
            WHERE tenant_id = $1::uuid {total_where}
        """, tenant_id, *status_params)

    data = []
    for r in rows:
        gc = {
            "aa_name":           r["aa_name"],
            "aa_subtitle":       r["aa_subtitle"],
            "aa_summary":        r["aa_summary"],
            "aa_description":    r["aa_description"],
            "aa_highlights":     _as_list(r["aa_highlights"]),
            "aa_itineraries":    r["aa_itineraries"],
            "mobile_card_text":  r["mobile_card_text"],
            "seo_title":         r["seo_title"],
            "seo_meta":          r["seo_meta"],
            "seo_keywords_used": _as_list(r["seo_keywords_used"]),
            "og_tags":           r["og_tags"] if isinstance(r["og_tags"], dict) else {},
        }
        codes = list(_as_list(r["failure_codes"])) + list(_as_list(r["brand_audit_codes"]))
        data.append({
            "id":                 str(r["id"]),
            "tour_id":            str(r["tour_id"]),
            "generated_content_id": str(r["generated_content_id"]),
            "review_status":      r["review_status"],
            "score_overall":      float(r["score_overall"]) if r["score_overall"] is not None else None,
            "failure_summary":    r["failure_summary"],
            "created_at":         str(r["created_at"]) if r["created_at"] else None,
            "version_num":        r["version_num"],                # AA-626: show which version failed
            "brand_audit_status": r["brand_audit_status"],         # AA-626: manual_check | flagged | None
            # editable fields (match _ALLOWED_GC_FIELDS so Part C edit maps 1:1 to PATCH)
            **gc,
            # AA-234 Part A audit columns (migration 072)
            "human_edited":       bool(r["human_edited"]),
            "reviewed_by":        r["reviewed_by"],
            "edited_at":          str(r["edited_at"]) if r["edited_at"] else None,
            "revalidate_passed":  r["revalidate_passed"],  # 3-state: None/True/False
            # AA-240 per-field failure reasons re-derived on current content
            "failures":           _derive_field_failures(gc, codes),
            # AA-242 regenerate context: tier the caller originally requested (may be null),
            # and the raw-tour batch so the FE can pass it back through run-tour-async
            "requested_tier":     r["requested_tier"],
            "raw_tours_batch_id": str(r["raw_tours_batch_id"]) if r["raw_tours_batch_id"] else None,
            # gc.status = publishable gate (_is_publishable → 'approved' | 'hitl'); FE keys
            # the supersede decision on this, never on a client-side score heuristic
            "status":             r["status"],
            # raw-tour context
            "src_name":           r["src_name"],
            "country":            r["country"],
            "duration":           r["duration"],
        })

    return {
        "data": data,
        "pagination": {"page": page, "page_size": page_size, "total": total},
    }


# ── Review queue approve/reject (admin alias — AA-230: gateway authorizer blocks /v1/pipeline)
# The /v1/pipeline/* routes sit behind the API Gateway Lambda authorizer (requires Bearer JWT).
# The admin BFF sends X-Admin-Secret only, so those calls 401 at the gateway. /admin/* is exempt
# from the authorizer, so we expose approve/reject here too and reuse the v1 logic verbatim.
# tenant=None is passed because the underlying functions hardcode the master tenant internally.


@router.post("/review-queue/{review_id}/approve")
async def admin_approve_review(
    review_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    from api.routers.v1_pipeline import approve_review
    return await approve_review(review_id=review_id, request=request, tenant=None)


@router.post("/review-queue/{review_id}/reject")
async def admin_reject_review(
    review_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    from api.routers.v1_pipeline import reject_review
    return await reject_review(review_id=review_id, request=request, tenant=None)


@router.post("/review-queue/{review_id}/supersede")
async def admin_supersede_review(
    review_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """AA-242: đóng review_queue row cũ khi Regenerate tạo ra version mới publishable
    cho cùng tour. Khác 'rejected' (reviewer đánh giá xấu) — đây là tự động thay thế,
    không phải reviewer từ chối chất lượng."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE silver_aa_internal.review_queue
            SET review_status = 'superseded'::review_status_enum
            WHERE id = $1::uuid AND review_status = 'pending'
        """, review_id)
    if result == "UPDATE 0":
        raise HTTPException(status_code=409, detail="review row not pending or not found")
    return {"status": "superseded", "review_id": review_id}


@router.post("/review-queue/{review_id}/dismiss")
async def admin_dismiss_review(
    review_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """AA-626: drop a stale failed version from the review queue WITHOUT editing content or
    publishing. Used when a tour already has an approved version in the Master Content pool and
    its older HITL version(s) are just noise the reviewer does not want to fix.

    Distinct from 'rejected' (reviewer judged content bad → mark_tour_rejected) and 'superseded'
    (auto-replaced by a newer regenerated version, AA-242). Same shape as admin_supersede_review:
    a pure review_status flip, no touch to generated_content, no process_export. Option A
    (AA-626): only hides THIS row — the _enqueue_review guard is scoped to 'pending', so a future
    rerun that fails the same tour can still enqueue a fresh pending row (a real new signal)."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE silver_aa_internal.review_queue
            SET review_status = 'dismissed'::review_status_enum, reviewed_at = NOW()
            WHERE id = $1::uuid AND review_status = 'pending'
        """, review_id)
    if result == "UPDATE 0":
        raise HTTPException(status_code=409, detail="review row not pending or not found")
    return {"status": "dismissed", "review_id": review_id}


# ── Tour version endpoints ────────────────────────────────────────────────────
# NOTE: /versions/{num}/promote MUST come before /versions/{num} — FastAPI greedy matching

@router.post("/tours/{tour_id}/versions/{version_num}/promote")
async def promote_tour_version(
    tour_id: str, version_num: int,
    request: Request, x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        updated = await conn.fetchval("""
            WITH gc AS (
                SELECT gc.id, gc.aa_name, gc.aa_subtitle,
                       qs.score_overall
                FROM silver_aa_internal.generated_content gc
                LEFT JOIN silver_aa_internal.quality_scores qs
                    ON qs.generated_content_id = gc.id
                WHERE gc.tour_id = $1::uuid AND gc.version_num = $2
                LIMIT 1
            )
            UPDATE gold_aa_internal.published_tours pt
            SET generated_content_id = gc.id,
                aa_name              = gc.aa_name,
                aa_subtitle          = gc.aa_subtitle,
                quality_score        = gc.score_overall,
                published_at         = NOW()
            FROM gc
            WHERE pt.tour_id = $1::uuid
            RETURNING pt.id::text
        """, tour_id, version_num)
    if not updated:
        raise HTTPException(status_code=404, detail="Tour or version not found")
    return {"promoted": True, "published_tour_id": updated, "version_num": version_num}



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
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE silver_aa_internal.raw_tours SET country = $1 WHERE tour_id = $2::uuid RETURNING tour_id",
            body.country.strip(), tour_id,
        )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Tour {tour_id} not found")
    return {"tour_id": tour_id, "country": body.country.strip()}


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
        updated = await conn.fetchval(
            f"UPDATE silver_aa_internal.raw_tours SET {set_clause}"
            f" WHERE tour_id = ${len(fields)+1}::uuid RETURNING tour_id",
            *values, tour_id,
        )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Tour {tour_id} not found")
    return {"tour_id": tour_id, "updated": fields}


# ── PATCH /admin/tours/{tour_id}/generated/{content_id} ──────────────────────

_ALLOWED_GC_FIELDS = {
    "aa_name", "aa_subtitle", "aa_summary", "aa_description", "aa_highlights",
    "aa_itineraries", "mobile_card_text", "seo_title", "seo_meta",
    "seo_keywords_used", "og_tags",
}
_JSONB_GC_FIELDS = {"aa_highlights", "seo_keywords_used", "og_tags"}


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
        updated = await conn.fetchval(
            f"UPDATE silver_aa_internal.generated_content SET {set_clause}{audit_sql}"
            f" WHERE id = ${len(fields)+3}::uuid AND tour_id = ${len(fields)+4}::uuid RETURNING id",
            *values, reviewed_by_value, _j_gc.dumps({"fields": fields, "reviewer_handle": reviewer_handle}),
            content_id, tour_id,
        )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Content {content_id} not found")
    return {"content_id": content_id, "updated": fields, "human_edited": True,
            "revalidate_required": True}


@router.post("/tours/{tour_id}/generated/{content_id}/revalidate")
async def revalidate_generated_content(
    tour_id: str,
    content_id: str,
    x_admin_secret: str = Header(None),
):
    """AA-234: async re-validate a human-edited version (202 + job poll).

    Runs build_revalidation_graph (validate+judge+brand_audit, NO flag_fix) in the
    background via the pipeline_jobs pattern, overwrites the version's scores, and
    sets revalidate_passed. Poll GET /admin/jobs/{job_id} for the outcome.
    """
    verify_admin_secret(x_admin_secret)
    from fastapi.responses import JSONResponse
    from .jobs_repo import create_job

    job_id = await create_job(
        {"job_type": "revalidate", "content_id": content_id, "tour_id": tour_id},
        "00000000-0000-0000-0000-000000000001",
    )
    task = asyncio.create_task(_revalidate_job(job_id, content_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "queued", "poll_url": f"/admin/jobs/{job_id}",
    })


# ── GET /admin/metrics ────────────────────────────────────────────────────────

@router.get("/metrics")
async def get_pipeline_metrics(
    request: Request,
    days: int = 7,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    tenant_slug = "aa_internal"

    async with pool.acquire() as conn:
        daily = await conn.fetch("""
            SELECT
                DATE(started_at)              AS day,
                COUNT(*)                      AS runs,
                COALESCE(SUM(tours_total),0)  AS tours,
                COALESCE(SUM(tours_passed),0) AS passed,
                COALESCE(SUM(tours_hitl),0)   AS hitl,
                COALESCE(SUM(tours_failed),0) AS failed,
                COALESCE(ROUND(SUM(cost_usd)::numeric,4), 0) AS cost
            FROM shared.pipeline_runs
            WHERE started_at >= NOW() - ($1 || ' days')::interval
              AND status != 'ingesting'
            GROUP BY DATE(started_at)
            ORDER BY day ASC
        """, str(days))

        # AA-348: the 'sonnet' label below is deliberately version-NEUTRAL ('claude-sonnet', not
        # 'claude-sonnet-4-5') — llm_model here can be the acc2-native model id (really 4-5) OR
        # a satellite audit label like "satellite-sonnet-4-6" (really 4-6, the common path in
        # practice — see shared/llm_client/pricing.py's BEDROCK_SONNET comment), and this LIKE
        # '%sonnet%' match can't tell which. A hardcoded '-4-5' suffix here would silently
        # mislabel every satellite-Sonnet row (confirmed reachable: this column is written from
        # LLMResponse.model_used, which IS the real per-call label, at
        # services/content_generation/graph.py:442 -> api/routers/admin_pipeline.py's own INSERT
        # further up in this file). Haiku is unaffected (both tiers really are 4-5-20251001).
        cost_by_model = await conn.fetch("""
            SELECT
                CASE
                    WHEN COALESCE(llm_model,'') LIKE '%haiku%'  THEN 'claude-haiku-4-5'
                    WHEN COALESCE(llm_model,'') LIKE '%sonnet%' THEN 'claude-sonnet'
                    WHEN COALESCE(llm_model,'') LIKE '%gpt-4%'  THEN 'gpt-4.1'
                    ELSE 'claude-haiku-4-5'
                END                              AS model,
                COUNT(*)                         AS batches,
                COALESCE(SUM(cost_usd), 0)       AS total_cost
            FROM shared.pipeline_runs
            WHERE cost_usd > 0 AND status != 'ingesting'
            GROUP BY 1 ORDER BY total_cost DESC
        """)

        # AA-476 (STEP0, AA-438-05 bug #11): this counts generated_content ROWS (1 per tour
        # version), NOT real LLM invocations. A version can involve multiple actual calls
        # (retries, judge, brand_audit, flag_fix/repair) that aren't reliably reconstructable
        # from persisted columns — checked brand_audit_status specifically and confirmed it's
        # set to 'pass' on 3 separate no-LLM-call skip branches in brand_audit_node (missing
        # `generated`, AA-206 judge-result reuse, missing OPENAI_API_KEY — the last of which is
        # effectively 100% of rows right now, see AA-351 zero-credit finding), so it cannot be
        # used to detect "a real call happened" either. An accurate count needs new
        # instrumentation in services/content_generation/graph.py (out of scope here — dashboard-
        # only fix). Frontend labels this "Versions", not "Calls", to match reality.
        # AA-348: same version-neutral 'claude-sonnet' label as cost_by_model above, same reason
        # — gc.model_editorial can be the real satellite audit label ("satellite-sonnet-4-6"),
        # not the acc2-native 4-5 id, and this LIKE match can't distinguish the two.
        models = await conn.fetch(f"""
            SELECT
                CASE
                    WHEN COALESCE(gc.model_editorial,'') LIKE '%haiku%'  THEN 'claude-haiku-4-5'
                    WHEN COALESCE(gc.model_editorial,'') LIKE '%sonnet%' THEN 'claude-sonnet'
                    WHEN COALESCE(gc.model_editorial,'') LIKE '%gpt-4%'  THEN 'gpt-4.1'
                    ELSE 'claude-haiku-4-5'
                END                                          AS model,
                COUNT(*)                                     AS calls,
                ROUND(AVG(qs.score_overall)::numeric, 1)     AS avg_score
            FROM silver_{tenant_slug}.generated_content gc
            LEFT JOIN silver_{tenant_slug}.quality_scores qs
                ON qs.generated_content_id = gc.id
            GROUP BY 1 ORDER BY calls DESC
        """)

        avg_cost_per_run = await conn.fetchval("""
            SELECT ROUND(SUM(cost_usd) / NULLIF(COUNT(*), 0)::numeric, 6)
            FROM shared.pipeline_runs
            WHERE cost_usd > 0 AND status != 'ingesting'
        """)

        published_count = await conn.fetchval("""
            SELECT COUNT(*) FROM gold_aa_internal.published_tours
            WHERE tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND master_status <> 'trashed'
        """)

        tenant_rewrite_count = await conn.fetchval(
            "SELECT COUNT(*) FROM gold_aa_internal.tenant_tour_versions"
        )

        tenant_breakdown = await conn.fetch("""
            SELECT t.slug, t.plan_tier, COUNT(ttv.id) AS rewrite_count
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN shared.tenants t ON t.tenant_id = ttv.tenant_id
            GROUP BY t.slug, t.plan_tier ORDER BY rewrite_count DESC
        """)

        daily_rewrites = await conn.fetch("""
            SELECT DATE(created_at) AS day, COUNT(*) AS rewrites
            FROM gold_aa_internal.tenant_tour_versions
            WHERE created_at >= NOW() - ($1 || ' days')::interval
            GROUP BY DATE(created_at)
        """, str(days))

        # AA-476: same "rows, not real calls" caveat as the `models` query above — kept the
        # response key name (llm_calls) to avoid a wider API contract change; the frontend
        # displays it as "Content Versions", not "LLM Calls".
        llm_calls = await conn.fetchval(
            f"SELECT COUNT(*) FROM silver_{tenant_slug}.generated_content"
        )

        last_run = await conn.fetchrow("""
            SELECT tours_total, tours_passed, tours_failed, started_at, completed_at,
                   EXTRACT(EPOCH FROM (completed_at - started_at)) AS duration_sec
            FROM shared.pipeline_runs
            WHERE completed_at IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
        """)

        health_rows = await conn.fetch("""
            SELECT endpoint,
                   COUNT(*)                                             AS calls,
                   ROUND(AVG(response_ms)::numeric, 0)                 AS avg_ms,
                   SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS errors,
                   SUM(CASE WHEN status_code = 429  THEN 1 ELSE 0 END) AS rate_limited,
                   MAX(called_at)                                       AS last_call
            FROM shared.tenant_api_usage
            WHERE called_at >= NOW() - INTERVAL '1 hour'
            GROUP BY endpoint ORDER BY calls DESC
        """)

    ENDPOINT_SERVICE_MAP = {
        "/v1/pipeline/run":         "Step Functions Pipeline",
        "/v1/pipeline/run-tour":    "Content Generation",
        "/admin/run-tour":          "Content Generation",
        "/v1/pipeline/review-queue": "Validation Lambda",
        "/v1/tours":                "Export / Catalog API",
        "/v1/pipeline/upload-url":  "Ingestion Lambda",
        "/admin/upload-url":        "Ingestion Lambda",
        "/admin/metrics":           "Admin Metrics API",
        "/v1/pipeline/sources":     "Source Tracker",
        "/health":                  "API Health Check",
    }

    import re as _re
    pipeline_health = []
    seen_services: set = set()
    for r in health_rows:
        ep = r["endpoint"]
        ep_norm = _re.sub(r'/[0-9a-f-]{8,}', '/{id}', ep)
        service = ENDPOINT_SERVICE_MAP.get(ep, ENDPOINT_SERVICE_MAP.get(ep_norm, ep))
        if service in seen_services:
            continue
        seen_services.add(service)
        errors = int(r["errors"] or 0)
        calls  = int(r["calls"] or 0)
        avg_ms = float(r["avg_ms"] or 0)
        status = "healthy" if errors == 0 else ("degraded" if errors / max(calls, 1) < 0.1 else "down")
        pipeline_health.append({"name": service, "status": status,
                                 "latency": f"{avg_ms:.0f}ms", "errors": errors, "calls": calls})

    CORE_SERVICES = ["Ingestion Lambda", "Step Functions Pipeline",
                     "Content Generation", "Validation Lambda", "Export / Catalog API"]
    for svc in CORE_SERVICES:
        if svc not in seen_services:
            pipeline_health.append({"name": svc, "status": "idle",
                                    "latency": "—", "errors": 0, "calls": 0})

    cost_map = {r["model"]: float(r["total_cost"]) for r in cost_by_model}
    seen_models: set = set()
    model_usage = []
    for r in models:
        model      = r["model"]
        calls      = int(r["calls"])
        total_cost = cost_map.get(model, 0.0)
        model_usage.append({
            "model":         model,
            "calls":         calls,
            "avg_score":     float(r["avg_score"]) if r["avg_score"] else None,
            "total_cost":    round(total_cost, 4),
            "cost_per_call": round(total_cost / calls, 6) if calls > 0 else 0.0,
        })
        seen_models.add(model)
    for r in cost_by_model:
        if r["model"] not in seen_models:
            total_cost = float(r["total_cost"])
            model_usage.append({
                "model": r["model"], "calls": int(r["batches"]),
                "avg_score": None, "total_cost": round(total_cost, 4), "cost_per_call": 0.0,
            })

    rewrite_by_day = {str(r["day"]): int(r["rewrites"]) for r in daily_rewrites}
    return {
        "daily_runs": [
            {
                "date":     str(r["day"]),
                "runs":     r["runs"],
                "tours":    r["tours"],
                "passed":   r["passed"],
                "hitl":     r["hitl"],
                "failed":   r["failed"],
                "cost":     float(r["cost"]),
                "rewrites": rewrite_by_day.get(str(r["day"]), 0),
            }
            for r in daily
        ],
        "model_usage": model_usage,
        "avg_cost_per_run": float(avg_cost_per_run) if avg_cost_per_run else 0.0,
        "last_run": {
            "tours_total":  last_run["tours_total"]  if last_run else 0,
            "tours_passed": last_run["tours_passed"] if last_run else 0,
            "tours_failed": last_run["tours_failed"] if last_run else 0,
            "duration_sec": float(last_run["duration_sec"]) if last_run and last_run["duration_sec"] else 0,
        } if last_run else None,
        "pipeline_health": pipeline_health,
        "published_count":       int(published_count or 0),
        "tenant_rewrite_count":  int(tenant_rewrite_count or 0),
        "llm_calls":             int(llm_calls or 0),
        "content_summary": {
            "total_published_master":    int(published_count or 0),
            "total_tenant_rewrites":     int(tenant_rewrite_count or 0),
            "total_content_all_tenants": int(published_count or 0) + int(tenant_rewrite_count or 0),
            "tenant_breakdown": [
                {"slug": r["slug"], "plan_tier": r["plan_tier"], "rewrite_count": int(r["rewrite_count"])}
                for r in tenant_breakdown
            ],
        },
    }


# ── GET /admin/metrics/seo ────────────────────────────────────────────────────

@router.get("/metrics/seo")
async def get_seo_metrics(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        top_keywords = await conn.fetch("""
            SELECT keyword_search, top_keywords, fetched_at
            FROM silver_aa_internal.seo_context
            ORDER BY fetched_at DESC LIMIT 20
        """)
        total_tours = await conn.fetchval(
            "SELECT COUNT(*) FROM gold_aa_internal.published_tours "
            "WHERE tenant_id = '00000000-0000-0000-0000-000000000001'::uuid "
            "AND master_status <> 'trashed'"
        )
        seo_covered = await conn.fetchval("""
            SELECT COUNT(DISTINCT pt.tour_id)
            FROM gold_aa_internal.published_tours pt
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND pt.master_status <> 'trashed'
              AND EXISTS (
                  SELECT 1 FROM silver_aa_internal.seo_context sc WHERE sc.tour_id = rt.tour_id
              )
        """)
        countries = await conn.fetch("""
            SELECT rt.country, COUNT(sc.id) as count
            FROM silver_aa_internal.seo_context sc
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = sc.tour_id
            WHERE rt.country IS NOT NULL
            GROUP BY rt.country ORDER BY count DESC
        """)

    redis = request.app.state.redis
    cache_stats: dict = {"hit_rate": "N/A", "keys": 0}
    try:
        info = await redis.info("stats")
        hits   = int(info.get("keyspace_hits", 0))
        misses = int(info.get("keyspace_misses", 0))
        total  = hits + misses
        cache_stats["hit_rate"] = f"{round(hits/total*100, 1)}%" if total > 0 else "0%"
        cache_stats["hits"]     = hits
        cache_stats["misses"]   = misses
        db_info = await redis.info("keyspace")
        cache_stats["keys"] = sum(
            int(v.split(",")[0].split("=")[1])
            for v in db_info.values() if isinstance(v, str) and "keys=" in v
        )
    except Exception:
        pass

    import json as _j
    keyword_counts: dict = {}
    for row in top_keywords:
        try:
            kw_data = row["top_keywords"]
            if isinstance(kw_data, str):
                kw_data = _j.loads(kw_data)
            items = (
                kw_data if isinstance(kw_data, list)
                else (kw_data.get("top_keywords") or [] if isinstance(kw_data, dict) else [])
            )
            for item in items[:5]:
                kw = item.get("keyword") if isinstance(item, dict) else str(item)
                if kw:
                    keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
        except Exception:
            pass

    top_kw = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    return {
        "total_tours":  total_tours,
        "seo_covered":  seo_covered,
        "coverage_pct": round(seo_covered / total_tours * 100, 1) if total_tours else 0,
        "countries":    [{"country": dict(r)["country"], "count": dict(r)["count"]} for r in countries],
        "top_keywords": [{"keyword": k, "count": v} for k, v in top_kw],
        "cache":        cache_stats,
    }


# ── GET /admin/metrics/library ────────────────────────────────────────────────

@router.get("/metrics/library")
async def get_library_metrics(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        by_country = await conn.fetch("""
            SELECT rt.country,
                   COUNT(pt.id)                            AS total,
                   ROUND(AVG(pt.quality_score)::numeric,2) AS avg_score,
                   MAX(pt.published_at)                    AS last_published
            FROM gold_aa_internal.published_tours pt
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND pt.master_status <> 'trashed'
            GROUP BY rt.country ORDER BY total DESC
        """)
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*)                                  AS total,
                ROUND(AVG(quality_score)::numeric, 2)     AS avg_score,
                COUNT(CASE WHEN published_at >= NOW() - INTERVAL '30 days' THEN 1 END) AS published_last_30d,
                COUNT(CASE WHEN published_at < NOW() - INTERVAL '180 days' THEN 1 END) AS stale_count
            FROM gold_aa_internal.published_tours
            WHERE tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND master_status <> 'trashed'
        """)
        score_dist = await conn.fetch("""
            SELECT
                CASE
                    WHEN quality_score >= 9 THEN '9-10'
                    WHEN quality_score >= 8 THEN '8-9'
                    WHEN quality_score >= 7 THEN '7-8'
                    ELSE '<7'
                END AS range,
                COUNT(*) AS count
            FROM gold_aa_internal.published_tours
            WHERE tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND master_status <> 'trashed'
            GROUP BY range ORDER BY range DESC
        """)

    return {
        "total":              stats["total"],
        "avg_score":          float(stats["avg_score"] or 0),
        "published_last_30d": stats["published_last_30d"],
        "stale_count":        stats["stale_count"],
        "by_country":         [dict(r) for r in by_country],
        "score_distribution": [dict(r) for r in score_dist],
    }


# ── GET /admin/metrics/spot-workers ──────────────────────────────────────────

@router.get("/metrics/spot-workers")
async def get_spot_workers(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    ecs = _boto3.client("ecs", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    cluster = os.environ.get("ECS_CLUSTER", "aa-cis-dev-cluster")
    try:
        task_arns = ecs.list_tasks(cluster=cluster, desiredStatus="RUNNING").get("taskArns", [])
        spot_tasks, on_demand_tasks = [], []
        if task_arns:
            for t in ecs.describe_tasks(cluster=cluster, tasks=task_arns).get("tasks", []):
                cap  = t.get("capacityProviderName", "FARGATE")
                info = {
                    "task_id":  t["taskArn"].split("/")[-1][:12],
                    "status":   t.get("lastStatus", "UNKNOWN"),
                    "cpu":      t.get("cpu", "256"),
                    "memory":   t.get("memory", "512"),
                    "started":  str(t.get("startedAt", "")),
                    "capacity": cap,
                }
                (spot_tasks if cap == "FARGATE_SPOT" else on_demand_tasks).append(info)
        spot_count  = len(spot_tasks)
        total_count = len(task_arns)
        return {
            "total_tasks":     total_count,
            "spot_tasks":      spot_count,
            "on_demand_tasks": len(on_demand_tasks),
            "spot_pct":        round(spot_count / total_count * 100) if total_count else 0,
            "saving_per_hr":   round(spot_count * 0.04048 * 0.7, 4),
            "tasks":           spot_tasks + on_demand_tasks,
        }
    except Exception as e:
        return {"total_tasks": 0, "spot_tasks": 0, "on_demand_tasks": 0,
                "spot_pct": 0, "saving_per_hr": 0, "tasks": [], "error": str(e)}


# ── GET /admin/infra/status ───────────────────────────────────────────────────
# AA-257 — live infra state (ECS/RDS/NAT/Redis), cached 60s in Redis to avoid
# re-hitting 5 describe-type AWS APIs on every page load/tab.
#
# STEP0 finding: aa-cis-dev-ecs-task-role (the role this endpoint runs under)
# does NOT currently have ecs:Describe*/ecs:ListTasks, rds:DescribeDBInstances,
# ec2:DescribeInstances, elasticache:DescribeCacheClusters, or ecr:DescribeImages
# — confirmed via a real call to the pre-existing GET /admin/metrics/spot-workers
# above, which has been silently returning AccessDeniedException in prod this
# whole time (degrades to zeros, per its own try/except). Same gap here.
# Per-check try/except below mirrors that same graceful-degradation shape —
# each section reports {"error": "..."} on AccessDenied rather than 500ing the
# whole endpoint, so this ships real and useful the moment the IAM grant lands
# (a Terraform PR, opened not auto-merged — see AA-257 Linear comment) instead
# of waiting on it. Also fixed a stale hardcoded value from the issue text: the
# NAT instance ID it names (i-04ebd090e97184f45) no longer exists — the real
# current one (i-05363515303ef7c21, tag Name=aa-cis-dev-nat-instance) is the
# default here, override-able via NAT_INSTANCE_ID like the other resource IDs.
_INFRA_STATUS_CACHE_KEY = "admin:infra_status:v1"
_INFRA_STATUS_CACHE_TTL_S = 60


@router.get("/infra/status")
async def get_infra_status(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)

    redis = request.app.state.redis
    try:
        cached = await redis.get(_INFRA_STATUS_CACHE_KEY)
        if cached:
            data = json.loads(cached)
            data["cache_hit"] = True
            return data
    except Exception as e:
        logger.warning("infra_status_redis_read_error", error=str(e))

    region        = os.environ.get("AWS_REGION", "us-west-1")
    cluster       = os.environ.get("ECS_CLUSTER", "aa-cis-dev-cluster")
    service       = os.environ.get("ECS_SERVICE", "aa-cis-dev-api")
    ecr_repo      = os.environ.get("ECR_REPO", "aa-cis-dev-api")
    db_id         = os.environ.get("RDS_INSTANCE_ID", "aa-cis-dev-db")
    nat_id        = os.environ.get("NAT_INSTANCE_ID", "i-05363515303ef7c21")
    redis_id      = os.environ.get("REDIS_CLUSTER_ID", "aa-cis-dev-redis")

    result = {
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cache_hit":  False,
    }

    # ── ECS (service state + running-task digest vs ECR :latest) ──
    try:
        ecs = _boto3.client("ecs", region_name=region)
        svc = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
        ecs_info = {
            "status":        svc["status"],
            "desired_count": svc["desiredCount"],
            "running_count": svc["runningCount"],
            "pending_count": svc["pendingCount"],
        }
        task_arns = ecs.list_tasks(cluster=cluster, serviceName=service,
                                    desiredStatus="RUNNING").get("taskArns", [])
        running_digest = None
        if task_arns:
            tasks = ecs.describe_tasks(cluster=cluster, tasks=task_arns).get("tasks", [])
            if tasks and tasks[0].get("containers"):
                running_digest = tasks[0]["containers"][0].get("imageDigest")
        ecs_info["running_digest"] = running_digest

        try:
            ecr = _boto3.client("ecr", region_name=region)
            latest = ecr.describe_images(
                repositoryName=ecr_repo, imageIds=[{"imageTag": "latest"}]
            )["imageDetails"][0]
            ecs_info["latest_digest"] = latest["imageDigest"]
            ecs_info["digest_matches_latest"] = (
                running_digest == latest["imageDigest"] if running_digest else None
            )
        except Exception as e:
            ecs_info["ecr_error"] = str(e)

        result["ecs"] = ecs_info
    except Exception as e:
        result["ecs"] = {"error": str(e)}

    # ── RDS ──
    try:
        rds = _boto3.client("rds", region_name=region)
        db = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
        result["rds"] = {
            "status":         db["DBInstanceStatus"],
            "engine":         db["Engine"],
            "instance_class": db["DBInstanceClass"],
        }
    except Exception as e:
        result["rds"] = {"error": str(e)}

    # ── NAT instance ──
    try:
        ec2 = _boto3.client("ec2", region_name=region)
        inst = ec2.describe_instances(InstanceIds=[nat_id])["Reservations"][0]["Instances"][0]
        result["nat"] = {"instance_id": nat_id, "state": inst["State"]["Name"]}
    except Exception as e:
        result["nat"] = {"instance_id": nat_id, "error": str(e)}

    # ── ElastiCache Redis ──
    try:
        ec = _boto3.client("elasticache", region_name=region)
        cache = ec.describe_cache_clusters(CacheClusterId=redis_id)["CacheClusters"][0]
        result["redis"] = {"status": cache["CacheClusterStatus"], "node_type": cache["CacheNodeType"]}
    except Exception as e:
        result["redis"] = {"error": str(e)}

    # AA-257: "AWS đang chạy — nhớ tắt cuối session" badge — true whenever ECS
    # has a nonzero desired count or RDS is available, regardless of which
    # describe calls succeeded (defaults to True/unknown-but-assume-running on
    # error, since a false "all stopped" reading is the worse mistake here).
    ecs_desired = result.get("ecs", {}).get("desired_count")
    rds_status  = result.get("rds", {}).get("status")
    result["running_reminder"] = (
        ecs_desired is None or ecs_desired > 0 or rds_status is None or rds_status == "available"
    )

    try:
        await redis.set(_INFRA_STATUS_CACHE_KEY, json.dumps(result), ex=_INFRA_STATUS_CACHE_TTL_S)
    except Exception as e:
        logger.warning("infra_status_redis_write_error", error=str(e))

    return result


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
    """Parse a brand brief DOCX (base64-encoded JSON) and return pre-filled brand fields."""
    verify_admin_secret(x_admin_secret)
    try:
        from docx import Document  # type: ignore
        import io
        content = base64.b64decode(body.file_base64)
        doc = Document(io.BytesIO(content))
    except ImportError:
        raise HTTPException(status_code=501, detail="python-docx not installed on this server")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse DOCX: {e}")

    _FIELD_KEYS = {
        "brand name": "brand_name",
        "brand type": "brand_type",
        "core idea": "core_idea",
        "target market": "target_markets",
        "customer segment": "customer_segment",
        "customer mindset": "customer_mindset",
        "tone of voice": "tone_of_voice",
        "tone": "tone_of_voice",
        "writing style": "writing_style",
        "good example": "good_examples",
        "should write": "should_write",
        "forbidden word": "forbidden_words",
        "forbidden": "forbidden_words",
    }
    _LIST_FIELDS = {"target_markets", "tone_of_voice", "forbidden_words"}

    result: dict = {}
    current_field: str | None = None
    buffer: list[str] = []

    def _flush():
        if current_field and buffer:
            text = "\n".join(buffer).strip()
            if current_field in _LIST_FIELDS:
                items = [line.lstrip("-•·*").strip() for line in text.splitlines() if line.strip()]
                result[current_field] = items or [text]
            else:
                result[current_field] = text

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        lower = text.lower().rstrip(":").strip()
        matched = next((v for k, v in _FIELD_KEYS.items() if lower == k or lower.startswith(k)), None)
        if matched or para.style.name.startswith("Heading"):
            _flush()
            buffer = []
            current_field = matched or current_field
            inline = text.split(":", 1)[1].strip() if ":" in text else ""
            if inline:
                buffer.append(inline)
        else:
            buffer.append(text)

    _flush()

    return {"parsed": result, "filename": body.filename}


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
    return {"status": "deleted", "brand_name": brand_name}


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
) -> str:
    if credentials is not None:
        try:
            payload = verify_jwt(credentials.credentials)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return payload["sub"]
    verify_admin_secret(x_admin_secret)
    return _AA_INTERNAL_TENANT_ID


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


@router.get("/brand-identity")
async def get_brand_identity(
    request: Request, tenant_id: str = Depends(_resolve_brand_tenant_id)
):
    # AA-557 J.23 — full field set (see BrandIdentityUpdate's own comment for why).
    _BRAND_COLS = (
        "system_prompt, style_guide, forbidden_words, version, updated_at, created_at, "
        "COALESCE(brand_name, '') AS brand_name, COALESCE(brand_type, '') AS brand_type, "
        "COALESCE(core_idea, '') AS core_idea, COALESCE(customer_segment, '') AS customer_segment, "
        "COALESCE(customer_mindset, '') AS customer_mindset, "
        "COALESCE(voice_examples, '[]'::jsonb) AS voice_examples, "
        "COALESCE(good_examples, '') AS good_examples, "
        "COALESCE(target_markets, ARRAY[]::text[]) AS target_markets"
    )
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"""
            SELECT {_BRAND_COLS}
            FROM shared.tenant_brand_rules
            WHERE tenant_id = $1 AND is_active = true
            ORDER BY version DESC LIMIT 1
        """, tenant_id)
        if not row:
            return {
                "configured": False, "system_prompt": None, "style_guide": None,
                "forbidden_words": [], "history": [], "brand_name": "", "brand_type": "",
                "core_idea": "", "customer_segment": "", "customer_mindset": "",
                "tone_of_voice": [], "good_examples": "", "target_markets": [],
            }
        history_rows = await conn.fetch(f"""
            SELECT {_BRAND_COLS}, is_active
            FROM shared.tenant_brand_rules WHERE tenant_id = $1 ORDER BY version DESC
        """, tenant_id)

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
    }


@router.post("/brand-identity")
async def update_brand_identity(
    body: BrandIdentityUpdate,
    request: Request,
    tenant_id: str = Depends(_resolve_brand_tenant_id),
):
    import json as _json
    # AA-487: this is a MORE direct prompt-injection surface than the DOCX Lambda
    # (services/acp_brand_brief_parser/) — BrandTab.tsx lets a tenant type system_prompt/
    # style_guide straight into a textarea and POST here, with no parsing step at all in
    # between. Sanitize the same way (shared.validators.prompt_sanitize, same module the Lambda
    # fix uses) before it lands in the column every future LLM call for this tenant replays.
    system_prompt = sanitize_text(body.system_prompt, MAX_SYSTEM_PROMPT_LEN)
    style_guide = sanitize_text(body.writing_style or body.style_guide, MAX_LONG_FIELD_LEN)
    forbidden_words = sanitize_list(body.forbidden_words or [], MAX_SHORT_FIELD_LEN, max_items=20)
    # AA-557 J.23 — extended fields, same sanitize_text/sanitize_list guards as the pre-existing
    # 3; brand_name falls back to 'default' (the single-brand-per-tenant sentinel this file
    # already uses elsewhere, _resolve_brand_rule) when the tenant hasn't named their brand yet.
    brand_name = sanitize_text(body.brand_name, MAX_SHORT_FIELD_LEN) or "default"
    brand_type = sanitize_text(body.brand_type, MAX_SHORT_FIELD_LEN)
    core_idea = sanitize_text(body.core_idea, MAX_LONG_FIELD_LEN)
    customer_segment = sanitize_text(body.customer_segment, MAX_LONG_FIELD_LEN)
    customer_mindset = sanitize_text(body.customer_mindset, MAX_LONG_FIELD_LEN)
    tone_of_voice = sanitize_list(body.tone_of_voice or [], MAX_SHORT_FIELD_LEN, max_items=20)
    good_examples = sanitize_text(body.good_examples, MAX_LONG_FIELD_LEN)
    target_markets = sanitize_list(body.target_markets or [], MAX_SHORT_FIELD_LEN, max_items=20)

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # NOT scoped by brand_name — this tenant-facing endpoint keeps its pre-existing invariant
        # (AA-424: exactly one active row per tenant_id at a time, regardless of brand_name) so a
        # tenant editing their own brand_name mid-flow can't leave 2 simultaneously-active rows
        # behind for GET to pick between non-deterministically. The admin-only `/admin/brands`
        # CRUD (BrandCreateRequest, above) is the one genuinely multi-brand-per-tenant path.
        current = await conn.fetchval("""
            SELECT COALESCE(MAX(version), 0) FROM shared.tenant_brand_rules WHERE tenant_id = $1
        """, tenant_id)
        await conn.execute(
            "UPDATE shared.tenant_brand_rules SET is_active = false WHERE tenant_id = $1", tenant_id
        )
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
        await conn.execute("""
            INSERT INTO shared.tenant_brand_rules
                (tenant_id, system_prompt, style_guide, forbidden_words, version, is_active,
                 updated_at, brand_name, brand_type, core_idea, customer_segment,
                 customer_mindset, voice_examples, good_examples, target_markets)
            VALUES ($1, $2, $3, $4::jsonb, $5, true, NOW(), $6, $7, $8, $9, $10, $11::jsonb, $12, $13)
        """, tenant_id, system_prompt, style_guide, _json.dumps(forbidden_words), current + 1,
            brand_name, brand_type, core_idea, customer_segment, customer_mindset,
            _json.dumps(tone_of_voice), good_examples, target_markets)
    return {"status": "updated", "version": current + 1}


_BRAND_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10MB — matches BrandTab.tsx's own stated limit
_BRAND_UPLOAD_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
}


@router.post("/brand-identity/upload")
async def upload_brand_file(
    tenant_id: str = Depends(_resolve_brand_tenant_id),
    file: UploadFile = File(...),
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

    if file.content_type not in _BRAND_UPLOAD_ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {file.content_type}")

    contents = await file.read()
    if len(contents) > _BRAND_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10MB limit")

    safe_name = _re2.sub(r'[^a-zA-Z0-9._-]', '_', file.filename or "brand.pdf")
    s3_key    = f"brand-identity/{tenant_id}/{_uuid2.uuid4()}_{safe_name}"
    bucket    = os.environ.get("BRONZE_BUCKET", "aa-cis-bronze-867490540162")

    s3 = _boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    try:
        s3.put_object(Bucket=bucket, Key=s3_key, Body=contents, ContentType=file.content_type)
    except Exception as e:
        logger.error("brand_identity_upload_failed", tenant_id=tenant_id, s3_key=s3_key, error=str(e))
        raise HTTPException(status_code=502, detail="Upload to storage failed")

    return {"status": "uploaded", "s3_key": s3_key}


# ── GET /admin/billing ────────────────────────────────────────────────────────

@router.get("/billing")
async def get_admin_billing(
    request: Request,
    x_admin_secret: str = Header(None),
    tenant_id: str = "00000000-0000-0000-0000-000000000001",
):
    """Admin billing view — defaults to aa_internal; pass ?tenant_id= for a specific tenant."""
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
        return {
            "plan_tier": "starter", "tours_quota_monthly": 50,
            "api_calls_quota_monthly": 5000, "price_usd_monthly": 299.0,
            "tours_rewritten": 0, "api_calls_used": 0,
            "quota_tours_pct": 0.0, "quota_calls_pct": 0.0,
            "tours_overage": 0, "overage_usd": 0.0,
            "llm_cost_usd": 0.0, "overage_rate_usd_per_tour": 4.0,
            "activity": [],
        }

    return {
        **{k: (float(v) if hasattr(v, '__float__') and not isinstance(v, int) else v)
           for k, v in dict(row).items() if k != "billing_month"},
        "billing_month": str(row["billing_month"])[:7] if row["billing_month"] else None,
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


# ── GET /admin/acp/stuck-runs ─────────────────────────────────────────────────

@router.get("/acp/stuck-runs")
async def get_stuck_runs(
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Return acp_shared.acp_runs rows stuck in s1_pending for >30 minutes."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT run_id::text, status, started_at,
                   EXTRACT(EPOCH FROM (NOW() - started_at)) / 60 AS age_minutes
            FROM acp_shared.acp_runs
            WHERE status = 's1_pending'
              AND started_at < NOW() - INTERVAL '30 minutes'
            ORDER BY started_at ASC
        """)
    return {
        "stuck_runs": [
            {
                "run_id":      r["run_id"],
                "status":      r["status"],
                "started_at":  r["started_at"].isoformat() if r["started_at"] else None,
                "age_minutes": round(float(r["age_minutes"]), 1) if r["age_minutes"] else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


# ── POST /admin/acp/runs/{run_id}/force-fail ──────────────────────────────────

@router.post("/acp/runs/{run_id}/force-fail")
async def force_fail_acp_run(
    run_id: str,
    body: ForceFailRequest,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Force a stuck acp_shared.acp_runs row to status='failed'. x-admin-secret required."""
    verify_admin_secret(x_admin_secret)
    try:
        run_uuid = str(UUID(run_id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail=f"Invalid UUID: {run_id!r}")

    reason = body.reason.strip()[:500]
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE acp_shared.acp_runs SET status='failed', error_message=$2 "
            "WHERE run_id=$1::uuid RETURNING run_id::text",
            run_uuid, reason,
        )
    if not updated:
        raise HTTPException(status_code=404, detail=f"run_id {run_id} not found")

    logger.info("acp_run_force_failed", run_id=run_id)
    return {"run_id": run_id, "status": "failed", "reason": reason}
