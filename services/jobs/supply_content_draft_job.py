"""AA-835 — job kind `supply_content_draft`: one LLM draft of the customer content of a Booking hotel / service.

Queued by the supply feed consumer (services/supply_content/pull.py) once per applied facts version, key
``supply_content_draft:<source_id>:<supply_version>``, or by a staff "regenerate" (key + ``:manual:<nonce>``).
Payload: ``{"source_id", "supply_version", "trigger": "feed" | "manual"}``.

  1. Skips (succeeds with ``skipped``) when SUPPLY_CONTENT_LLM=off (kill switch), the entity is inactive, or its facts
     moved past this version (the newer version has its own job).
  2. Cost: at most SUPPLY_CONTENT_MAX_DRAFTS_PER_DAY (default 20) paid writer calls a UTC day across all entities
     (counted in shared.llm_call_log, stage supply_content_writer, so an unusable reply or a draft discarded because
     the facts moved on counts too), and the cost guard row bedrock / job:supply_content_draft (CIS 278: $0.10 a run,
     $1.00 a day, this kind's own spend). Either limit stops the job with status stopped_budget (BudgetExceeded),
     before any paid call. A stopped job is not re-queued the next day: the entity keeps its status (stale / draft,
     no text for Booking) until Booking changes it again or a reviewer presses regenerate.
  3. Copies the rights-cleared photos (services/supply_content/photos.py), so the draft can use them.
  4. One LLM call (services/supply_content/writer.py, stage supply_content_writer, logged to shared.llm_call_log
     with this job's id), in a thread.
  5. Writes a new llm draft version (review.insert_llm_draft), superseding only older open llm drafts; it never
     touches an approved or human version and never publishes: a human reviews every draft in /admin/supply-content.

Background kind (never takes the worker's last free slots, so it cannot starve a wave), concurrency 1, 2 attempts.
Observable: the Jobs page (result: version, cost, photos), the admin status board (draft jobs per status, LLM cost
today), llm_call_log rows of stage supply_content_writer.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

import structlog

from shared.cost_guard import BudgetExceeded, load_run_budget
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "supply_content_draft"
MAX_ATTEMPTS = 2
DEFAULT_MAX_DRAFTS_PER_DAY = 20
# S251 (CIS 282): Sonnet 5.5, ~3k in / <= 2048 out (thinking included) ≈ $0.026 a request; worst case one attempt is
# two Converse requests (schema retry) + the legacy Sonnet 4.6 fallback ≈ $0.09. The check uses that upper bound
# (it still fits the $0.10 per-run cap: one call per run).
CALL_ESTIMATE_USD = 0.10

ENTITY_SQL = """
    SELECT id::text AS id, entity_type, title, facts::text AS facts, applied_supply_version, active
      FROM shared.supply_entity WHERE source_system = 'booking' AND source_id = $1
"""
# Paid calls, not written drafts: every writer call (usable or not) has its llm_call_log row (writer.py).
DRAFTS_TODAY_SQL = """
    SELECT count(*) FROM shared.llm_call_log
     WHERE stage = $1 AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
"""
USABLE_PHOTOS_SQL = """
    SELECT booking_media_id::text AS media_id FROM shared.supply_media
     WHERE entity_id = $1::uuid AND usage_rights AND active AND url_1600 IS NOT NULL
"""


def llm_enabled() -> bool:
    return (os.environ.get("SUPPLY_CONTENT_LLM") or "on").strip().lower() not in ("off", "0", "false", "no")


def max_drafts_per_day() -> int:
    try:
        return max(0, int(os.environ.get("SUPPLY_CONTENT_MAX_DRAFTS_PER_DAY", DEFAULT_MAX_DRAFTS_PER_DAY)))
    except ValueError:
        return DEFAULT_MAX_DRAFTS_PER_DAY


def draft_photos(facts: dict, usable: set[str], alt: str) -> list[dict]:
    """The usable photos in Booking's order (facts.media), as version photos."""
    out = []
    for m in facts.get("media") or []:
        media_id = str((m or {}).get("media_id") or "")
        if media_id in usable:
            out.append({"media_id": media_id, "caption": None, "alt": alt[:160] or None})
    return out


@job_kind(KIND, concurrency=1, max_attempts=MAX_ATTEMPTS, expected_seconds=300, background=True)
async def run(ctx: JobContext) -> dict:
    from services.supply_content import photos as photos_mod
    from services.supply_content import review, writer

    payload = ctx.payload or {}
    source_id, version = payload.get("source_id"), payload.get("supply_version")
    if not isinstance(source_id, str) or not isinstance(version, int):
        raise NonRetryable("payload needs source_id (str) and supply_version (int)")
    base = {"source_id": source_id, "supply_version": version, "trigger": payload.get("trigger")}
    if not llm_enabled():
        return {**base, "skipped": "llm_off"}
    entity = await ctx.pool.fetchrow(ENTITY_SQL, source_id)
    if entity is None:
        raise NonRetryable(f"no supply entity for Booking id {source_id[:64]}")
    if not entity["active"]:
        return {**base, "skipped": "inactive"}
    if int(entity["applied_supply_version"]) != version:
        return {**base, "skipped": "superseded", "current_supply_version": int(entity["applied_supply_version"])}

    cap = max_drafts_per_day()
    today = int(await ctx.pool.fetchval(DRAFTS_TODAY_SQL, writer.STAGE) or 0)
    if today >= cap:
        raise BudgetExceeded(f"supply content draft cap reached: {today} of {cap} writer calls today (UTC)",
                             "bedrock", "per_day_drafts")
    budget = await load_run_budget(ctx.pool, "bedrock", KIND, day_scope="kind")
    budget.check(CALL_ESTIMATE_USD)

    copied = await photos_mod.copy_rights_cleared(ctx.pool, entity["id"], source_id)
    facts = json.loads(entity["facts"])
    usage: Optional[dict] = None
    try:
        draft, usage = await asyncio.to_thread(writer.draft_content, entity["entity_type"], facts)
    except writer.DraftUnusable as exc:
        raise NonRetryable(f"draft unusable: {exc}") from exc
    ctx.add_cost(usage["cost_usd"])
    budget.charge(usage["cost_usd"])

    usable = {r["media_id"] for r in await ctx.pool.fetch(USABLE_PHOTOS_SQL, entity["id"])}
    content = {**draft, "photos": draft_photos(facts, usable, entity["title"])}
    async with ctx.pool.acquire() as conn:
        written = await review.insert_llm_draft(conn, entity["id"], version, content, job_id=ctx.job_id,
                                                created_by=f"job:{KIND}")
    result = {**base, "entity_id": entity["id"], "photos": copied, "cost_usd": round(usage["cost_usd"], 6),
              "model": usage["model"]}
    if written is None:
        logger.info("supply_content_draft_discarded", job_id=ctx.job_id, **base)
        return {**result, "skipped": "facts_changed_during_draft"}
    logger.info("supply_content_draft_written", job_id=ctx.job_id, version=written["content_version"], **base)
    return {**result, "version_id": written["id"], "content_version": written["content_version"]}
