"""AA-847 Problem 2 — `place_dedupe` job kind (platform, debounced, background, cap 1): the automatic duplicate-place
merge (services/places/dedupe_run.run; decision services/places/dedupe.decide; storage migration 257).

The only writer of shared.destinations.duplicate_of besides the admin fix (an override). place_geocode stays the only
writer of coordinates. Both take the destinations advisory lock.

Payload: {"mode"?: "apply" (default: writes for countries in mode apply on geo.place_dedupe_country) | "dry_run"
(never writes), "country"?, "reason"?, "coalesced"?}. Enqueued (debounced) by place_geocode when it wrote or moved a
destination, by place_shape when a shape is drawn (services.places.enqueue.enqueue_dedupe), by a merge undo, and by
POST /admin/places/dedupe/run.

Paid calls (allow-listed countries only): the two-model check of the middle band — a3_place_dedupe (primary, asked
A/B and B/A) and a3_place_dedupe_check (another vendor), both role `validate`, temperature 0, structured output,
one shared.llm_call_log row per call (the job id is bound by the worker); budget bedrock/job:place_dedupe ($0.50 per
run, hard stop) and ≤ 150 pairs per run. A merge needs the two vendors to agree: when the gateway answers both calls
from one vendor (a fallback), the check counts as unsure.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from services.places import dedupe as dd
from services.places import dedupe_run
from services.places.enqueue import DEDUPE_KIND
from shared.cost_guard import load_run_budget, maybe_alert
from shared.jobs.registry import JobContext, job_kind

logger = structlog.get_logger()

KIND = DEDUPE_KIND
# pre-call estimates for the budget check (≈ 900 tokens in / 80 out): Luna ≈ $0.0002, Haiku ≈ $0.0013
ESTIMATE_USD = {dd.STAGE: 0.001, dd.CHECK_STAGE: 0.003}


def vendor_of(model: Optional[str]) -> str:
    m = (model or "").lower()
    if "gpt" in m or "luna" in m or "openai" in m:
        return "openai"
    if any(x in m for x in ("haiku", "sonnet", "opus", "claude", "anthropic")):
        return "anthropic"
    return m or "?"


def _generate_sync(stage: str, system: str, user: str):
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest
    return LLMClient().generate(LLMRequest(system_prompt=system, user_prompt=user, stage=stage,
                                           max_tokens=dd.MAX_TOKENS, temperature=0.0, json_schema=dd.VERDICT_SCHEMA))


def make_models(pool, budget, names: list[str], generate_sync=_generate_sync) -> dedupe_run.Models:
    """The primary and the check as async (system, user) -> text, each call budget-checked, logged to
    shared.llm_call_log (role validate) and charged on Models.cost_usd. A check answered by the primary's vendor is
    returned as `unsure` (no two-vendor agreement)."""
    from shared.llm_client import call_log
    from shared.llm_client.client import UnusableReply, unusable_reply_cost

    state: dict = {"primary_vendor": None}
    models = dedupe_run.Models(None, None, names)

    async def call(stage: str, system: str, user: str):
        if budget is not None:
            budget.check(ESTIMATE_USD[stage])
        try:
            resp = await asyncio.to_thread(generate_sync, stage, system, user)
        except UnusableReply as e:
            # S251 (AA-835): billed but unusable (the gateway checks the verdict schema on every key now). Charge the
            # run like any paid call; the gateway already wrote its llm_call_log row(s), so none here.
            cost = unusable_reply_cost(e)
            if budget is not None:
                budget.charge(cost)
            models.calls += 1
            models.cost_usd += cost
            raise
        if budget is not None:
            budget.charge(resp.cost_usd)
        models.calls += 1
        models.cost_usd += float(resp.cost_usd or 0.0)
        await call_log.record_call_with_pool(
            pool, stage=stage, role="validate", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, quality_signal={"app": "cis", "issue": "AA-847", "op": "place_dedupe",
                                                    "rule_version": dd.RULE_VERSION,
                                                    "output_len_chars": len(resp.content or "")},
            stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None))
        return resp

    async def primary(system: str, user: str) -> str:
        resp = await call(dd.STAGE, system, user)
        state["primary_vendor"] = vendor_of(resp.model_used)
        return resp.content

    async def check(system: str, user: str) -> str:
        try:
            resp = await call(dd.CHECK_STAGE, system, user)
        except UnusableReply as e:
            # S251: one off-schema check reply makes only this pair unsure (as parse_verdict did before the gateway
            # checked the schema on the legacy haiku key), instead of stopping the rest of the run's model band.
            logger.warning("place_dedupe_check_unusable", error=str(e)[:200])
            return '{"verdict": "unsure", "confidence": 0, "reason": "check reply did not match the schema"}'
        if vendor_of(resp.model_used) == state["primary_vendor"]:
            logger.warning("place_dedupe_check_same_vendor", model=resp.model_used)
            return '{"verdict": "unsure", "confidence": 0, "reason": "check answered by the primary\'s vendor"}'
        return resp.content

    models.primary, models.check = primary, check
    return models


async def stage_models(pool) -> list[str]:
    """`stage:model` of both stages as configured (the verdict cache key: a model change asks again)."""
    from shared.llm_client.role_config import SAFE_DEFAULTS
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT stage, model_id FROM shared.llm_role_config WHERE stage = ANY($1::text[])",
                                [dd.STAGE, dd.CHECK_STAGE])
    got = {r["stage"]: r["model_id"] for r in rows}
    return [f"{s}:{got.get(s) or SAFE_DEFAULTS[s].model_id}" for s in (dd.STAGE, dd.CHECK_STAGE)]


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=900, background=True)
async def run(ctx: JobContext) -> Optional[dict]:
    p = ctx.payload or {}
    mode = p.get("mode") or "apply"
    budget = await load_run_budget(ctx.pool, "bedrock", KIND)
    models = make_models(ctx.pool, budget, await stage_models(ctx.pool))
    result = await dedupe_run.run(ctx.pool, mode=mode, country=p.get("country") or None, models=models,
                                  job_id=ctx.job_id, progress=ctx.progress)
    ctx.add_cost(round(models.cost_usd, 6))
    if models.stopped == "budget":
        await maybe_alert(ctx.pool, budget, reason="place_dedupe stopped by its per-run budget")
    logger.info("place_dedupe_done", mode=mode, model_calls=models.calls, model_cost_usd=round(models.cost_usd, 6),
                **{c: v.get("status") for c, v in result["countries"].items()})
    return result
