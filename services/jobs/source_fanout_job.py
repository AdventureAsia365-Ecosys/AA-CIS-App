"""AA-1052 W6 (root ADR 0020 layer 3) — job kind ``source_fanout``: the $0 re-check of the writer objects that read a
source that changed (database reads and one upsert per mark; no model call, no regeneration, no status change).

Payload (services/quality/source_fanout.Change.payload): {"source_kind": raw_tour | master | atom | place_card,
"reason": <typed reason>, "changed_at": <iso>, "tour_id"?: <uuid>, "source_ref"?: <atom / card / published_tours id>,
"fields"?: [<raw field name>], "dry_run"?: bool}.

Queued by the source write paths through ``source_fanout.enqueue_safe`` (the admin raw-tour edit, a Master publish,
an admin atom edit, the admin place card edits) and by POST /admin/quality/lineage/fanout/{tour_id}/recheck. One
job per source and minute (idempotency key). max_attempts 3: the fan-out is one transaction, so a retry starts
over; before CIS 392 is applied it fails (UndefinedTable) and is retried, then fails visibly.
"""
from __future__ import annotations

import json

import structlog

from services.quality import source_fanout
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = source_fanout.KIND


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=60)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    try:
        change = source_fanout.Change.from_payload(payload)
    except source_fanout.FanoutRefused as e:
        raise NonRetryable(f"bad source_fanout payload: {e}") from e
    dry_run = bool(payload.get("dry_run", False))
    async with ctx.pool.acquire() as conn:
        result = await source_fanout.fan_out(conn, change, job_id=ctx.job_id, dry_run=dry_run)
    logger.info("source_fanout_done", job_id=ctx.job_id, source_kind=change.source_kind, reason=change.reason,
                tour_id=str(change.tour_id or ""), source_ref=change.source_ref, dependents=result["dependents"],
                marked=len(result["marked"]), bumped=len(result["bumped"]), unchanged=len(result["unchanged"]),
                dry_run=dry_run)
    result = json.loads(json.dumps({"kind": KIND, **result}, default=str))
    ctx.set_result(result)
    return result
