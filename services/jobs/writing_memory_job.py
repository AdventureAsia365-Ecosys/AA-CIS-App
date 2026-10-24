"""AA-917 — the writing-memory job kinds (app ADR 0010; WM-10, WM-11, WM-12).

  writing_signal_backfill     WM-10: copy the existing learning material into shared.writing_signal (idempotent,
                              insert-only, $0), then queue the fact filter. Payload {"dry_run": bool, "sources": [..]}.
  writing_signal_fact_filter  WM-11: classify the `pending` signals (pass / grey / fail, $0), then queue an automatic
                              mining run when its trigger holds. Payload {"limit": int}.
  writing_memory_mine         WM-12: one mining run (the only paid kind: <= 50 judge-route calls, <= $1, month cap
                              $25). Payload {"trigger": "auto" | "manual", "requested_by": "<admin id>"}. A refused
                              trigger ends the job as succeeded with `ran: false` and the reason (nothing to retry).

All three are background kinds (they never take the worker's reserve slots of a wave) with concurrency 1. The mine
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
        refusal, facts = await run_gate(conn, trigger)
    if refusal is not None:
        logger.info("aa917_mine_refused", trigger=trigger, reason=refusal, **facts)
        return {"ran": False, "trigger": trigger, "refused": refusal, **facts}
    result = await run_mine(ctx.pool, ctx, trigger=trigger)
    result.update({"ran": True, "requested_by": ctx.payload.get("requested_by"), "gate": facts})
    logger.info("aa917_mine_done", **{k: result[k] for k in ("trigger", "clusters", "calls", "candidates",
                                                              "lint_rejected", "cost_usd", "stopped")})
    return result


__all__ = ["BACKFILL", "FACT_FILTER", "MINE"]
