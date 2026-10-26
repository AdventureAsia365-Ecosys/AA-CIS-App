"""AA-652 — `t2_rewrite` job kind: a tenant's T2 rewrite of one published tour.

Moved out of `api/routers/v1_tours.py::trigger_rewrite()`, where it ran as an in-process asyncio
task. A deploy or restart killed it and the version stayed 'pending', so the portal showed
"Writing…" forever. The router now creates the pending version, enqueues this job and returns;
the job does the SEO lookup, the rewrite (`_rewrite_tour`), the T3 QA gate and the save, with the
same live-writing progress as before (ADR 0004, Redis via the worker's resources).

Payload: {"version_id", "tenant_id", "published_tour_id", "rewrite_language"}

Retries: a failed attempt is retried once. When the job ends without success (attempts used,
cancelled), `on_terminal` marks the version 'failed' so the portal shows Failed + Retry.
"""
from __future__ import annotations

import structlog

from services.acp_shared.writing_progress import (
    TOUR_STAGE_STEPS, TOUR_STEPS, WritingProgress, progress_fail, progress_show, progress_step,
)
from shared.jobs.registry import JobContext, NonRetryable, job_kind
from shared.llm_client import stream_sink
from services.quality.registry import threshold

logger = structlog.get_logger()

KIND = "t2_rewrite"

_PT_SQL = """
    SELECT pt.id, pt.tour_id, pt.aa_name, pt.aa_subtitle,
           pt.aa_summary, pt.aa_description, pt.aa_highlights,
           pt.aa_itineraries, pt.seo_title, pt.seo_meta,
           pt.seo_keywords_used,
           rt.country, rt.duration
    FROM gold_aa_internal.published_tours pt
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    WHERE pt.id = $1::uuid
"""


_T2_STATUS_PASS = threshold("t2.status_pass")  # services/quality/thresholds.yaml (AA-926)


def _t2_status(rewrite_score: float) -> str:
    """AA-436: the stored T2 version status is purely score-based (same formula for a real T3 pass and
    an auto-pass). AA-926: pure seam so the pass line is pinned by the gate-outcome snapshot."""
    if rewrite_score >= _T2_STATUS_PASS:
        return "ai_generated"   # ready for tenant to review
    if rewrite_score > 0:
        return "needs_review"   # LLM finished but low quality
    return "needs_review"       # hitl / score=0 — needs human


def source_tour_dict(pt) -> dict:
    """The T2 writer's source (a `_PT_SQL` row) as the dict `_rewrite_tour` and the T3 gate read.
    AA-802 — also the source the admin sentence edit on a tenant version checks against
    (admin_content_quality), so the two never drift."""
    return {
        "name":        pt["aa_name"],
        "subtitle":    pt.get("aa_subtitle") or "",
        "summary":     pt.get("aa_summary") or "",
        "description": pt.get("aa_description") or "",
        "highlights":  pt.get("aa_highlights") or "",
        "itineraries": pt.get("aa_itineraries") or "",
        "seo_title":   pt.get("seo_title") or "",    # fixed: was "aa_seo_title"
        "seo_meta":    pt.get("seo_meta") or "",     # fixed: was "aa_seo_meta"
        "country":     pt.get("country") or "",       # now from raw_tours JOIN
        "duration":    pt.get("duration") or "",      # now from raw_tours JOIN
    }


def brand_rules_from_pack(pack: dict, rewrite_language: str = "EN-US") -> dict:
    """The T2 `brand_rules` dict (the keys _rewrite_tour maps into the graph's brand_* state) from an
    AA-771 brand pack. AA-800: also what T3 and the admin T2 edit check read (signature words)."""
    return {
        "system_prompt":    pack.get("system_prompt") or "",
        "style_guide":      pack.get("style_guide") or "",
        "forbidden_words":  list(pack.get("forbidden_words") or []),
        "rewrite_language": rewrite_language,
        # AA-612a: brand-diff fields — the keys _rewrite_tour maps into the graph's
        # brand_* state (v1_pipeline.py), which the judge brand-fit gate reads.
        "core_idea":        pack.get("core_idea") or "",
        "customer_segment": pack.get("customer_segment") or "",
        "customer_mindset": pack.get("customer_mindset") or "",
        "voice_examples":   list(pack.get("voice") or []),
        "good_examples":    pack.get("good_examples") or "",
        # AA-792: the brand's identity for the T2 CLIENT BRAND block. default_cta is carried
        # (tenant rubric for the audit fallback) but not written into the tour: a tour has no
        # CTA field.
        "brand_name":       pack.get("brand_name") or "",
        "brand_type":       pack.get("brand_type") or "",
        "default_cta":      pack.get("default_cta") or "",
        # AA-800: the explicit signature words (custom_validators); the rest is read from the T0 text.
        "signature_words":  list(pack.get("signature_words") or []),
        "_brand_loaded":    True,
    }


async def _prepare(pool, pt, tenant_id: str, rewrite_language: str) -> tuple[dict, dict]:
    """tour_dict + brand_rules, unchanged from the pre-AA-652 router code."""
    tour_dict = source_tour_dict(pt)

    # Fetch brand rules for this tenant
    # AA-425 fix: `br_row = await pool.acquire().__aenter__()` used to sit right before the
    # `async with pool.acquire() as _conn2` below -- it acquired a SECOND connection from the
    # pool and called __aenter__() directly without ever pairing it with __aexit__(), leaking
    # one pooled connection on every single tenant rewrite (br_row itself was never even used
    # afterward -- dead code). Pool is min_size=2/max_size=10 (api/main.py) -- found live during
    # AA-425 verification: after ~8 real rewrite calls in one session the pool was exhausted
    # enough that THIS query started timing out, silently falling into the except below
    # (brand_rules = {} minus system_prompt/style_guide/forbidden_words) -- the LLM then had no
    # brand voice guidance at all and reliably drifted into generic marketing adjectives that
    # happen to be on graph.py's own forbidden-word list, escalating every rewrite to
    # review_queue. Removed the leaked acquire; only the real query below remains.
    # AA-741 pre-audit: brand_rules ALWAYS carries rewrite_language (the no-row path used to drop
    # it), and a DB error is no longer swallowed into an unbranded rewrite (the AA-425 failure
    # mode: job "succeeded" with no voice and no forbidden words). It raises so the job retries;
    # after the last attempt on_terminal marks the version failed.
    brand_rules = {"rewrite_language": rewrite_language, "_brand_loaded": False}
    # AA-792: one brand loader for every tenant writer — the AA-771 brand pack (T8/T9/T10 already
    # use it). Before, this was its own SELECT (newest active version, not 'default'-first) that never
    # read brand_name / brand_type / default_cta, so the T2 writer could not name the brand it writes
    # as. The pack also parses the jsonb lists (asyncpg has no jsonb codec here: AA-425/AA-612a —
    # forbidden_words used to be split into characters). A DB error still raises (AA-741).
    from services.acp_shared.brand_pack import fetch_brand_pack_conn
    async with pool.acquire() as _conn2:
        pack = await fetch_brand_pack_conn(_conn2, tenant_id)
    if pack:
        brand_rules = brand_rules_from_pack(pack, rewrite_language)
    return tour_dict, brand_rules


async def t2_seo(pool, pt, tenant_id: str) -> tuple[dict, dict]:
    """(seo_data, diag): the SEO the T2 rewrite passes to the graph, read from the caches only (moved out of
    `_rewrite_and_save`, AA-919, so the WM-20 release-0 replay rebuilds the same T2 prompt)."""
    # AA-445-02 — T2 passes the tour's SEO context to the shared rewrite graph (it used to reach it
    # as {}). AA-646 follow-up (KAN-90, 29/09/2026): a tenant action never buys DataForSEO, so T2
    # only READS the cached seo_context row; it no longer calls process_seo() on a miss (~$0.18
    # per tour, outside every spend budget). A tour with no row is rewritten without SEO keywords
    # and logged (`t2_seo_context_missing`) so admin research can cover it.
    seo_data: dict = {}
    # AA-741 pre-audit: which SEO the rewrite actually used, returned into job.result (a lookup
    # failure used to be a log line only — the rewrite silently ran keyword-less).
    diag = {"seo_source": "none", "market": None}
    try:
        async with pool.acquire() as _conn_seo:
            _existing = await _conn_seo.fetchrow("""
                SELECT top_keywords, keyword_ideas, people_also_ask
                FROM silver_aa_internal.seo_context
                WHERE tour_id = $1::uuid
                ORDER BY fetched_at DESC LIMIT 1
            """, pt["tour_id"])
        if _existing:
            import json as _json_seo
            _tk = _existing["top_keywords"]
            seo_data = {
                "top_keywords": (_json_seo.loads(_tk) if isinstance(_tk, str) else _tk) or [],
            }
            seo_data["keywords"] = {"top_keywords": seo_data["top_keywords"]}
            _paa = _existing["people_also_ask"]
            seo_data["people_also_ask"] = (_json_seo.loads(_paa) if isinstance(_paa, str) else _paa) or []
        # AA-707 — the admin row above is US-market. Prefer keywords + PAA for the tenant's own
        # market from the research cache (search_demand); keep the admin row only as a fallback.
        from services.seo_intelligence.s1_prefetch import tenant_market_seo
        async with pool.acquire() as _conn_mkt:
            _market, _mkt_seo = await tenant_market_seo(_conn_mkt, tenant_id, str(pt["tour_id"]))
        diag["market"] = _market
        if _mkt_seo:
            seo_data = _mkt_seo
            diag["seo_source"] = "tenant_market"
        else:
            if seo_data:
                diag["seo_source"] = "admin_us_fallback"
            import structlog as _sl_mkt
            _sl_mkt.get_logger().info("t2_market_seo_fallback", tour_id=pt["tour_id"],
                                      tenant_id=tenant_id, market=_market,
                                      admin_row=bool(_existing))
        if not seo_data:
            import structlog as _sl_seo
            _sl_seo.get_logger().info("t2_seo_context_missing", tour_id=pt["tour_id"],
                                      tenant_id=tenant_id)
    except Exception as _seo_err:
        import structlog as _sl_seo
        _sl_seo.get_logger().warning("t2_seo_step_failed", tour_id=pt["tour_id"], error=str(_seo_err))
        diag["seo_source"] = "error"
        diag["seo_error"] = str(_seo_err)[:300]
    return seo_data, diag


async def _rewrite_and_save(pool, pt, tour_dict: dict, brand_rules: dict, tenant_id: str,
                            version_id: str, published_tour_id: str) -> dict:
    """SEO lookup, rewrite, T3 QA gate, save — unchanged from the pre-AA-652 router closure
    except that a failure raises (so the job retries) instead of silently marking the version."""
    from api.routers.v1_pipeline import _rewrite_tour as _do_rewrite

    seo_data, diag = await t2_seo(pool, pt, tenant_id)
    # AA-919 (WM-18 / WM-20): the T2 writing-memory release of this tenant (platform + tenant pointer; None = memory
    # off, fails open). Every T2 write (the first and the T3 repair rounds) goes through build_t2_system_prompt, which
    # consumes it; the job result records it (`writing_release`). Release 0 changes no prompt byte.
    from services.writing_memory.insertion import resolve as _wm_resolve, stamp as _wm_stamp
    writing_memory = await _wm_resolve(pool, "t2", tenant_id)
    if writing_memory:
        diag["writing_release"] = _wm_stamp(writing_memory)

    try:
        result = await _do_rewrite(
            tour_dict, idx=0, total=1,
            brand_rules=brand_rules,
            seo=seo_data,
            is_tenant_rewrite=True,  # skips name-match check in validate_node
            tenant_id=tenant_id,             # AA-620: log this tenant in llm_call_log
            generate_stage="t2_generate",    # AA-620: tenant writer stage (admin-tunable)
            writing_memory=writing_memory,   # AA-919
        )
        if result.get("status") == "success" and result.get("generated"):
            # AA-425 T3 — QA gate (grounding + structural), self-repair up to
            # TENANT_QA_MAX_REPAIRS rounds. source_texts is T2's OWN input (the
            # pre-rewrite published_tours content) — the grounding baseline a
            # tenant rewrite must not introduce new numbers/measurements beyond.
            from services.acp_produce.tenant_pipeline import run_t3_qa_gate
            source_texts = [str(v) for v in tour_dict.values() if v]
            progress_step("check")  # AA-637
            qa = await run_t3_qa_gate(
                tour_dict, source_texts, result, brand_rules,
                seo_data=seo_data,  # AA-445-02 — repair-round rewrites also carry seo_data
                tenant_id=tenant_id,  # AA-620 — repair-round logs t2_generate + this tenant
                writing_memory=writing_memory,  # AA-919 — the repair rounds write with the same release
            )
            result = qa["result"]  # possibly a later repair round's output
            # AA-793: which draft was kept and every draft's score, in job.result — the rerun's
            # evidence that no saved draft scores below an earlier one.
            diag["t3"] = {
                "chosen_attempt": qa.get("chosen_attempt"), "chosen_via": qa.get("chosen_via"),
                "drafts": qa.get("drafts") or [], "sentence_repairs": qa.get("sentence_repairs", 0),
                "background_cleared": qa.get("background_cleared", 0),
                "graph_draft_scores": result.get("draft_scores") or [],
                "best_draft_restored": bool(result.get("best_draft_restored")),
                # AA-925: T3's fail-open paths (Jev outages while clearing hits, repair-contract reverts) —
                # job.result is where they are read; the log event alone was invisible outside CloudWatch.
                "degraded": qa.get("degraded") or [],
            }

            import json as _j3
            gen = result["generated"]
            rewritten = {
                "name":        gen.get("name", tour_dict["name"]),
                "subtitle":    gen.get("subtitle", ""),
                "summary":     gen.get("summary", ""),
                "highlights":  gen.get("highlights", []),
                "itineraries": gen.get("itineraries", tour_dict.get("itineraries", "")),
                "seo_title":   gen.get("seo_title", ""),
                "seo_meta":    gen.get("seo_meta", ""),
                "trip_type":   gen.get("trip_type", ""),
                "status":      "done",
            }
            # AA-778 (S238): no em-dash in tenant copy. The last text step: after the writer, judge,
            # flag-fix and T3 (which all read the draft as written), right before the save. T2 only:
            # this job is the one writer of AI-generated tenant_tour_versions; A1 never runs here.
            from services.content_generation.t2_dashes import em_dash_count, fix_t2_dashes
            dashes_before = em_dash_count(rewritten)
            rewritten, dash_log = fix_t2_dashes(rewritten, tour_dict)
            diag["em_dashes"] = {"before": dashes_before, "after": em_dash_count(rewritten), "fields": dash_log}
            rewrite_score = float(result.get("quality_score") or 0)
            # AA-436: status is now purely score-based, same formula for a real T3 pass
            # and an auto-pass — T3 no longer forces needs_review on its own (see
            # qa_auto_passed below for the separate signal that a QA-gate failure
            # happened). ADR-2026-038 §0.1 (amend §10.3): escalate-and-stop broke the
            # single-job T2->T3->T5 chain and made the tenant fix wording themselves —
            # both rejected 22/08.
            new_status = _t2_status(rewrite_score)
            # Never write 0.0 — fall back to source published_tours quality_score
            async with pool.acquire() as _conn_qs:
                source_score = await _conn_qs.fetchval(
                    "SELECT quality_score FROM gold_aa_internal.published_tours WHERE id = $1::uuid",
                    published_tour_id
                )
            final_score = rewrite_score if rewrite_score else float(source_score or 0)
            # qa_status keeps its migration-107 meaning unchanged (the real QA verdict,
            # 'escalated' still means the gate did NOT actually clear) — qa_auto_passed
            # (migration 109) is the new, separate tenant-facing badge flag: true means
            # this version reached the pool despite qa_status='escalated'.
            qa_status = "passed" if qa["passed"] else "escalated"
            qa_auto_passed = not qa["passed"]
            # AA-668: the live view's final text is the saved version (T3 may keep an earlier draft or
            # repair sentences, and the em-dash fix runs after the last streamed draft).
            progress_show(rewritten)
            progress_step("save")  # AA-637
            async with pool.acquire() as _conn3:
                await _conn3.execute("""
                    UPDATE gold_aa_internal.tenant_tour_versions
                    SET rewritten_content = $1::jsonb,
                        status = $2,
                        quality_score = $3,
                        qa_status = $4,
                        qa_repair_count = $5,
                        qa_checked_at = now(),
                        qa_auto_passed = $6
                    WHERE id = $7::uuid
                """,
                    _j3.dumps(rewritten), new_status, final_score,
                    qa_status, qa["attempts"], qa_auto_passed, version_id)
            import structlog as _sl2
            _sl2.get_logger().info("tenant_rewrite_done",
                version_id=str(version_id), score=final_score, status=new_status,
                qa_status=qa_status, qa_attempts=qa["attempts"],
                qa_auto_passed=qa_auto_passed)

            if not qa["passed"]:
                # AA-436: T3 no longer escalate-BLOCKS — still write the review_queue
                # row exactly as AA-425 did (escalate_t3_failure() itself unchanged), so
                # A4 (AA-437, separate issue) can see it — the tour still reaches the
                # tenant's pool (T4) either way.
                from services.acp_produce.tenant_pipeline import escalate_t3_failure
                await escalate_t3_failure(
                    pool, tenant_id, pt["tour_id"], str(version_id),
                    qa["structural_issues"], qa["grounding_issues"],
                )

            # AA-526 (04/09/2026 architecture decision) — atomize (T5) no longer runs on
            # tenant-rewritten content at all, here or via the (now-removed) standalone
            # POST /v1/tours/versions/{version_id}/atomize AA-469 Việc 1 introduced. Atoms
            # for this tour already exist (owner_scope='platform') by the time it's even
            # visible in Browse Pool — atomize now runs once, at A3 (services/export/
            # handler.py::process_export(), right after the tour is published), not
            # per-tenant, not on every rewrite. See services/acp_produce/tenant_pipeline.py's
            # own module docstring + docs/implementation-notes/AA-526.md.
            #
            # AA-545 — Segment-matching + ranking + route-detection (AA-509/510/515) ALSO
            # moved to A3 (`services/export/handler.py::_run_a3_atomize_background()`),
            # right after atomize itself, platform-wide — not per-tenant-rewrite anymore
            # (AA-526's own note above, kept for history, is now superseded: Segment/Route/
            # Ranking are the single global set A3 already computes once for everyone; see
            # docs/implementation-notes/AA-545.md).
            #
            # AA-646 — `run_segment_research()` (the DataForSEO purchase) no longer fires here.
            # After AA-545 it swept every platform place per tenant rewrite ($49 on 25/09/2026).
            # A tenant action never buys DFS; research is admin-triggered and scoped
            # (`POST /admin/segment-research/run`, api/routers/admin_segment_research.py).
        else:
            # Before AA-652 this case wrote nothing and left the version 'pending' forever
            # (the portal then showed "Writing…" forever). Raise so the job retries, and
            # after the last attempt on_terminal marks the version 'failed'.
            raise RuntimeError(
                f"rewrite did not succeed: status={result.get('status')!r} "
                f"error={str(result.get('error'))[:300]!r}")
    except Exception:
        progress_fail()  # AA-637
        raise
    return diag


async def mark_version_failed(pool, job_row: dict) -> None:
    """on_terminal: the job ended without success, so the version must not stay 'pending'."""
    version_id = (job_row.get("payload") or {}).get("version_id")
    if not version_id:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE gold_aa_internal.tenant_tour_versions SET status = 'failed' "
            "WHERE id = $1::uuid AND status = 'pending'", version_id)
    logger.warning("tenant_rewrite_job_failed", version_id=version_id, job_id=job_row.get("id"),
                   job_status=job_row.get("status"), error=(job_row.get("error") or "")[:300])


# Live T2 rewrite ≈ 35 s (S201); 5 min means something is stuck.
@job_kind(KIND, concurrency=4, max_attempts=2, on_terminal=mark_version_failed, expected_seconds=300)
async def run(ctx: JobContext) -> dict:
    p = ctx.payload
    version_id, tenant_id = p.get("version_id"), p.get("tenant_id")
    published_tour_id = p.get("published_tour_id")
    if not (version_id and tenant_id and published_tour_id):
        raise NonRetryable("payload needs version_id, tenant_id and published_tour_id")
    pool = ctx.pool

    async with pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM gold_aa_internal.tenant_tour_versions WHERE id = $1::uuid", version_id)
        if status is None:
            raise NonRetryable(f"version {version_id} not found")
        if status != "pending":  # already written (or failed and not retried from the version)
            return {"skipped": True, "version_status": status}
        pt = await conn.fetchrow(_PT_SQL, published_tour_id)
    if not pt:
        raise NonRetryable(f"published tour {published_tour_id} not found")

    tour_dict, brand_rules = await _prepare(pool, pt, tenant_id, p.get("rewrite_language") or "EN-US")
    brand_loaded = bool(brand_rules.pop("_brand_loaded", False))
    if not brand_loaded:
        logger.warning("t2_rewrite_without_brand", tenant_id=tenant_id, version_id=version_id)

    # AA-637 — live progress (steps + streamed tour fields), read by GET /v1/progress/tour/{id}.
    progress = WritingProgress(
        ctx.resources.get("redis"), tenant_id=str(tenant_id), kind="tour", job_id=str(version_id),
        steps=TOUR_STEPS, stream_stages={"t2_generate", "s1_generate"}, display="tour_json",
        stage_steps=TOUR_STAGE_STEPS,
    )
    progress.start()
    progress.step("research")
    await ctx.progress(phase="writing", version_id=version_id)
    try:
        with stream_sink.bind(progress):
            diag = await _rewrite_and_save(pool, pt, tour_dict, brand_rules, tenant_id, version_id,
                                           published_tour_id)
    except Exception:
        progress.fail()
        raise
    finally:
        await progress.finish(not progress.failed,
                              None if not progress.failed else "Writing didn't finish. Please try again.")

    async with pool.acquire() as conn:
        final = await conn.fetchrow(
            "SELECT status, quality_score::float AS score, qa_status FROM "
            "gold_aa_internal.tenant_tour_versions WHERE id = $1::uuid", version_id)
    await ctx.progress(phase="done")
    return {"version_id": version_id, "version_status": final["status"], "score": final["score"],
            "qa_status": final["qa_status"], "brand_loaded": brand_loaded, **(diag or {})}
