# api/routers/admin_pipeline/_shared.py — the router object, logger and the helpers several parts share (S1 result
# gates, review enqueue, generated metadata, brand rule resolver, request models).
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import asyncio
import datetime
import json
from uuid import UUID
import asyncpg
import structlog
from fastapi import APIRouter, Depends
from api.core.model_tier import refuse_model_tier
from api.core.staff_authz import staff_policy_if_staff
from pydantic import BaseModel, model_validator
from typing import Optional, List
from services.content_generation import s1_checkpoint as _ckpt  # AA-993 G2
from shared.tour_liveness import live_source_exists_sql
from services.quality.registry import threshold
from services.quality import final_contract as _fcon  # AA-1047 W1 (REPORT-mode exit contract)
from services.writing_memory.reason_codes import ReasonCode  # AA-916 WM-06
from services.quality.day_oracle import enforced_codes as _day_enforced_codes  # AA-925


logger = structlog.get_logger()
# AA-882 (GĐ3): staff_policy_if_staff narrows the staff calls (X-Admin-Secret) by role, per
# api/core/staff_authz.py POLICY; the routes' own guard still decides who else gets in.
router = APIRouter(prefix="/admin", tags=["admin-pipeline"], dependencies=[Depends(staff_policy_if_staff)])

_pipeline_semaphore = asyncio.Semaphore(2)


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


# AA-736: the subset of _HARD_BLOCK_CODES a model rewrite can actually fix (content/voice), so a
# survivor is worth one haiku->sonnet retry. The rest of _HARD_BLOCK_CODES (SEO_META_TOO_LONG,
# SEO_TITLE_TOO_LONG, META_INCOMPLETE_SENTENCE, META_TOO_SHORT) are deterministic-fixable by
# AA-641 fit_seo_meta/fit_seo_title — upgrading the model for those would waste a Sonnet run.
# AA-738: FORBIDDEN_WORD removed. The writer output is now stripped deterministically before
# validate (forbidden_strip), so a surviving one is a word the strip could not clean — and the S217
# Nepal wave showed Sonnet writes the same brand words (explore/package/nestled) anyway, while T2
# rewrites on Sonnet regardless. It goes straight to review instead of paying for Sonnet twice.
_REWRITE_CLASS_HARD_CODES = frozenset({
    "BRAND_SEO_META_VIOLATION", "MISSING_FIELD",
}) | _day_enforced_codes()  # AA-925: an enforced day-oracle code is a rewrite-class hard code (none yet)


def _surviving_hard_codes(result: dict) -> set:
    """AA-736: hard-block codes still present in a run's failure_codes. Mirrors the set
    human_edit_gate_node already enforces (graph._HARD_BLOCK_CODES), but applied to the
    automated rewrite->publish path, which historically never consulted it.

    AA-645 (S249): ``hard_block_codes(result)`` — an A1 writer-v2 draft also counts ITINERARY_DAY_COUNT_MISMATCH
    (graph.V2_HARD_BLOCK_CODES); v1 and tenant runs are unchanged."""
    from services.content_generation.graph import hard_block_codes
    return set(result.get("failure_codes", []) or []) & set(hard_block_codes(result))


# AA-926 — services/quality/thresholds.yaml (the pass line and the review score bands).
_PUBLISH_FLOOR = threshold("a1.publish_floor")
_LOW_QUALITY_FLOOR = threshold("review.low_quality_floor")
_SCORE_BAND_LOW = threshold("review.score_band_low")


def _is_publishable(result: dict) -> bool:
    """AA-211: audit-aware publish gate (mirror catalog readiness + v5 spec).

    quality_score >= 7.0 is the floor; additionally block manual_check, and block brand-flagged
    tours the fix pass did not repair (flagged + not fix_pass_applied).

    AA-736: ALSO block any run whose failure_codes still contain a hard-block code (e.g. a
    FORBIDDEN_WORD the writer emitted and flag_fix never removed). Before AA-736 this gate read
    only quality_score + brand_audit, so a tour scoring >=7 with a leftover forbidden word in its
    body was published to Master — _HARD_BLOCK_CODES was wired only into the human-edit re-validate
    gate (AA-234), never here. Everything else — a clean pass, flagged-but-fixed (Terra case), or
    no brand profile — stays publishable.
    """
    audit = result.get("brand_audit_status")
    return (
        result.get("quality_score", 0.0) >= _PUBLISH_FLOOR
        and audit != "manual_check"
        and not (audit == "flagged" and not result.get("fix_pass_applied"))
        and not _surviving_hard_codes(result)
    )


def _tour_passed(result: dict) -> bool:
    """The run-log pass flag (score only; the publish gate is `_is_publishable`). AA-926 seam."""
    return float(result.get("quality_score") or 0.0) >= _PUBLISH_FLOOR


def _score_band_where(score: str):
    """Review Queue score filter → SQL WHERE fragment (None = no filter). AA-926 seam: the band edges
    are pinned byte for byte by the gate-outcome snapshot."""
    if score == "critical":
        return f"rq.score_overall < {_SCORE_BAND_LOW:g}"
    if score == "low":
        return f"rq.score_overall >= {_SCORE_BAND_LOW:g} AND rq.score_overall < {_LOW_QUALITY_FLOOR:g}"
    if score == "ok":
        return f"rq.score_overall >= {_LOW_QUALITY_FLOOR:g}"
    return None


def _histogram_case(edges) -> str:
    """The published-tour score histogram CASE (library metrics): one bucket per edge, top bucket to 10."""
    pad = "\n" + " " * 20
    whens = [f"WHEN quality_score >= {e:g} THEN '{e:g}-{u:g}'" for e, u in zip(edges, [10, *edges[:-1]])]
    return "CASE" + "".join(pad + w for w in whens) + pad + f"ELSE '<{edges[-1]:g}'" + "\n" + " " * 16 + "END"


# Published-tour score histogram (/admin/pipeline overview). AA-926: pinned by the snapshot.
_SCORE_HISTOGRAM_CASE = _histogram_case(threshold("report.score_histogram_edges"))


def _build_failure_summary(result: dict) -> str:
    """AA-212: human-readable reason a tour was routed to the HITL review_queue.

    AA-738: a FORBIDDEN_WORD that survived the deterministic strip is named with its field(s).
    AA-736: when a sonnet retry was already tried (hard code survived haiku) and a hard code is
    STILL present, say so explicitly with the offending field(s) — the reviewer must know the
    stronger model was already attempted, e.g. 'retried with sonnet, still FORBIDDEN_WORD in
    aa_summary'.
    """
    audit = result.get("brand_audit_status")
    score = float(result.get("quality_score") or 0.0)
    codes = list(result.get("failure_codes", [])) + list(result.get("brand_audit_codes", []))
    parts = []
    if audit:
        parts.append(f"brand_audit={audit}")
    if codes:
        parts.append("codes=" + ",".join(str(c) for c in codes))
    if score < _LOW_QUALITY_FLOOR:
        parts.append(f"low_quality(score={score:.1f}<{_LOW_QUALITY_FLOOR:.1f})")

    # AA-736: surface the surviving hard codes per field, noting the sonnet attempt.
    surviving = _surviving_hard_codes(result)
    if result.get("_sonnet_retried") and surviving:
        gc = _result_to_gc(result)
        field_fails = _derive_field_failures(gc, sorted(surviving), result.get("_brand_forbidden_words"))
        where = ", ".join(
            f"{f['code']} in {f['field']}" for f in field_fails if f["code"] in surviving
        ) or ",".join(sorted(surviving))
        parts.append(f"retried with sonnet, still {where}")
    elif "FORBIDDEN_WORD" in surviving:
        # AA-738: no Sonnet retry for FORBIDDEN_WORD — tell the reviewer where the word is.
        gc = _result_to_gc(result)
        fields = sorted({f["field"] for f in _derive_field_failures(
            gc, ["FORBIDDEN_WORD"], result.get("_brand_forbidden_words")) if f["code"] == "FORBIDDEN_WORD"})
        parts.append("forbidden word not auto-removable" + (f" in {', '.join(fields)}" if fields else ""))

    return "; ".join(parts) or "blocked: not publishable"


def _result_to_gc(result: dict) -> dict:
    """AA-736: map a rewrite `result['generated']` (name/subtitle/summary/... keys) onto the
    gc-column keys _derive_field_failures expects (aa_name/aa_subtitle/...), so the per-field
    forbidden-word scan runs on the content this run actually produced."""
    g = result.get("generated") or {}
    return {
        "aa_name": g.get("name") or "",
        "aa_subtitle": g.get("subtitle") or "",
        "aa_summary": g.get("summary") or "",
        "aa_description": g.get("description") or "",
        "aa_highlights": g.get("highlights") or [],
        "aa_itineraries": g.get("itineraries") or "",
        "seo_title": g.get("seo_title") or "",
        "seo_meta": g.get("seo_meta") or "",
    }


async def _policy_review_detail(conn, tour_id, result) -> Optional[list]:
    """AA-1049: the policy review ``escalate_detail`` when this S1 result is held only by the elephant-riding policy
    (``elephant_policy.is_policy_only_hold`` over the result's page and the tour's raw source: the approve gate's
    own rule), else None."""
    from services.quality import elephant_policy, policy_review
    from services.tour_lifecycle import policy as lifecycle_policy

    if result.get("brand_audit_status") != "manual_check":
        return None
    src = await lifecycle_policy.source_row(conn, tour_id)
    source = elephant_policy.raw_source_from_row(src) if src else None
    generated = result.get("generated") or {}
    if not elephant_policy.is_policy_only_hold(result.get("brand_audit_status"), result.get("brand_audit_codes"),
                                               generated, source):
        return None
    hits = [{**h, "where": "version" if h["where"] == "generated" else h["where"]}
            for h in elephant_policy.scan(generated, source)]
    return policy_review.detail(hits)


async def _enqueue_review(conn, tour_id, generated_content_id, result, failure_summary=None) -> None:
    """AA-212: enqueue a blocked / low-quality tour into review_queue for HITL review.

    Idempotent: the NOT EXISTS guard skips re-insert when a pending row already exists for this
    generated_content_id (guards pipeline re-runs against double-enqueue). SF columns are left
    NULL — that NULL is what marks the admin/direct-export path picked up by approve_review.

    AA-653 (S207): a tour that already has a live version in Master Content enters the queue
    'dismissed', not 'pending'. Reviewers only work on tours with nothing published; a better
    version of a published tour comes from Regenerate, not from hand-editing a failed one. The
    row is still written (soft) so the failed attempt stays traceable.

    AA-1049 (tech lead decision S277, root ADR 0020 D8): except a version held ONLY by the elephant-riding policy
    (``_policy_review_detail``): it enters 'pending' even when the tour has a live or policy-held Master, marked as a
    policy review (``services/quality/policy_review.py``: ``escalate_detail`` evidence, summary prefix), because a
    person must review the tour before it is on sale. A row entered 'dismissed' never supersedes a pending policy
    review of the tour; a row entered 'pending' replaces every older pending row (one per tour).

    AA-986 L1: the same for a tour whose source is no longer live (trashed / superseded / deleted at S0/A0,
    for example an S1 run that finished after the trash): its row enters 'dismissed', never 'pending', so
    nobody can approve a deleted tour back into Master Content. `failure_summary` overrides the summary built
    from `result` (the S1 auto-export passes the source_not_live message: that version passed every gate).
    """
    from services.quality import policy_review

    policy_detail = await _policy_review_detail(conn, tour_id, result)
    summary = failure_summary if failure_summary is not None else _build_failure_summary(result)
    if policy_detail is not None:
        summary = policy_review.summary(summary)
    await conn.execute(f"""
        INSERT INTO silver_aa_internal.review_queue (
            tour_id, generated_content_id, tenant_id,
            failure_summary, score_overall, review_status, reviewed_at, escalate_detail
        )
        SELECT $1::uuid, $2::uuid, $3::uuid, $4, $5,
               (CASE WHEN NOT pub.source_live THEN 'dismissed'
                     WHEN $6::jsonb IS NOT NULL THEN 'pending'
                     WHEN pub.live THEN 'dismissed' ELSE 'pending' END)::review_status_enum,
               CASE WHEN NOT pub.source_live OR ($6::jsonb IS NULL AND pub.live) THEN NOW() END,
               $6::jsonb
        FROM (SELECT EXISTS (
                SELECT 1 FROM gold_aa_internal.published_tours pt
                WHERE pt.tour_id = $1::uuid AND pt.master_status <> 'trashed'
             ) AS live,
             {live_source_exists_sql("$1::uuid")} AS source_live) pub
        WHERE NOT EXISTS (
            SELECT 1 FROM silver_aa_internal.review_queue
            WHERE generated_content_id = $2::uuid
              AND review_status IN ('pending', 'dismissed')
        )
    """,
        tour_id,
        generated_content_id,
        _MASTER_TENANT_ID,
        summary,
        float(result.get("quality_score") or 0.0),
        json.dumps(policy_detail) if policy_detail is not None else None,
    )
    # AA-653 (S207): one failed version per tour is enough — the reviewer's move is Regenerate,
    # not comparing attempts. A newer version replaces older pending rows of the same tour.
    # AA-1049: only a row that entered pending replaces a pending policy review.
    await conn.execute(f"""
        UPDATE silver_aa_internal.review_queue rq
        SET review_status = 'superseded'::review_status_enum, reviewed_at = NOW()
        WHERE rq.tour_id = $1::uuid AND rq.review_status = 'pending'
          AND rq.generated_content_id <> $2::uuid
          AND (NOT {policy_review.row_is_policy_sql("rq")}
               OR EXISTS (SELECT 1 FROM silver_aa_internal.review_queue n
                           WHERE n.generated_content_id = $2::uuid AND n.review_status = 'pending'))
    """, tour_id, generated_content_id)


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


def _s1_prompt_version(result: dict) -> Optional[str]:
    """AA-1058 — generated_content.prompt_version of a new version: the writer stage's registered prompt version
    (prompt_registry, the value the gateway writes on the writer's llm_call_log row), else the writer's AA-289 prompt
    hash (metadata.prompt_version; the writer row carries the same hash as its fallback, call_log.billed_fields), so
    a version joins the cost of the prompt it was written with. None when the writer never ran. varchar(50)."""
    from shared.llm_client import prompt_registry
    stage = result.get("generate_stage") or "s1_generate"
    registered = prompt_registry.version_for(stage)
    value = registered or result.get("prompt_version") or None
    return str(value)[:50] if value else None


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
        # AA-645 (S249): which rubric scored it — absent = v1 (every version before S249). A v2 score is on another
        # scale (voice within the source) and is never compared with a v1 one as the same number.
        if result.get("judge_rubric"):
            metadata["judge"]["rubric"] = result["judge_rubric"]
            metadata["judge"]["unsupported"] = list(result.get("judge_unsupported") or [])[:12]
            metadata["judge"]["caps"] = list(result.get("judge_caps") or [])
    # AA-691: A1 grounding — what was found, what the sentence repair changed, what is still
    # unsupported (those sent the tour to manual_check) and the reviewer's soft notes. Omitted when
    # grounding did not run (T2, HITL branch, older rows) — same guard style as ``judge``.
    if result.get("grounding_ran"):
        metadata["grounding"] = {
            "found":           len(result.get("grounding_found") or []),
            "repaired_fields": result.get("grounding_repaired_fields") or [],
            "violations":      result.get("grounding_violations") or [],
            "notes":           result.get("grounding_notes") or [],
        }
    # AA-925 (Quality System v1): the shadow findings of the last validate (day oracle, S2 cut signature) and the
    # fail-open paths that ran; omitted when empty (same guard style as ``judge``).
    if result.get("quality_shadow"):
        metadata["quality_shadow"] = result["quality_shadow"]
    if result.get("degraded"):
        metadata["degraded"] = result["degraded"]
    # AA-645 step 0: which writer wrote the version ("v2" = S1 writer v2; absent = today's writer). On a v2 version
    # metadata.judge.brand_fit is report-only (it did not gate the version) — the per-wave report reads both.
    if result.get("writer_version"):
        metadata["writer_version"] = result["writer_version"]
    # S271 W0 (AA-900 l16): "hitl" = the draft left through the hitl exit (retries used up); since S271 that exit runs
    # the same checks (brand audit, grounding, day guard, degraded hold). Absent = the normal exit, or an older row.
    if result.get("exit_path"):
        metadata["exit_path"] = result["exit_path"]
    # AA-919 (WM-18 / WM-20): the writing-memory release the v2 writer wrote this version with (ids and numbers);
    # absent when memory is off for A1 or the v1 writer wrote it (no hook there).
    if result.get("writing_release"):
        metadata["writing_release"] = result["writing_release"]
    # AA-1049 W3 (S1 train): what the S1 control plane composed, routed and fixed (only when S1_CONTROL=contract).
    if result.get("s1_train"):
        metadata["s1_train"] = result["s1_train"]
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


def _final_contract_at_store(result: dict, generated: dict, status: str) -> Optional[dict]:
    """AA-1047 W1: the run's final-contract record as the version INSERT stores it (``metadata.final_contract``):
    the current gate's status and the stored text's sha stamped on it, one ``final_contract`` log event. None when
    the run has no record (a T2 result, a graph that failed before its last node)."""
    record = result.get("final_contract")
    if not isinstance(record, dict):
        return None
    stored = {**generated, "name": (generated.get("name") or "")[:500],
              "subtitle": (generated.get("subtitle") or "")[:500]}
    out = _fcon.stamp_stored(record, _fcon.tour_text_sha(stored), actual=status)
    _fcon.log_record(out, tour_id=result.get("tour_id"))
    return out


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
    # AA-1024 A2 (ADR 0009 decision 5): no model_tier. The run uses the s1_generate stage route; a body that sends a
    # model_tier value gets a 422 (api/core/model_tier.py). The AA-237 / AA-736 one-shot "sonnet" re-run below is
    # internal (_sonnet_upgrade), not a request field. A queued job payload that still carries the key is filtered by
    # s1_rewrite_job (only model_fields are read).
    subtitle_focus: str = "standard"
    brand_rules_version: Optional[int] = None
    brand_name: Optional[str] = None
    brand_identity_id: Optional[str] = None
    allow_auto_upgrade: bool = False  # AA-237: opt-in haiku→sonnet auto-upgrade (default OFF)
    # AA-916 WM-06 — Regenerate from the A1 review: the review row it answers and why (a reason-only signal).
    # Not part of the job payload.
    review_id: Optional[str] = None
    reason_code: Optional[ReasonCode] = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_model_tier(cls, data):          # AA-1024 A2 (api/core/model_tier.py)
        return refuse_model_tier(data)


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
    subtitle_focus: str = "standard"
    dry_run: bool = False
    tenant_id: str = "00000000-0000-0000-0000-000000000001"
    max_tours: int = 500

    @model_validator(mode="before")
    @classmethod
    def _refuse_model_tier(cls, data):          # AA-1024 A2: the route picks the model
        return refuse_model_tier(data)


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
    default_cta:       Optional[str] = None  # AA-771 (S233, migration 220)
    # AA-1080 — AA-800 signature words, stored in custom_validators; None (not sent) keeps the previous value.
    signature_words:   Optional[List[str]] = None


class CountryUpdateRequest(BaseModel):
    country: str


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
    # AA-700: prefer the 'default' row; a tenant who named their brand in the portal
    # (POST /admin/brand-identity keeps one active row per tenant under that name) has no
    # 'default' row, so fall back to the tenant's newest active row instead of None.
    return await conn.fetchrow(
        f"SELECT {_BRAND_RULE_COLS} FROM shared.tenant_brand_rules"
        " WHERE tenant_id = $1::uuid AND is_active = true"
        " ORDER BY (brand_name = 'default') DESC, version DESC"
        " LIMIT 1",
        tenant_uuid,
    )


async def _stamp_fact_sheet(conn, row, tenant_uuid, version_id) -> Optional[str]:
    """AA-1047 — store the S1 FactSheet of `row` (the raw_tours row) and set generated_content.fs_id; the fs_id or
    None (switched off, or the best-effort write failed)."""
    from services.quality import factsheet_store
    if not factsheet_store.enabled() or row is None:
        return None
    try:
        fs_id = await factsheet_store.save_tour_sheet(conn, row, tenant_id=tenant_uuid, workflow="s1",
                                                      tour_id=row.get("tour_id"))
        if fs_id:
            await factsheet_store.stamp_generated_content(conn, version_id, fs_id)
        return fs_id
    except Exception as e:  # noqa: BLE001 — the FactSheet record never fails the rewrite
        logger.warning("s1_factsheet_store_failed", version_id=str(version_id), error=str(e)[:200])
        return None


async def _version_of_job(conn, tour_id: str, job_id: str) -> Optional[str]:
    """AA-993 G2: the version an s1_rewrite job already wrote for this tour (its metadata.s1_job_id marker), or None.
    The tour_id filter uses the tour index; the containment test can use the metadata GIN index."""
    vid = await conn.fetchval(
        "SELECT id FROM silver_aa_internal.generated_content"
        " WHERE tour_id = $1::uuid AND metadata @> jsonb_build_object($2::text, $3::text)"
        " ORDER BY version_num DESC LIMIT 1",
        tour_id, _ckpt.JOB_MARKER_KEY, str(job_id),
    )
    return str(vid) if vid else None


# AA-993 G2 review: version INSERT errors that come from the content itself (class 22 data errors such as
# StringDataRightTruncation, NOT NULL / CHECK violations). The same saved rewrite would fail every time, so it is
# dropped; other errors (connection lost, a version_num race on the unique key) keep it for the next attempt.
_CONTENT_INSERT_ERRORS = (
    asyncpg.exceptions.DataError,
    asyncpg.exceptions.NotNullViolationError,
    asyncpg.exceptions.CheckViolationError,
)



def _derive_field_failures(gc: dict, codes: list, extra_forbidden=None) -> list:
    """AA-240: re-derive per-field failure reasons on the CURRENT gc content.

    `codes` = historical failure_codes + brand_audit_codes from quality_scores (snapshot at
    last validate). We re-measure the live field values so a code whose field a reviewer has
    since fixed is NOT surfaced (the UI must mark only fields that still fail right now).
    Multi-field codes (FORBIDDEN_WORD, MISSING_FIELD) are resolved by scanning live content.
    Import is lazy (mirrors build_revalidation_graph at call sites) to avoid pulling the
    LangGraph/boto graph module into router import time.
    """
    import json as _json
    from services.content_generation.forbidden_words import has_word
    from services.content_generation.graph import _CODE_FIELD_MAP, _VALIDATE_FORBIDDEN
    from services.content_generation.seo_meta_utils import (
        SEO_META_MIN, SEO_META_MAX, SEO_META_FORBIDDEN, SEO_TITLE_MAX, meta_complete_sentence,
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
    if "SEO_TITLE_TOO_LONG" in seen and len(title) > SEO_TITLE_MAX:
        add("seo_title", "SEO_TITLE_TOO_LONG", f"title {len(title)}>{SEO_TITLE_MAX}")
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
    # AA-738: also the brand's own words (extra_forbidden) and the body fields — the S217 Nepal
    # hits (explore/package/nestled) were brand words in aa_itineraries/aa_highlights, which this
    # scan used to miss, so the review reason could not name the field.
    if "FORBIDDEN_WORD" in seen:
        _words = list(dict.fromkeys(list(_VALIDATE_FORBIDDEN) + [
            w.lower().strip() for w in (extra_forbidden or []) if w and w.strip()]))
        for col in ("aa_name", "aa_subtitle", "aa_summary", "aa_description", "aa_highlights",
                    "aa_itineraries", "seo_title", "seo_meta"):
            val = gc.get(col)
            txt = (val if isinstance(val, str) else _json.dumps(val or "")).lower()
            hit = next((w for w in _words if has_word(txt, w)), None)
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



async def _record_review_reason(pool, review_id: str, action: str, reason_code: Optional[str],
                                actor_id: Optional[str]) -> None:
    """AA-916 WM-06 — a reason-only writing signal (no text) for an admin action on a review row: writer a1, or
    t2 for a tenant tour version's row; the row's tenant decides the scope (the AA master's rows are platform).
    Best-effort, after the action committed: a lost signal never fails the reject / dismiss / regenerate. The
    whole-version Jev write stays off (S245 scope change in grounding.record_rejected_outcome)."""
    if not reason_code:
        return
    from services.writing_memory import signals as wm
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tenant_id, tenant_tour_version_id FROM silver_aa_internal.review_queue WHERE id = $1::uuid",
                review_id)
            if row is None:
                return
            scope, tenant = wm.scope_for_tenant(row["tenant_id"])
            await wm.record_writer_feedback_safe(
                conn, writer="t2" if row["tenant_tour_version_id"] else "a1", scope=scope, tenant_id=tenant,
                reason_code=reason_code, action=action, actor_type="admin", actor_id=wm.admin_actor_id(actor_id),
                source_table="review_queue", source_id=review_id)
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning("aa916_signal_failed", action=action, review_id=review_id, error=str(exc)[:200])
