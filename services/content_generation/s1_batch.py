"""AA-606: orchestrate S1-rewrite attempt-1 over Bedrock Batch Inference.

Hybrid rerun for the ~763-tour S1 backlog:
  submit_s1_batch(tour_ids)  → materialize each tour's attempt-1 (system,user) prompt (the SAME
    builder generate_node uses) into a JSONL manifest, upload to S3, CreateModelInvocationJob.
  ingest_s1_batch(job)       → poll to completion, read output, and for each tour hand the raw
    writer text to admin_pipeline._execute_run_tour(batch_text=...), which runs the identical
    validate → judge → gate → persist → export/review-queue path. A tour whose batch write fails
    the gate falls into the existing on-demand retry loop (generate_node attempt-2/3) inside that
    same call — so nothing about post-gate behaviour changes, only WHERE attempt-1 was written.

Why the writer prompt is materialized here rather than reused from _execute_run_tour: Batch needs
every record BEFORE any model call, whereas _execute_run_tour builds one prompt per invocation. The
prompt builder (batch_prompt.materialize_s1_prompt) is pure and shared, so the manifest prompt is
byte-identical to what generate_node would have sent. SEO context is fetched the same way both here
(for the prompt) and in _execute_run_tour (for validate/persist); DataForSEO is Redis-cached per
country/seed so the two reads agree in practice.

NOTE: the writer here is Haiku only (the S1 writer tier). The judge (GPT-4.1) never batches — it
runs on-demand inside _execute_run_tour. Do not route the judge through this module.
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

import asyncpg
import structlog

from shared.llm_client import bedrock_batch
from shared.llm_client.bedrock_batch import BatchUnavailable
from services.content_generation.batch_prompt import materialize_s1_prompt

logger = structlog.get_logger()

_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"
_TENANT_SLUG_MAP = {"aa_internal": _MASTER_TENANT_ID}


async def _load_prompt_state(conn, tour_id: str, tenant_uuid: str, *,
                             seo_mode: str = "standard",
                             rewrite_language: str = "en-US",
                             subtitle_focus: str = "standard") -> Optional[dict]:
    """Build the graph-state-shaped dict the prompt builder needs for one tour: tour data +
    resolved brand rule (+ lessons) + SEO context. Returns None if the tour is missing/trashed.

    Deliberately mirrors _execute_run_tour's own context load (raw_tours row shape, brand resolve,
    lessons append, SEO fetch) so the materialized prompt matches the synchronous writer prompt.
    Imported lazily from admin_pipeline to reuse _resolve_brand_rule without a circular import."""
    from api.routers.admin_pipeline import _resolve_brand_rule
    import json as _json

    row = await conn.fetchrow(
        "SELECT * FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid", tour_id,
    )
    if not row:
        logger.warning("s1_batch_tour_missing", tour_id=tour_id)
        return None
    if row.get("source_status") and str(row["source_status"]) == "trashed":
        logger.info("s1_batch_tour_trashed_skip", tour_id=tour_id)
        return None

    src_highlights = row["src_highlights"]
    if not isinstance(src_highlights, list):
        src_highlights = _json.loads(src_highlights) if src_highlights else []

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
    try:
        br_row = await _resolve_brand_rule(conn, tenant_uuid, None, None)
        if br_row:
            _voice = br_row["voice_examples"]
            brand_rules = {
                "system_prompt":   br_row["system_prompt"] or "",
                "style_guide":     br_row["style_guide"] or "",
                "forbidden_words": (
                    list(br_row["forbidden_words"]) if isinstance(br_row["forbidden_words"], list)
                    else _json.loads(br_row["forbidden_words"] or "[]")
                ),
                "core_idea":        br_row.get("core_idea") or "",
                "customer_segment": br_row.get("customer_segment") or "",
                "customer_mindset": br_row.get("customer_mindset") or "",
                "voice_examples":   (
                    list(_voice) if isinstance(_voice, list) else _json.loads(_voice or "[]")
                ),
                "good_examples":    br_row.get("good_examples") or "",
            }
    except Exception as _br_err:
        logger.warning("s1_batch_brand_fetch_failed", tour_id=tour_id, error=str(_br_err))

    # Lessons (same append the sync path does) — non-fatal.
    try:
        lesson_rows = await conn.fetch(
            "SELECT what_to_do, field FROM shared.pipeline_lessons "
            "WHERE stage IN ('S1_rewrite','S1_editor') AND is_active = true ORDER BY id"
        )
        if lesson_rows:
            lines = [f"- {('['+lr['field']+'] ') if lr['field'] else ''}{lr['what_to_do']}"
                     for lr in lesson_rows]
            brand_rules["system_prompt"] = (brand_rules.get("system_prompt") or "") + (
                "\n\n## LESSONS FROM PREVIOUS BATCHES\n"
                "Apply these rules strictly — they come from real batch failures:\n" + "\n".join(lines)
            )
    except Exception as _le:
        logger.warning("s1_batch_lessons_failed", tour_id=tour_id, error=str(_le))

    seo_data: dict = {}
    _SEO_MODE_MAP = {"standard": "dataforseo", "aggressive": "dataforseo", "minimal": "disabled"}
    effective_seo_mode = _SEO_MODE_MAP.get(seo_mode, seo_mode)
    try:
        from services.seo_intelligence.handler import process_seo
        from services.seo_intelligence.seed_builder import build_seed
        seed = build_seed(row.get("country"), row.get("activities"), row.get("src_name")) or row.get("src_name", "")
        if seed:
            seo_result = await process_seo(
                tour_id=tour_id, destination=seed, seed=seed,
                tenant_id=tenant_uuid, seo_mode=effective_seo_mode,
            )
            seo_data = seo_result.get("data", {})
            if "keywords" in seo_data and "top_keywords" not in seo_data:
                seo_data["top_keywords"] = seo_data["keywords"].get("top_keywords", [])
            elif "top_keywords" in seo_data and "keywords" not in seo_data:
                seo_data["keywords"] = {"top_keywords": seo_data["top_keywords"]}
            seo_data.setdefault("top_keywords", [])
            seo_data.setdefault("people_also_ask", [])
    except Exception as _seo_err:
        logger.warning("s1_batch_seo_failed", tour_id=tour_id, error=str(_seo_err))

    return {
        "tour": tour,
        "seo": seo_data,
        "few_shots": [],
        "subtitle_focus": subtitle_focus,
        "rewrite_language": brand_rules.get("rewrite_language", rewrite_language),
        "brand_system_prompt": brand_rules.get("system_prompt", ""),
        "brand_style_guide": brand_rules.get("style_guide", ""),
        "brand_forbidden_words": brand_rules.get("forbidden_words", []),
        "brand_core_idea": brand_rules.get("core_idea", ""),
        "brand_customer_segment": brand_rules.get("customer_segment", ""),
        "brand_customer_mindset": brand_rules.get("customer_mindset", ""),
        "brand_voice_examples": brand_rules.get("voice_examples", []),
        "brand_good_examples": brand_rules.get("good_examples", ""),
    }


async def submit_s1_batch(
    tour_ids: list[str],
    *,
    tenant_id: str = _MASTER_TENANT_ID,
    account: str = "acc3",
    max_tokens: int = 4096,
    enforce_min: bool = True,
) -> dict:
    """Materialize attempt-1 prompts for tour_ids, upload the JSONL manifest, and submit a Bedrock
    Batch job. Returns {"job_arn","job_slug","output_uri","account","tour_ids","record_count"}.

    enforce_min: reject a batch below MIN_BATCH_RECORDS (Bedrock rejects small Claude jobs) — set
    False only for a live quota probe once the real minimum is confirmed."""
    tenant_uuid = _TENANT_SLUG_MAP.get(tenant_id, tenant_id)
    job_slug = uuid.uuid4().hex[:12]

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    records: list[tuple[str, dict]] = []
    included: list[str] = []
    try:
        for tid in tour_ids:
            state = await _load_prompt_state(conn, tid, tenant_uuid)
            if state is None:
                continue
            system, user, _pv = materialize_s1_prompt(state)
            records.append((tid, bedrock_batch.build_model_input(system, user, max_tokens=max_tokens)))
            included.append(tid)
    finally:
        await conn.close()

    if not records:
        raise BatchUnavailable("no eligible tours to batch (all missing/trashed)")
    if enforce_min and len(records) < bedrock_batch.MIN_BATCH_RECORDS:
        raise BatchUnavailable(
            f"batch has {len(records)} records, below Bedrock minimum "
            f"{bedrock_batch.MIN_BATCH_RECORDS} — add more tours or split differently"
        )

    jsonl = bedrock_batch.build_manifest_jsonl(records)
    input_uri = bedrock_batch.upload_manifest(jsonl, account=account, job_slug=job_slug)
    job = bedrock_batch.submit_batch_job(
        input_uri, account=account, model="haiku", job_slug=job_slug,
    )
    logger.info("s1_batch_submitted", job_arn=job.get("job_arn"), record_count=len(records),
                account=account)
    return {**job, "tour_ids": included, "record_count": len(records)}


async def ingest_s1_batch(
    *,
    job_arn: str,
    output_uri: str,
    account: str,
    tour_ids: list[str],
    batch_id: str,
    seo_mode: str = "standard",
    model_tier: Optional[str] = "haiku",
    poll: bool = True,
) -> dict:
    """Poll the batch job (optional), read its output, then run each tour through the existing
    persist path via _execute_run_tour(batch_text=...). Returns a per-tour summary.

    Uses batch_id as TourRunRequest.batch_id so pipeline_runs accounting and generated_content
    metadata carry the same batch traceability the synchronous ingest already records."""
    from api.routers.admin_pipeline import TourRunRequest, _execute_run_tour

    if poll:
        status = bedrock_batch.poll_batch_job(job_arn, account=account)
        if status not in ("Completed", "PartiallyCompleted"):
            raise BatchUnavailable(f"batch job terminal status={status}, nothing to ingest")

    outputs = bedrock_batch.read_batch_output(output_uri, account=account)

    summary = {"ingested": 0, "gate_fail_retry": 0, "batch_write_failed": 0, "missing_output": 0,
               "per_tour": []}
    for tid in tour_ids:
        rec = outputs.get(str(tid))
        if rec is None:
            summary["missing_output"] += 1
            summary["per_tour"].append({"tour_id": tid, "outcome": "missing_output"})
            continue
        if not rec.ok:
            # No usable text from the batch write — let the tour rewrite entirely on-demand so it
            # still gets a version (the writer retry loop starts from generate, not the seed node).
            summary["batch_write_failed"] += 1
            req = TourRunRequest(tour_id=tid, batch_id=batch_id, tenant_id=_MASTER_TENANT_ID,
                                 seo_mode=seo_mode, model_tier=model_tier)
            try:
                await _execute_run_tour(req)
                summary["per_tour"].append({"tour_id": tid, "outcome": "batch_failed_fell_back_sync"})
            except Exception as e:
                summary["per_tour"].append({"tour_id": tid, "outcome": "error", "error": str(e)[:300]})
            continue

        req = TourRunRequest(tour_id=tid, batch_id=batch_id, tenant_id=_MASTER_TENANT_ID,
                             seo_mode=seo_mode, model_tier=model_tier)
        try:
            result = await _execute_run_tour(
                req,
                batch_text=rec.text,
                batch_model_used="satellite-batch-haiku-4-5",
                batch_account=account,
            )
            summary["ingested"] += 1
            # retry_count > 0 in the result means the batch write missed the gate and the on-demand
            # loop rewrote it — surfaced for observability, not an error.
            if (result.get("retry_count") or 0) > 0:
                summary["gate_fail_retry"] += 1
            summary["per_tour"].append({
                "tour_id": tid, "outcome": "ingested",
                "version_id": result.get("version_id"),
                "quality_score": result.get("quality_score"),
                "retry_count": result.get("retry_count"),
            })
        except Exception as e:
            summary["per_tour"].append({"tour_id": tid, "outcome": "error", "error": str(e)[:300]})

    logger.info("s1_batch_ingested", job_arn=job_arn, **{k: v for k, v in summary.items() if k != "per_tour"})
    return summary
