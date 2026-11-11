"""AA-1076 — `llm_batch` job kind: one gateway batch (shared/llm_client/batch_lane.py) from prepare to hand-off.

ONE row per batch, whatever it takes (minutes to 24 hours): the handler does one step per run and reschedules its own
row (``registry.Reschedule``: back to queued, due later, the attempt not counted, the state merged into ``progress``),
so nothing waits in process and a worker restart or a deploy in between loses nothing.

Steps (``progress.phase``):

1. ``preparing``: the consumer (services/llm_batch/consumers.py) builds the requests; the lane resolves the stage's
   direct-API target and runs every guard before submit (lane switch, tenant stages, stage switch, app AI switch, the
   batch's cost cap and the central LLM budgets: ``batch_lane.guard``). ``dry_run`` stops here (``estimated``, $0).
2. ``submitting`` -> ``submitted``: ``submitting`` is written BEFORE the provider call, so a run that died between the
   provider accepting the batch and the next write is not sent twice: it fails ("submit outcome unknown", check the
   provider console) instead of paying again.
3. ``polling``: the provider status every POLL_DELAYS seconds (60 s growing to 10 min). Past DEADLINE_HOURS after
   submit the batch is cancelled; whatever it did not finish ends ``canceled`` / ``expired`` and goes on demand.
   A provider or transport error after submit (status, cancel, results: a 5xx, overloaded, a connection blip, a 401
   while a key rotates) never uses up the job's attempts: the batch is already sent and billed, so the run
   reschedules itself (growing delay, ``poll_errors`` + ``last_error`` in progress, reset by the next good status
   read) and the job fails only after POLL_ERROR_LIMIT errors in a row. Even then a Retry resumes polling the same
   provider batch (the counter is cleared first), nothing is sent again.
4. ``ingesting`` -> ``done``: one llm_call_log row per request at the batch rate (written once: a re-run that finds
   the batch's rows already in the ledger does not write them again), then the consumer's hand-off (idempotent
   job keys). The result: counts per outcome, spend, the standard-price equivalent and the hand-off summary.

Payload: {"batch_id": <uuid, ours>, "consumer": "s1_attempt1" | "a3_atomize" | "adhoc", "cap_usd": <float>,
"dry_run": bool, "cache_system": bool | null, "tour_ids": [...] (S1 / A3; top level, so the wave report and the wave
quality check find the batch's tours), "args": {batch_id of the run, seo_mode, drill_withhold; ad hoc: stage,
route_stage, items}}. Enqueued by POST /admin/ai-gateway/batches (api/routers/admin_ai_gateway.py); an ad-hoc script
enqueues it with shared.jobs.registry.enqueue.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from shared.jobs.registry import JobContext, NonRetryable, Reschedule, job_kind
from shared.llm_client import batch_lane, call_log
from shared.llm_client.batch_lane import BatchCapExceeded, BatchItem, BatchTarget, BatchUnavailable

logger = structlog.get_logger()

KIND = "llm_batch"
POLL_DELAYS = (60, 60, 120, 120, 300, 300, 600)     # seconds; the last one repeats
DEADLINE_HOURS = 25                                # the providers end a batch at 24 h; one hour of slack
# Provider errors in a row after submit before the job fails (~5 h of errors at the 10-minute delay). Spread-out errors
# never add up: every good status read resets the count.
POLL_ERROR_LIMIT = 36


def poll_delay(polls: int) -> int:
    return POLL_DELAYS[min(max(polls, 0), len(POLL_DELAYS) - 1)]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw
    try:
        out = datetime.fromisoformat(str(raw))
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def item_meta(stage: str, items: list[BatchItem]) -> list[dict]:
    """What the job keeps per request (never the prompt): custom id, tour, prompt identity, consumer meta."""
    return [{"custom_id": i.custom_id, "tour_id": i.tour_id, "prompt_sha": batch_lane.prompt_sha(stage, i.system,
                                                                                                  i.user),
             **({"meta": i.meta} if i.meta else {})} for i in items]


def outcome_counts(results: dict) -> dict:
    out: dict[str, int] = {}
    for r in results.values():
        key = "usable" if r.usable else r.outcome
        out[key] = out.get(key, 0) + 1
    return out


async def _prepare_and_submit(ctx: JobContext, p: dict) -> dict:
    from services.llm_batch.consumers import consumer

    prepare, _ingest = consumer(p.get("consumer") or "")
    await ctx.progress(phase="preparing", batch_id=p["batch_id"], consumer=p["consumer"])
    stage, items, extra = await prepare(ctx.pool, p)
    if not items:
        return {"phase": "done", "batch_id": p["batch_id"], "requests": 0, "prepare": extra,
                "note": "nothing to send (every tour skipped or unchanged)"}
    target = await asyncio.to_thread(batch_lane.resolve_target, stage)
    if extra.get("ledger_stage"):
        target.ledger_stage = extra["ledger_stage"]
    basis = await batch_lane.history_basis(ctx.pool, target)
    est = await asyncio.to_thread(batch_lane.guard, target, items, p.get("cap_usd"), basis)
    meta = item_meta(stage, items)
    summary = {"batch_id": p["batch_id"], "consumer": p["consumer"], "target": target.as_dict(), "requests": len(items),
               "estimate_usd": est.usd, "estimate": est.as_dict(), "cap_usd": float(p["cap_usd"]), "prepare": extra}
    if p.get("dry_run"):
        await ctx.progress(phase="estimated", **summary)
        return {"phase": "estimated", **summary}
    # Written before the provider call: a run that dies after the provider accepted the batch is never sent twice.
    await ctx.progress(phase="submitting", items=meta, **summary)
    provider_batch_id = await asyncio.to_thread(batch_lane.submit, target, items, batch_id=p["batch_id"],
                                                cache_system=p.get("cache_system"))
    logger.info("llm_batch_submitted", batch_id=p["batch_id"], provider_batch_id=provider_batch_id,
                stage=stage, model=target.model_key, connection=target.connection_key, requests=len(items),
                estimate_usd=est.usd, estimate_basis=est.basis)
    raise Reschedule(poll_delay(0), {"phase": "submitted", "provider_batch_id": provider_batch_id,
                                     "submitted_at": _now().isoformat(), "polls": 0}, "submitted")


async def _provider_call(ctx: JobContext, prog: dict, phase: str, fn, *args):
    """One provider call after submit (status, cancel, results). Its error reschedules the row instead of failing the
    attempt (the batch is sent and billed; a failed job would leave it unledgered and its tours never handed off):
    ``poll_errors`` counts the errors in a row (reset by a good status read in ``_poll``); at POLL_ERROR_LIMIT the job
    fails, with the counter cleared so a Retry polls the same provider batch again."""
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception as e:  # noqa: BLE001 — any provider / transport error, a 401 while a key rotates included
        errors = int(prog.get("poll_errors") or 0) + 1
        last = f"{type(e).__name__}: {str(e)[:300]}"
        logger.warning("llm_batch_provider_error", batch_id=prog.get("batch_id"),
                       provider_batch_id=prog.get("provider_batch_id"), phase=phase, poll_errors=errors, error=last)
        if errors >= POLL_ERROR_LIMIT:
            await ctx.progress(poll_errors=0, last_error=last, last_error_at=_now().isoformat())
            raise NonRetryable(f"{errors} provider errors in a row after submit (last: {last}); the provider batch "
                               f"{prog.get('provider_batch_id')} is still there: Retry resumes polling it, nothing "
                               "is sent again") from e
        raise Reschedule(poll_delay(errors), {"phase": phase, "poll_errors": errors, "last_error": last,
                                              "last_error_at": _now().isoformat()}, "provider error") from e


async def _poll(ctx: JobContext, prog: dict, target: BatchTarget) -> Optional[object]:
    """The provider state when the batch has ended, else reschedules (raises)."""
    pbid = prog["provider_batch_id"]
    st = await _provider_call(ctx, prog, "polling", batch_lane.status, target, pbid)
    if st.ended:
        return st
    polls = int(prog.get("polls") or 0) + 1
    fields = {"phase": "polling", "provider_status": st.processing_status, "counts": st.counts, "polls": polls,
              "poll_errors": 0}
    submitted = _parse_ts(prog.get("submitted_at"))
    if submitted and _now() - submitted > timedelta(hours=DEADLINE_HOURS) and not prog.get("cancel_sent_at"):
        await _provider_call(ctx, {**prog, "poll_errors": 0}, "polling", batch_lane.cancel, target, pbid)
        fields["cancel_sent_at"] = _now().isoformat()
        logger.warning("llm_batch_deadline_cancel", batch_id=prog.get("batch_id"), provider_batch_id=pbid)
    raise Reschedule(poll_delay(polls), fields, f"provider {st.processing_status}")


async def _ingest(ctx: JobContext, p: dict, prog: dict, target: BatchTarget, state) -> dict:
    from services.llm_batch.consumers import consumer

    _prepare, ingest = consumer(p["consumer"])
    items = prog.get("items") or []
    pbid = prog["provider_batch_id"]
    await ctx.progress(phase="ingesting", provider_status=state.processing_status, counts=state.counts)
    results = await _provider_call(ctx, prog, "ingesting", batch_lane.results, target, pbid,
                                   [i["custom_id"] for i in items], state.processing_status)
    since = _parse_ts(prog.get("submitted_at")) or (_now() - timedelta(days=2))
    seen = await batch_lane.ledgered(ctx.pool, target.log_stage, since, p["batch_id"])
    if seen["rows"]:
        logger.info("llm_batch_ledger_already_written", batch_id=p["batch_id"], rows=seen["rows"])
    else:
        await call_log.record_calls(batch_lane.ledger_rows(target, results, items, batch_id=p["batch_id"],
                                                           provider_batch_id=pbid, consumer=p["consumer"]))
    spend = await batch_lane.ledgered(ctx.pool, target.log_stage, since, p["batch_id"])
    handoff = await ingest(ctx.pool, p, target, items, results, batch_id=p["batch_id"])
    out = {"phase": "done", "batch_id": p["batch_id"], "consumer": p["consumer"], "target": target.as_dict(),
           "provider_batch_id": pbid, "provider_status": state.processing_status, "requests": len(items),
           "outcomes": outcome_counts(results), "ledger_rows": spend["rows"], "spend_usd": round(spend["usd"], 6),
           # what the same requests would have cost on demand (the batch rate is 50% of every token)
           "standard_usd": round(spend["usd"] / batch_lane.pricing.BATCH_DISCOUNT, 6),
           "estimate_usd": prog.get("estimate_usd"), "cap_usd": prog.get("cap_usd"), "handoff": handoff}
    await ctx.progress(phase="done", spend_usd=out["spend_usd"], outcomes=out["outcomes"])
    return out


# Each run is short (a provider call or a DB pass); the row lives as long as the batch (rescheduled, not running).
# Not deploy_blocking: every step is safe to re-run (submit is fenced by the 'submitting' phase, the ledger by the
# rows already written, the hand-off by idempotency keys).
@job_kind(KIND, concurrency=2, max_attempts=3, expected_seconds=900)
async def run(ctx: JobContext) -> Optional[dict]:
    p = ctx.payload or {}
    for key in ("batch_id", "consumer", "cap_usd"):
        if p.get(key) in (None, ""):
            raise NonRetryable(f"payload needs {key}")
    prog = dict(ctx.job.progress or {})
    phase = prog.get("phase")
    try:
        if phase == "estimated":            # a dry run (or its Retry): nothing more to do
            return {"phase": "estimated", **{k: prog.get(k) for k in ("estimate_usd", "requests", "target")}}
        if phase == "submitting":
            raise NonRetryable("submit outcome unknown: the run stopped while the batch was being sent; check the "
                               "provider console before sending it again (nothing is sent twice automatically)")
        if not prog.get("provider_batch_id"):  # new, refused before submit (a Retry), or nothing was sent
            return await _prepare_and_submit(ctx, p)
        target = BatchTarget.from_dict(prog["target"])
        state = await _poll(ctx, prog, target)
        return await _ingest(ctx, p, prog, target, state)
    except BatchCapExceeded as e:
        await ctx.progress(phase="refused", reason=str(e)[:500])
        raise NonRetryable(f"refused before submit: {e}") from e
    except BatchUnavailable as e:
        if not prog.get("provider_batch_id"):
            await ctx.progress(phase="refused", reason=str(e)[:500])
            raise NonRetryable(f"refused before submit: {e}") from e
        raise
