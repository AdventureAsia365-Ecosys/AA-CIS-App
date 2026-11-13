# api/routers/admin_pipeline/review.py — Review Queue routes: queue, claims, approve, reject, supersede, dismiss.
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import re
from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional
from api.routers.admin import verify_admin_secret
from api.routers.v1_pipeline import ReviewApproveBody
from shared.tour_liveness import live_source_exists_sql, live_source_sql
from services.writing_memory.reason_codes import ReasonCode  # AA-916 WM-06

from api.routers.admin_pipeline._shared import (
    _as_list, _derive_field_failures, _is_uuid, _low_quality_floor, _record_review_reason, _score_band_where, logger,
    router,
)


# ── Review queue (admin alias — no JWT required) ─────────────────────────────


_REVIEW_MAX_PAGE_SIZE = 200
_MANUAL_CHECK_REVIEW_CODES = {"FACT_CHECK_MANUAL_CHECK", "PRODUCT_TRUTH_RISK", "UNSUPPORTED_NUMBER",
                              "UNSUPPORTED_CLAIM"}


def _review_block(score, codes: list, failures: list, brand_audit_status) -> dict:
    """AA-739: the reason a review row is NOT on Master, as the headline the UI shows instead of the
    score. A high score can still be blocked (AA-736 hard codes are orthogonal to the score), so the
    reviewer must see WHY. Kinds: needs_human (fact check / unsupported claim — a person decides,
    e.g. elephant riding, chị Thư 08/10), hard (a hard-block code survived), low_quality (score <
    7), other. Presentation only — the gate itself (_is_publishable) is unchanged."""
    from services.content_generation.graph import _HARD_BLOCK_CODES
    cs = set(codes or [])
    manual = sorted(cs & _MANUAL_CHECK_REVIEW_CODES)
    if manual or brand_audit_status == "manual_check":
        why = {"FACT_CHECK_MANUAL_CHECK": "fact check (e.g. elephant riding)",
               "PRODUCT_TRUTH_RISK": "product-truth risk",
               "UNSUPPORTED_NUMBER": "number not in the source",
               "UNSUPPORTED_CLAIM": "claim not in the source"}
        label = ", ".join(why.get(m, m) for m in manual) or "manual check"
        return {"kind": "needs_human", "label": f"Needs a person: {label}"}
    hard = sorted(cs & set(_HARD_BLOCK_CODES))
    if hard:
        where = sorted({f"{f['code']} in {f['field']}" for f in (failures or []) if f.get("code") in hard})
        return {"kind": "hard", "label": "Blocked: " + (", ".join(where) or ", ".join(hard))}
    if score is not None and score < _low_quality_floor():
        return {"kind": "low_quality", "label": f"Low quality: score {score:.1f} < {_low_quality_floor():.1f}"}
    return {"kind": "other", "label": "Held for review"}


_TRANSIENT_RE = re.compile(
    r"timeout|timed out|throttl|ThrottlingException|TooManyRequests|\b429\b|"
    r"ServiceUnavailable|InternalServerError|Bad Gateway|Gateway Timeout|HTTP\s*5\d\d",
    re.IGNORECASE)


def _review_failure_class(score, codes: list, failures: list, brand_audit_status,
                          failure_summary, src_itinerary_chars) -> dict:
    """AA-728: per-row *actionability* of a held review row — can Regenerate (a retry on the same
    harness) actually fix it, and if not, what should the reviewer do instead? Distinct from
    `_review_block` (the headline label): this drives whether the UI shows the Regenerate button.

    First match wins, in this order:
      - `raw_insufficient` — the raw itinerary source is too thin for any rewrite to work.
        Not retryable; trash the tour or enrich the raw itinerary.
      - `needs_human` — a person must decide (fact check / unsupported claim, or brand audit
        manual_check — e.g. elephant riding, chị Thư's keep-and-review policy). Not retryable.
      - `writer_tone` — a Sonnet retry already failed (AA-736 'retried with sonnet' in the
        summary); a stronger model will not change a tone/word the brand forbids. Not retryable.
      - `transient` — the failure text looks like a timeout / throttle / 429 / 5xx; a plain retry
        is exactly the fix. Retryable.
      - `hard` — any other surviving hard-block code (incl. a FORBIDDEN_WORD not yet retried with
        Sonnet). Retryable (Regenerate escalates to Sonnet).
      - `low_quality` — score below the floor with no hard code. Retryable.
      - `other` — fallback. Retryable.
    """
    from services.content_generation.graph import _HARD_BLOCK_CODES
    cs = set(codes or [])
    summary = failure_summary or ""

    if src_itinerary_chars is not None and src_itinerary_chars < 500:
        return {"failure_class": "raw_insufficient", "retryable": False,
                "hint": "Raw source too thin — trash the tour or enrich the raw itinerary"}

    if (cs & _MANUAL_CHECK_REVIEW_CODES) or brand_audit_status == "manual_check":
        return {"failure_class": "needs_human", "retryable": False,
                "hint": "A person decides — edit, then Approve or Reject"}

    if "retried with sonnet" in summary.lower():
        return {"failure_class": "writer_tone", "retryable": False,
                "hint": "Sonnet already tried — edit by hand or Reject"}

    if _TRANSIENT_RE.search(summary):
        return {"failure_class": "transient", "retryable": True,
                "hint": "Transient model error — regenerate to retry"}

    if cs & set(_HARD_BLOCK_CODES):
        return {"failure_class": "hard", "retryable": True,
                "hint": "Regenerate with Sonnet"}

    if score is not None and score < _low_quality_floor():
        return {"failure_class": "low_quality", "retryable": True,
                "hint": "Low quality — regenerate for a better draft"}

    return {"failure_class": "other", "retryable": True,
            "hint": "Regenerate to try again"}


@router.get("/review-queue")
async def admin_review_queue(
    request: Request,
    x_admin_secret: str = Header(None),
    page: int = 1,
    page_size: int = 20,
    status: str = "pending",
    country: Optional[str] = None,
    score: str = "all",
    tour_id: Optional[str] = None,
    policy: Optional[str] = None,
):
    """AA-739: server-side paging + filters (country, score band, tour) and full facets, so the
    page no longer paginates/filters client-side over a 20-row slice (it showed 20 of 54 and only
    the countries on that slice).

    AA-1049: ``policy=elephant_riding`` keeps only the elephant-riding policy reviews
    (services/quality/policy_review.py); every row carries ``policy_review`` (null, or {kind, held, approvable,
    evidence}) and the facets count them (``facets.policy_reviews``)."""
    from services.quality import approve_gate, policy_review
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    tenant_id = "00000000-0000-0000-0000-000000000001"
    page = max(1, page)
    page_size = max(1, min(page_size, _REVIEW_MAX_PAGE_SIZE))
    offset = (page - 1) * page_size

    # Shared WHERE for rows, total and facets. $1 = tenant; further params appended in order.
    params: list = [tenant_id]
    # AA-986 L1: a pending row whose source is no longer live (trashed / superseded / deleted at S0/A0) is
    # hidden: it cannot be approved (409 source_not_live). Closed rows stay, with `source_live` on each.
    where = ["rq.tenant_id = $1::uuid", f"(rq.review_status <> 'pending' OR ({live_source_sql('rt')}))"]
    if status == "all":
        # AA-626: hide 'dismissed' from the "all" view too, same as 'superseded' — both are
        # closed-out-without-a-quality-verdict rows the reviewer intentionally cleared.
        where.append("rq.review_status NOT IN ('superseded', 'dismissed')")
    else:
        params.append(status)
        where.append(f"rq.review_status = ${len(params)}")
    facet_where = list(where)          # facets ignore the country/score/tour filters themselves
    facet_params = list(params)
    if country and country != "all":
        params.append(country)
        where.append(f"rt.country = ${len(params)}")
    band_sql = _score_band_where(score)
    if band_sql:
        where.append(band_sql)
    if tour_id and _is_uuid(tour_id):
        params.append(tour_id)
        where.append(f"rq.tour_id = ${len(params)}::uuid")
    if policy == policy_review.FILTER_VALUE:
        where.append(policy_review.row_is_policy_sql("rq"))
    where_sql = " AND ".join(where)

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
                   rt.src_name, rt.country, rt.duration, rt.batch_id AS raw_tours_batch_id,
                   length(rt.src_itineraries) AS src_itinerary_chars,
                   ({live_source_sql('rt')}) AS source_live,
                   rq.escalate_detail::text AS escalate_detail,
                   ({policy_review.row_is_policy_sql('rq')}) AS is_policy_review,
                   (pt.master_status::text = 'inactive' AND pt.master_status_reason = 'policy_hold') AS policy_held,
                   (pt.generated_content_id = rq.generated_content_id) AS is_master_version
            FROM silver_aa_internal.review_queue rq
            JOIN silver_aa_internal.generated_content gc ON gc.id = rq.generated_content_id
            -- AA-739: latest evaluation only; a plain LEFT JOIN repeated a row per evaluation.
            LEFT JOIN LATERAL (
                SELECT q.failure_codes, q.brand_audit_codes, q.brand_audit_status
                FROM silver_aa_internal.quality_scores q
                WHERE q.generated_content_id = rq.generated_content_id
                ORDER BY q.evaluated_at DESC LIMIT 1
            ) qs ON TRUE
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = rq.tour_id
            LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rq.tour_id
            WHERE {where_sql}
            ORDER BY rq.created_at DESC
            LIMIT {page_size} OFFSET {offset}
        """, *params)
        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM silver_aa_internal.review_queue rq
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = rq.tour_id
            WHERE {where_sql}
        """, *params)
        facet_rows = await conn.fetch(f"""
            SELECT rt.country, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE {policy_review.row_is_policy_sql("rq")}) AS policy_n
            FROM silver_aa_internal.review_queue rq
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = rq.tour_id
            WHERE {" AND ".join(facet_where)}
            GROUP BY rt.country ORDER BY rt.country
        """, *facet_params)
        # AA-1049: can a policy row be approved as it is? The held Master's own version: yes (the approve puts back
        # what was on sale); another version: the approve gate's own answer (v1_pipeline._APPROVE_GATE_SQL).
        policy_gate: dict = {}
        from api.routers.v1_pipeline import _APPROVE_GATE_SQL
        gate_sql = _APPROVE_GATE_SQL.format(live=live_source_exists_sql("rq.tour_id"))
        for r in rows:
            if r.get("is_policy_review") and r["review_status"] == "pending" and not (
                    r.get("policy_held") and r.get("is_master_version")):
                g = await conn.fetchrow(gate_sql, r["id"], tenant_id)
                policy_gate[str(r["id"])] = bool(g) and (
                    r["revalidate_passed"] is True if g["human_edited"] else not approve_gate.blockers(g))
        # AA-739 (from AA-738): mark the brand's own forbidden words too, not only the AA core list.
        brand_words = await conn.fetchval("""
            SELECT forbidden_words FROM shared.tenant_brand_rules
            WHERE tenant_id = $1::uuid AND is_active
            ORDER BY (brand_name = 'default') DESC, version DESC LIMIT 1
        """, tenant_id)
    brand_words = _as_list(brand_words)

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
        _failures = _derive_field_failures(gc, codes, brand_words)
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
            "failures":           _failures,
            # AA-739: why this row is not on Master — the UI headline, ahead of the score
            "block":              _review_block(
                float(r["score_overall"]) if r["score_overall"] is not None else None,
                codes, _failures, r["brand_audit_status"]),
            # AA-728: whether Regenerate (a same-harness retry) can fix this row, and the hint for
            # what to do when it cannot. Drives the FE Regenerate button + bulk skip.
            **_review_failure_class(
                float(r["score_overall"]) if r["score_overall"] is not None else None,
                codes, _failures, r["brand_audit_status"],
                r["failure_summary"], r["src_itinerary_chars"]),
            # AA-242 regenerate context: tier the caller originally requested (may be null),
            # and the raw-tour batch so the FE can pass it back through run-tour-async
            "requested_tier":     r["requested_tier"],
            "raw_tours_batch_id": str(r["raw_tours_batch_id"]) if r["raw_tours_batch_id"] else None,
            # gc.status = publishable gate (_is_publishable → 'approved' | 'hitl'); FE keys
            # the supersede decision on this, never on a client-side score heuristic
            "status":             r["status"],
            # raw-tour context
            # AA-986 L1: false when the source was trashed / superseded / deleted (only closed rows can be false)
            "source_live":        bool(r["source_live"]),
            "src_name":           r["src_name"],
            "country":            r["country"],
            "duration":           r["duration"],
            # AA-1049: the elephant-riding policy review (null for any other row)
            "policy_review":      _policy_review_view(r, policy_gate) if r.get("is_policy_review") else None,
        })

    return {
        "data": data,
        "pagination": {"page": page, "page_size": page_size, "total": total},
        # AA-739: every country present for this status, regardless of page/filters.
        "facets": {"countries": [{"country": f["country"], "n": f["n"]} for f in facet_rows
                                 if f["country"]],
                   "policy_reviews": sum(int(f.get("policy_n") or 0) for f in facet_rows)},
    }


def _policy_review_view(r, policy_gate: dict) -> dict:
    """AA-1049: the Review Queue's view of an elephant-riding policy review row."""
    from services.quality import policy_review
    held_version = bool(r.get("policy_held")) and bool(r.get("is_master_version"))
    return {
        "kind": policy_review.FILTER_VALUE,
        # the tour's Master is held (off sale) until this review is approved
        "held": bool(r.get("policy_held")),
        "approvable": r["review_status"] == "pending" and (held_version or policy_gate.get(str(r["id"]), False)),
        "evidence": policy_review.evidence(r.get("escalate_detail")),
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
    body: Optional[ReviewApproveBody] = None,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    from api.routers.v1_pipeline import approve_review
    # S271 W0: the body (optional) carries an override reason for an unedited version the publish checks refuse.
    result = await approve_review(review_id=review_id, request=request, tenant=None, body=body)
    # AA-756 — best-effort: after the approve commits, stamp the ground truth for grounding. Every
    # grounded sentence of the approved master version is now human-confirmed as supported, so
    # a1_claim_supported truth=True for each. Never block or fail the approve on this.
    if result.get("status") == "approved":
        try:
            await _record_grounding_approved_outcome(request.app.state.pool, review_id)
        except Exception:
            logger.warning("approve_grounding_outcome_failed", review_id=review_id, exc_info=True)
    return result


# AA-756 — the raw source fields that _execute_run_tour builds the grounding `tour` dict from, in
# the same shape (so source_text() reproduces the exact `source` the S1 run judged against).
# AA-845: also the review's version id / status, read by the per-sentence claims panel.
_GROUNDING_OUTCOME_SQL = """
    SELECT rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights,
           rt.src_itineraries, rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions,
           gc.aa_subtitle, gc.aa_summary, gc.aa_highlights, gc.aa_itineraries,
           rq.tour_id::text AS tour_id, gc.id::text AS generated_content_id,
           rq.review_status::text AS review_status, gc.human_edited
    FROM silver_aa_internal.review_queue rq
    JOIN silver_aa_internal.generated_content gc ON gc.id = rq.generated_content_id
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = rq.tour_id
    WHERE rq.id = $1::uuid
"""


async def _record_grounding_approved_outcome(pool, review_id: str) -> None:
    """Build the raw `tour` dict (SOURCE_FIELDS shape) and the approved grounded fields, then stamp
    a1_claim_supported truth=True per sentence (grounding.record_approved_outcome). Best-effort."""
    from services.content_generation import grounding as gr

    row = await pool.fetchrow(_GROUNDING_OUTCOME_SQL, review_id)
    if row is None:
        return
    await gr.record_approved_outcome(pool, gr.tour_from_row(row), gr.generated_from_row(row))


# ── AA-845 — reviewer per-sentence "unsupported" (the a1_claim_supported ground-truth loop) ─────────
# A Review reject scores the whole version, so it writes no outcome (S245 scope change). Instead the
# reviewer marks the sentences that are not in the source; each mark stamps truth=False on exactly
# the decision_log rows the S1 grounding run wrote for that sentence (same subject key builder as the
# approve hook: grounding.review_units).

async def _review_claim_units(pool, review_id: str):
    """(row, units) for a review, or HTTP 404. Units carry field/day/sentence/subject_key/index."""
    from services.content_generation import grounding as gr

    if not _is_uuid(review_id):
        raise HTTPException(status_code=404, detail="review not found")
    row = await pool.fetchrow(_GROUNDING_OUTCOME_SQL, review_id)
    if row is None:
        raise HTTPException(status_code=404, detail="review not found")
    return row, gr.review_units(gr.tour_from_row(row), gr.generated_from_row(row))


def _claim_label(outcome: Optional[dict]) -> Optional[dict]:
    if not outcome:
        return None
    return {"truth": outcome.get("truth"), "source": outcome.get("source"), "at": outcome.get("at"),
            "by": outcome.get("by")}


@router.get("/review-queue/{review_id}/claims",
            summary="The review's grounded sentences, each with its Jev verdict and label")
async def admin_review_claims(review_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.content_generation import grounding as gr

    pool = request.app.state.pool
    row, units = await _review_claim_units(pool, review_id)
    verdicts = await gr.latest_verdicts(pool, [u["subject_key"] for u in units],
                                        (gr.QUESTION, gr.PROMISE_QUESTION))
    claims = []
    for u in units:
        v = verdicts.get((gr.QUESTION, u["subject_key"]))
        promise = verdicts.get((gr.PROMISE_QUESTION, u["subject_key"]))
        label = _claim_label(v.get("outcome") if v else None)
        claims.append({
            "index": u["index"], "field": u["field"], "day": u["day"], "sentence": u["sentence"],
            "subject_key": u["subject_key"],
            "verdict": None if v is None else {
                "zone": v["zone"], "mode": v["mode"], "probability": v["probability"],
                "at": v["created_at"].isoformat() if v.get("created_at") else None,
            },
            "promise_probability": promise["probability"] if promise else None,
            "label": label,
            "marked_unsupported": bool(label and label["source"] == gr.REVIEW_UNSUPPORTED_SOURCE),
        })
    return {
        "review_id": review_id, "tour_id": row["tour_id"],
        "generated_content_id": row["generated_content_id"], "review_status": row["review_status"],
        "human_edited": bool(row["human_edited"]), "question_key": gr.QUESTION,
        "claims": claims,
        "with_verdict": sum(1 for c in claims if c["verdict"] is not None),
        "marked_unsupported": sum(1 for c in claims if c["marked_unsupported"]),
    }


class ClaimLabelBody(BaseModel):
    subject_key: str = Field(..., min_length=1, max_length=64)
    unsupported: bool = True


@router.post("/review-queue/{review_id}/claims/label",
             summary="Mark (or unmark) one sentence as not supported by the source")
async def admin_label_review_claim(review_id: str, body: ClaimLabelBody, request: Request,
                                   x_admin_secret: str = Header(None),
                                   x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.content_generation import grounding as gr

    pool = request.app.state.pool
    _, units = await _review_claim_units(pool, review_id)
    unit = next((u for u in units if u["subject_key"] == body.subject_key), None)
    if unit is None:
        raise HTTPException(status_code=404, detail="sentence not in this review's current version")
    verdicts = await gr.latest_verdicts(pool, [body.subject_key])
    if (gr.QUESTION, body.subject_key) not in verdicts:
        raise HTTPException(status_code=409, detail=(
            "No Jev verdict is logged for this sentence (edited after the source check, or aged out), "
            "so there is nothing to label."))
    reviewer = x_admin_user_id if x_admin_user_id and _is_uuid(x_admin_user_id) else None
    if body.unsupported:
        updated = await gr.record_rejected_outcome(pool, body.subject_key, ref=review_id, by=reviewer)
    else:
        updated = await gr.clear_rejected_outcome(pool, body.subject_key)
    logger.info("review_claim_labelled", review_id=review_id, subject_key=body.subject_key,
                unsupported=body.unsupported, rows=updated, admin_user=reviewer)
    return {"review_id": review_id, "subject_key": body.subject_key, "unsupported": body.unsupported,
            "rows_updated": updated}


class ReviewReasonBody(BaseModel):
    """AA-916 WM-06 — optional body of reject / dismiss: why (one-click chip). An unknown code is a 422."""
    reason_code: Optional[ReasonCode] = None


@router.post("/review-queue/{review_id}/reject")
async def admin_reject_review(
    review_id: str,
    request: Request,
    body: Optional[ReviewReasonBody] = None,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    from api.routers.v1_pipeline import reject_review
    out = await reject_review(review_id=review_id, request=request, tenant=None)
    await _record_review_reason(request.app.state.pool, review_id, "reject", body.reason_code if body else None,
                                x_admin_user_id)
    return out


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
    body: Optional[ReviewReasonBody] = None,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
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
    await _record_review_reason(pool, review_id, "dismiss", body.reason_code if body else None, x_admin_user_id)
    return {"status": "dismissed", "review_id": review_id}
