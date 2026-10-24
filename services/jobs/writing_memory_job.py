"""AA-917 / AA-918 — the writing-memory job kinds (app ADR 0010; WM-10, WM-11, WM-12, WM-15).

  writing_signal_backfill     WM-10: copy the existing learning material into shared.writing_signal (idempotent,
                              insert-only, $0), then queue the fact filter. Payload {"dry_run": bool, "sources": [..]}.
  writing_signal_fact_filter  WM-11: classify the `pending` signals (pass / grey / fail, $0), then queue an automatic
                              mining run when its trigger holds. The trigger needs WRITING_MEMORY_AUTO_MINE on the
                              worker (default off: the result only reports `mine_gate`). Payload {"limit": int}.
  writing_memory_mine         WM-12: one mining run (the only paid kind: <= 50 judge-route calls, <= $1, month cap
                              $25). Payload {"trigger": "auto" | "manual", "requested_by": "<admin id>"}. A refused
                              trigger ends the job as succeeded with `ran: false` and the reason (nothing to retry).

  writing_memory_r0           AA-918 WM-15: the R0 replay ($0, no model call) of up to 20 rules, each result kept on
                              its rule (`lint.r0`); or, with {"baseline": true}, the sanity check alone (the gates
                              re-run on stored T9 texts against their stored ledgers). Payload {"rule_ids": [..],
                              "requested_by": "<admin id>"} | {"baseline": true}.

All four are background kinds (they never take the worker's reserve slots of a wave) with concurrency 1. The mine
kind has max_attempts 1: a paid run is never retried by the runner (an admin starts a new one).
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


__all__ = ["BACKFILL", "FACT_FILTER", "MINE", "R0"]
