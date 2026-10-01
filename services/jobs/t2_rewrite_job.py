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
    TOUR_STAGE_STEPS, TOUR_STEPS, WritingProgress, progress_fail, progress_step,
)
from shared.jobs.registry import JobContext, NonRetryable, job_kind
from shared.llm_client import stream_sink

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


async def _prepare(pool, pt, tenant_id: str, rewrite_language: str) -> tuple[dict, dict]:
    """tour_dict + brand_rules, unchanged from the pre-AA-652 router code."""
    # Build tour dict from published tour
    tour_dict = {
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
    brand_rules = {}
    try:
        async with pool.acquire() as _conn2:
            # AA-612a: T2 (tenant rewrite) shares build_graph with admin S1, whose judge
            # brand-fit gate (judge_node.has_brand_signals) keys off core_idea /
            # customer_mindset / voice_examples. This SELECT used to omit those brand-diff
            # columns, so brand_rules never carried them → the graph state's brand_* fields were
            # always empty → judge_skipped(no_brand_profile) even for a tenant WITH a real brand
            # (the exact case that SHOULD be judged on brand-fit). Fetch the same brand-diff
            # columns admin_pipeline._BRAND_RULE_COLS does so a real tenant brand is judged.
            _br = await _conn2.fetchrow("""
                SELECT system_prompt, style_guide, forbidden_words,
                       core_idea, customer_segment, customer_mindset, voice_examples, good_examples
                FROM shared.tenant_brand_rules
                WHERE tenant_id = $1::uuid AND is_active = true
                ORDER BY version DESC LIMIT 1
            """, tenant_id)
        if _br:
            # AA-425 fix (found via hash-diff forensic comparison against a direct-call harness
            # using the exact same tenant/tour): asyncpg has no jsonb codec registered on this
            # app's connections (same gap AA-314 already found/fixed elsewhere, api/routers/
            # v1_tours.py's own src_highlights handling) -- forbidden_words arrives as a raw
            # JSON-encoded STRING, not a parsed list. `list(_br["forbidden_words"] or [])` was
            # calling list() on that STRING, splitting it into individual characters
            # (['[', '"', 'l', 'u', 'x', ...]) instead of parsing it -- every tenant rewrite's
            # system prompt carried a garbled "FORBIDDEN WORDS: [, ", l, u, x, ..." instruction
            # instead of the tenant's real word list. Confirmed via SHA-256 hash comparison: the
            # user_prompt and tour_dict hashes matched byte-for-byte between this endpoint and a
            # direct _rewrite_tour() call using identical inputs, but brand_rules.forbidden_words
            # diverged at exactly this line.
            import json as _json2
            _fw_raw = _br["forbidden_words"]
            if isinstance(_fw_raw, str):
                _fw_raw = _json2.loads(_fw_raw) if _fw_raw else []
            # AA-612a: voice_examples has the same asyncpg no-jsonb-codec gotcha as
            # forbidden_words above — it arrives as a JSON-encoded string, so parse it before
            # list() (mirrors admin_pipeline._execute_run_tour's own handling).
            _voice_raw = _br["voice_examples"]
            if isinstance(_voice_raw, str):
                _voice_raw = _json2.loads(_voice_raw) if _voice_raw else []
            brand_rules = {
                "system_prompt":    _br["system_prompt"] or "",
                "style_guide":      _br["style_guide"] or "",
                "forbidden_words":  list(_fw_raw or []),
                "rewrite_language": rewrite_language,
                # AA-612a: brand-diff fields — the keys _rewrite_tour maps into the graph's
                # brand_* state (v1_pipeline.py), which the judge brand-fit gate reads.
                "core_idea":        _br["core_idea"] or "",
                "customer_segment": _br["customer_segment"] or "",
                "customer_mindset": _br["customer_mindset"] or "",
                "voice_examples":   list(_voice_raw or []),
                "good_examples":    _br["good_examples"] or "",
            }
    except Exception:
        brand_rules = {"rewrite_language": rewrite_language}
    return tour_dict, brand_rules


async def _rewrite_and_save(pool, pt, tour_dict: dict, brand_rules: dict, tenant_id: str,
                            version_id: str, published_tour_id: str) -> None:
    """SEO lookup, rewrite, T3 QA gate, save — unchanged from the pre-AA-652 router closure
    except that a failure raises (so the job retries) instead of silently marking the version."""
    from api.routers.v1_pipeline import _rewrite_tour as _do_rewrite

    # AA-445-02 — T2 passes the tour's SEO context to the shared rewrite graph (it used to reach it
    # as {}). AA-646 follow-up (KAN-90, 29/09/2026): a tenant action never buys DataForSEO, so T2
    # only READS the cached seo_context row; it no longer calls process_seo() on a miss (~$0.18
    # per tour, outside every spend budget). A tour with no row is rewritten without SEO keywords
    # and logged (`t2_seo_context_missing`) so admin research can cover it.
    seo_data: dict = {}
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
        if _mkt_seo:
            seo_data = _mkt_seo
        else:
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

    try:
        result = await _do_rewrite(
            tour_dict, idx=0, total=1,
            brand_rules=brand_rules,
            seo=seo_data,
            is_tenant_rewrite=True,  # skips name-match check in validate_node
            tenant_id=tenant_id,             # AA-620: log this tenant in llm_call_log
            generate_stage="t2_generate",    # AA-620: tenant writer stage (admin-tunable)
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
            )
            result = qa["result"]  # possibly a later repair round's output

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
            rewrite_score = float(result.get("quality_score") or 0)
            # AA-436: status is now purely score-based, same formula for a real T3 pass
            # and an auto-pass — T3 no longer forces needs_review on its own (see
            # qa_auto_passed below for the separate signal that a QA-gate failure
            # happened). ADR-2026-038 §0.1 (amend §10.3): escalate-and-stop broke the
            # single-job T2->T3->T5 chain and made the tenant fix wording themselves —
            # both rejected 22/08.
            if rewrite_score >= 7.0:
                new_status = "ai_generated"   # ready for tenant to review
            elif rewrite_score > 0:
                new_status = "needs_review"   # LLM finished but low quality
            else:
                new_status = "needs_review"   # hitl / score=0 — needs human
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
            await _rewrite_and_save(pool, pt, tour_dict, brand_rules, tenant_id, version_id,
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
            "qa_status": final["qa_status"]}
