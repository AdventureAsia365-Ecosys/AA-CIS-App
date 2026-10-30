"""AA-917 / AA-918 — the writing-memory job kinds (app ADR 0010; WM-10, WM-11, WM-12, WM-15).

  writing_signal_backfill     WM-10: copy the existing learning material into shared.writing_signal (idempotent,
                              insert-only, $0), then queue the fact filter. Payload {"dry_run": bool, "sources": [..]}.
  writing_signal_fact_filter  WM-11: classify the `pending` signals (pass / grey / fail, $0), then queue an automatic
                              mining run when its trigger holds. The trigger needs WRITING_MEMORY_AUTO_MINE on the
                              worker (default off: the result only reports `mine_gate`). Payload {"limit": int}.
  writing_memory_mine         WM-12: one mining run (paid: <= 50 judge-route calls, <= $1, month cap
                              $25). Payload {"trigger": "auto" | "manual", "requested_by": "<admin id>"}. A refused
                              trigger ends the job as succeeded with `ran: false` and the reason (nothing to retry).
                              AA-921 WM-28: {"mode": "preference_summary"} drafts the tenant preference summaries
                              instead (same caps; off unless WRITING_MEMORY_PREF_SUMMARY: `state: needs_llm_path`).

  writing_memory_r0           AA-918 WM-15: the R0 replay ($0, no model call) of up to 20 rules, each result kept on
                              its rule (`lint.r0`); or, with {"baseline": true}, the sanity check alone (the gates
                              re-run on stored T9 texts against their stored ledgers). Payload {"rule_ids": [..],
                              "requested_by": "<admin id>"} | {"baseline": true}.

  writing_memory_r2           AA-919 WM-19: one R2 run of a candidate release (or today's writers) against the golden
                              set (paid, <= $10 hard cap). Payload = services.writing_memory.r2.R2Config.
  writing_memory_shadow       AA-919 WM-21: one targeted shadow run of a release on recent real inputs (paid, <= $7).
                              Payload = services.writing_memory.shadow.ShadowConfig.

  writing_memory_post_activation  AA-920 WM-26: the post-activation check ($0, no model call) of every active release
                              with content, or of one ({"release_id": ...}); a violation rolls the writer's pointer back
                              (`apply`, default true) and alerts. Queued hourly by the api ops sampler while such a
                              release exists. Payload {"release_id"?: str, "n"?: int, "apply"?: bool}.

All of them are background kinds (they never take the worker's reserve slots of a wave) with concurrency 1. The paid
kinds (mine, r2, shadow) have max_attempts 1: a paid run is never retried by the runner (an admin starts a new one).
"""
from __future__ import annotations

import structlog

from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

BACKFILL = "writing_signal_backfill"
FACT_FILTER = "writing_signal_fact_filter"
MINE = "writing_memory_mine"


@job_kind(BACKFILL, concurrency=1, max_attempts=2, expected_seconds=900, background=True)
async def run_backfill_job(ctx: JobContext) -> dict:
    from services.writing_memory.backfill import SOURCES, run_backfill
    sources = ctx.payload.get("sources") or list(SOURCES)
    bad = [s for s in sources if s not in SOURCES]
    if bad:
        raise NonRetryable(f"unknown backfill source(s) {bad}; known: {list(SOURCES)}")
    dry_run = bool(ctx.payload.get("dry_run", False))
    result = await run_backfill(ctx.pool, sources=sources, dry_run=dry_run)
    if not dry_run:
        job_id, created = await enqueue(ctx.pool, FACT_FILTER, {"after": ctx.job_id},
                                        idempotency_key=f"{FACT_FILTER}:after:{ctx.job_id}",
                                        created_by=f"job:{BACKFILL}")
        result["fact_filter_job_id"] = job_id
    return result


@job_kind(FACT_FILTER, concurrency=1, max_attempts=3, expected_seconds=600, background=True)
async def run_fact_filter_job(ctx: JobContext) -> dict:
    from services.writing_memory.fact_filter import run_fact_filter
    from services.writing_memory.mining import run_gate
    limit = int(ctx.payload.get("limit") or 5000)
    result = await run_fact_filter(ctx.pool, limit=max(1, min(limit, 20000)))
    async with ctx.pool.acquire() as conn:
        refusal, facts = await run_gate(conn, "auto")
    result["mine_gate"] = {"refused": refusal, **facts}
    if refusal is None:
        job_id, _ = await enqueue(ctx.pool, MINE, {"trigger": "auto"}, idempotency_key=f"{MINE}:auto:{ctx.job_id}",
                                  created_by=f"job:{FACT_FILTER}")
        result["mine_job_id"] = job_id
    return result


@job_kind(MINE, concurrency=1, max_attempts=1, expected_seconds=1800, background=True)
async def run_mine_job(ctx: JobContext) -> dict:
    from services.writing_memory.mining import TRIGGERS, run_gate, run_mine
    if ctx.payload.get("mode") == "preference_summary":
        # AA-921 WM-28: the tenant preference summaries (same stage, route and caps; its own run, outside the
        # miner's monthly run count). Off unless WRITING_MEMORY_PREF_SUMMARY is set: no call, state needs_llm_path.
        from services.writing_memory.preference import run_preference
        result = await run_preference(ctx.pool, ctx)
        result["requested_by"] = ctx.payload.get("requested_by")
        logger.info("aa921_pref_done", **{k: result.get(k) for k in ("state", "calls", "candidates", "quarantined",
                                                                      "cost_usd")})
        return result
    trigger = ctx.payload.get("trigger") or "manual"
    if trigger not in TRIGGERS:
        raise NonRetryable(f"unknown trigger {trigger!r}")
    async with ctx.pool.acquire() as conn:
        refusal, facts = await run_gate(conn, trigger, exclude_job_id=ctx.job_id)
    if refusal is not None:
        logger.info("aa917_mine_refused", trigger=trigger, reason=refusal, **facts)
        return {"ran": False, "trigger": trigger, "refused": refusal, **facts}
    result = await run_mine(ctx.pool, ctx, trigger=trigger)
    result.update({"ran": True, "requested_by": ctx.payload.get("requested_by"), "gate": facts})
    logger.info("aa917_mine_done", **{k: result[k] for k in ("trigger", "clusters", "calls", "candidates",
                                                              "lint_rejected", "redacted", "store_refused",
                                                              "cost_usd", "stopped")})
    return result


R0 = "writing_memory_r0"
R0_MAX_RULES = 20


# expected_seconds: a cold run is ~0.3 s of CPU per text, up to 300 pieces x 6 texts per T9 writer (the baseline covers
# both writers), so up to ~20 minutes before the per-job cache helps; the Jobs page flags it only past this.
@job_kind(R0, concurrency=1, max_attempts=2, expected_seconds=1800, background=True)
async def run_r0_job(ctx: JobContext) -> dict:
    from services.writing_memory import rules as rules_mod
    from services.writing_memory.replay import run_baseline, run_r0
    if ctx.payload.get("baseline"):
        return {"baseline": await run_baseline(ctx.pool), "cost_usd": 0.0}
    ids = [str(i) for i in ctx.payload.get("rule_ids") or []][:R0_MAX_RULES]
    if not ids:
        raise NonRetryable("writing_memory_r0 needs rule_ids or baseline: true")
    out: dict = {"rules": [], "missing": [], "cost_usd": 0.0}
    cache: dict = {}                     # the rule-off runs of each stored text, shared by the rules of this job
    for rid in ids:
        async with ctx.pool.acquire() as conn:
            rule = await rules_mod.get_rule(conn, rid)
        if rule is None:
            out["missing"].append(rid)
        else:
            res = await run_r0(ctx.pool, rules_mod.serialise(rule), cache=cache)
            async with ctx.pool.acquire() as conn:
                await rules_mod.store_r0(conn, rid, res)
            out["rules"].append({"rule_id": rid, **{k: res[k] for k in ("l0_ok", "l0_reason", "r2_eligible", "phrase",
                                                                         "new_hard_stops", "pieces", "prevented")},
                                 "baseline_agreement": res["baseline"]["agreement"]})
        await ctx.progress(done=len(out["rules"]) + len(out["missing"]), total=len(ids))
    logger.info("aa918_r0_done", rules=len(out["rules"]), missing=len(out["missing"]),
                l0_ok=sum(1 for r in out["rules"] if r["l0_ok"]))
    return out


R2 = "writing_memory_r2"
SHADOW = "writing_memory_shadow"


async def _progress(ctx: JobContext, **fields) -> None:
    try:
        await ctx.progress(**{k: v for k, v in fields.items() if v is not None})
    except Exception as e:  # noqa: BLE001 — progress is cosmetic, never fails a paid run
        logger.warning("aa919_progress_failed", job_id=ctx.job_id, error=str(e)[:200])


# AA-919 WM-19: one R2 run (paid, <= $10 hard cap from llm_call_log, $25 month cap). max_attempts 1: a paid run is
# never retried by the runner (the operator starts a new one); a refused run (R2Refused) ends as failed with the reason,
# nothing spent. expected_seconds: a full A1 run is ~50 items x ~60-90 s of graph, so up to ~75 minutes.
# deploy_blocking: a deploy releases a running job back to the queue, and a re-claimed run would start over and pay
# again (a release does not count as an attempt), so a running R2 / shadow job blocks the deploy (AA-993 G5 guard).
@job_kind(R2, concurrency=1, max_attempts=1, expected_seconds=5400, background=True, deploy_blocking=True)
async def run_r2_job(ctx: JobContext) -> dict:
    from services.writing_memory.r2 import R2Refused, config_from_payload, run_with_pool
    conf = config_from_payload(ctx.payload)
    if not conf.triggered_by or conf.triggered_by == "manual":
        conf.triggered_by = f"job:{ctx.job_id}"
    try:
        out = await run_with_pool(ctx.pool, conf, progress=lambda **f: _progress(ctx, **f))
    except R2Refused as e:
        raise NonRetryable(f"R2 refused: {e}")
    ctx.add_cost(out.cost_usd)
    result = {"run_id": out.run_id, "verdict": out.verdict, "stop_reason": out.stop_reason,
              "stopped_phase": out.stopped_phase, "cost_usd": round(out.cost_usd, 6), "judge_kappa": out.judge_kappa,
              "judge_counts": out.judge_counts, "kappa_strata_below": out.kappa_strata_below,
              "counts": out.counts}
    logger.info("aa919_r2_job_done", **{k: result[k] for k in ("run_id", "verdict", "stop_reason", "cost_usd")})
    return result


# AA-919 WM-21: one targeted shadow run (paid, <= $7 hard cap; T2 / Facebook 50 inputs of ~90 s each at worst).
@job_kind(SHADOW, concurrency=1, max_attempts=1, expected_seconds=5400, background=True, deploy_blocking=True)
async def run_shadow_job(ctx: JobContext) -> dict:
    from services.writing_memory.shadow import ShadowRefused, config_from_payload, run_shadow
    conf = config_from_payload(ctx.payload)
    if not conf.triggered_by or conf.triggered_by == "manual":
        conf.triggered_by = f"job:{ctx.job_id}"
    try:
        out = await run_shadow(ctx.pool, conf, progress=lambda **f: _progress(ctx, **f))
    except ShadowRefused as e:
        raise NonRetryable(f"shadow refused: {e}")
    ctx.add_cost(out["cost_usd"])
    return out


POST_ACTIVATION = "writing_memory_post_activation"


# AA-920 WM-26: $0 and idempotent (a second run finds the release already moved back, or the same verdict), so it may
# retry; concurrency 1 keeps two runs from moving the same pointer at once (the rollback also locks the rows).
@job_kind(POST_ACTIVATION, concurrency=1, max_attempts=2, expected_seconds=300, background=True)
async def run_post_activation_job(ctx: JobContext) -> dict:
    from services.writing_memory import config as wm_cfg
    from services.writing_memory.post_activation import check_all, check_release
    n = int(ctx.payload.get("n") or wm_cfg.POST_ACTIVATION_N)
    apply = bool(ctx.payload.get("apply", True))
    actor = f"job:{ctx.job_id}"
    if ctx.payload.get("release_id"):
        report = await check_release(ctx.pool, str(ctx.payload["release_id"]), n=n, apply=apply, actor=actor)
        return {"releases": 1, "reports": [report], "cost_usd": 0.0}
    out = await check_all(ctx.pool, n=n, apply=apply, actor=actor)
    logger.info("aa920_post_activation_done", releases=out["releases"], verdicts=out["verdicts"])
    return out


__all__ = ["BACKFILL", "FACT_FILTER", "MINE", "POST_ACTIVATION", "R0", "R2", "SHADOW"]
