"""AA-1090 TripPlanner R8 — job kind ``tripplanner_custom_domain``: one tenant host from added to served, and from
removed to gone (``services/tenant_account/tripplanner_domain.py`` queues it; ``tp_edge.py`` holds the AWS calls).

Payload: {"domain_id": "<uuid>"}. One job per editor action (add, remove, retry); the job works on the row's CURRENT
status, so a remove queued while an add job waits simply wins. The job polls by rescheduling itself (the runner's
``Reschedule``: the attempt is not counted, the row keeps its id), every ``cis.tripplanner.custom_domain_poll_seconds``.

    pending     request the host's ACM certificate (us-east-1, DNS validation, tagged) -> once ACM shows the
                validation record: store it with the distribution's domain name (the two DNS records Tenant 360
                shows) -> validating
    validating  ISSUED -> attaching; still PENDING_VALIDATION past the setting
                cis.tripplanner.custom_domain_validation_timeout_hours after requested_at -> failed
                validation_timeout (certificate deleted); FAILED / timed out / revoked -> failed (certificate deleted)
    attaching   sync the edge (tp_edge.sync_edge: edge certificate naming the base names + every attaching / active
                host, then one UpdateDistribution) -> once the distribution reports Deployed: active
    active      sync the edge (a no-op when it is in step) and end
    removing    sync the edge without this host (alias detached, back to the base certificate when no host is left),
                delete the host's certificate, delete the row
    failed      nothing (staff retry or remove it)

``concurrency=1``: every change of the one distribution is serialised. A concurrent change (ETag mismatch) or a
certificate CloudFront still holds reschedules; a failure no wait can fix (an alias another distribution serves, a
refused certificate) fails the host with its code. On a job that ends without success the host is failed
(``job_failed``) unless it is being removed (then it stays removing for a retry of the removal). $0 for AWS (public
ACM certificates and alternate domain names are free; each poll is one free API read). No LLM.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from services.tenant_account import tp_edge
from services.tenant_account import tripplanner_domain as tpd
from shared import settings
from shared.jobs.registry import (RESCHEDULE_MAX_COUNT, RESCHEDULE_MAX_DELAY_SECONDS, JobContext, NonRetryable,
                                  Reschedule, job_kind)

logger = structlog.get_logger()

KIND = tpd.KIND
POLL_KEY = "cis.tripplanner.custom_domain_poll_seconds"
TIMEOUT_KEY = "cis.tripplanner.custom_domain_validation_timeout_hours"

ROW_SQL = f"SELECT {tpd.COLUMNS} FROM shared.tenant_tripplanner_domain WHERE domain_id = $1::uuid"
EDGE_HOSTS_SQL = """
    SELECT domain FROM shared.tenant_tripplanner_domain WHERE status IN ('attaching', 'active') ORDER BY domain
"""
SET_CERT_SQL = """
    UPDATE shared.tenant_tripplanner_domain SET certificate_arn = $2, updated_at = now(), updated_by = $3
     WHERE domain_id = $1::uuid
"""
VALIDATING_SQL = """
    UPDATE shared.tenant_tripplanner_domain
       SET status = 'validating', validation_name = $2, validation_value = $3, cname_target = $4,
           updated_at = now(), updated_by = $5
     WHERE domain_id = $1::uuid AND status = 'pending'
"""
ATTACHING_SQL = """
    UPDATE shared.tenant_tripplanner_domain SET status = 'attaching', validated_at = now(), updated_at = now(),
           updated_by = $2
     WHERE domain_id = $1::uuid AND status = 'validating'
"""
ACTIVE_SQL = """
    UPDATE shared.tenant_tripplanner_domain SET status = 'active', active_at = now(), cname_target = $2,
           updated_at = now(), updated_by = $3
     WHERE domain_id = $1::uuid AND status = 'attaching'
"""
FAILED_SQL = """
    UPDATE shared.tenant_tripplanner_domain
       SET status = 'failed', failure_code = $2, failure_message = left($3, 500),
           certificate_arn = CASE WHEN $4 THEN NULL ELSE certificate_arn END, updated_at = now(), updated_by = $5
     WHERE domain_id = $1::uuid AND status IN ('pending', 'validating', 'attaching')
"""
DELETE_SQL = "DELETE FROM shared.tenant_tripplanner_domain WHERE domain_id = $1::uuid AND status = 'removing'"


def actor(job_id: str) -> str:
    return f"job:{KIND}:{job_id}"[:64]


async def _poll_s() -> int:
    return int(await settings.aget(POLL_KEY))


async def _validation_delay_s() -> int:
    """The wait between two validation looks: the poll setting, but never so short that the whole validation window
    needs more than half of the runner's reschedule budget (RESCHEDULE_MAX_COUNT): 72 h at 120 s would be 2,160 looks,
    and the runner fails a job after 600. Capped at the runner's longest reschedule."""
    window_s = int(await settings.aget(TIMEOUT_KEY)) * 3600
    spread = -(-window_s // (RESCHEDULE_MAX_COUNT // 2))
    return min(RESCHEDULE_MAX_DELAY_SECONDS, max(await _poll_s(), spread))


async def _timed_out(row: Any, now: datetime) -> bool:
    hours = int(await settings.aget(TIMEOUT_KEY))
    return now - row["requested_at"] > timedelta(hours=hours)


async def _fail(ctx: JobContext, row: Any, code: str, message: str, acm: Any, *, delete_cert: bool) -> dict:
    if delete_cert:
        try:
            await asyncio.to_thread(tp_edge.delete_certificate, acm, row["certificate_arn"])
        except Exception as exc:  # the host fails anyway; a leftover certificate is free and tagged for clean-up
            logger.warning("tripplanner_custom_domain_cert_delete_failed", domain_id=str(row["domain_id"]),
                           error=f"{type(exc).__name__}: {str(exc)[:200]}")
            delete_cert = False
    await ctx.pool.execute(FAILED_SQL, str(row["domain_id"]), code, message, delete_cert, actor(ctx.job_id))
    logger.info("tripplanner_custom_domain_failed", domain_id=str(row["domain_id"]), code=code)
    return {"domain_id": str(row["domain_id"]), "status": "failed", "failure_code": code}


async def _edge_hosts(pool, exclude: Optional[str] = None) -> list[str]:
    return [r["domain"] for r in await pool.fetch(EDGE_HOSTS_SQL) if r["domain"] != exclude]


async def _sync(ctx: JobContext, acm: Any, cf: Any, hosts: list[str]) -> tp_edge.EdgeState:
    """sync_edge with the job's memory: the edge certificate an earlier run requested, the replaced one to delete."""
    progress = dict(ctx.job.progress or {})
    retire = progress.get("retire_cert")
    if retire:
        await asyncio.to_thread(tp_edge.delete_certificate, acm, retire)   # EdgeBusy while CloudFront still holds it
        progress.pop("retire_cert", None)
    state = await asyncio.to_thread(tp_edge.sync_edge, acm, cf, hosts, pending_arn=progress.get("pending_edge_cert"))
    progress["pending_edge_cert"] = state.pending_arn
    if state.retired:
        progress["retire_cert"] = state.retired
    ctx.job.progress = progress
    return state


async def _step_pending(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    domain_id, domain = str(row["domain_id"]), row["domain"]
    arn = row["certificate_arn"]
    if not arn:
        arn = await asyncio.to_thread(tp_edge.request_host_certificate, acm, domain, domain_id)
        await ctx.pool.execute(SET_CERT_SQL, domain_id, arn, actor(ctx.job_id))
    cert = await asyncio.to_thread(tp_edge.describe, acm, arn)
    record = tp_edge.validation_record(cert, domain)
    if record is None:
        raise Reschedule(await _poll_s(), {"step": "waiting_for_validation_record"}, reason="acm_record")
    target = await asyncio.to_thread(tp_edge.cname_target, cf, tp_edge.distribution_id())
    await ctx.pool.execute(VALIDATING_SQL, domain_id, record[0], record[1], target, actor(ctx.job_id))
    raise Reschedule(await _validation_delay_s(), {"step": "validating"}, reason="validation")


async def _step_validating(ctx: JobContext, row: Any, acm: Any, cf: Any) -> Optional[dict]:
    cert = await asyncio.to_thread(tp_edge.describe, acm, row["certificate_arn"])
    status = cert.get("Status")
    if status == tp_edge.ISSUED:
        await ctx.pool.execute(ATTACHING_SQL, str(row["domain_id"]), actor(ctx.job_id))
        return None   # go on to attaching in this run
    if status in tp_edge.FAILED_STATES:
        reason = cert.get("FailureReason") or status
        return await _fail(ctx, row, "certificate_" + str(status).lower(), f"ACM: {reason}", acm, delete_cert=True)
    if await _timed_out(row, datetime.now(timezone.utc)):
        return await _fail(ctx, row, "validation_timeout",
                           "The validation record was not found in time. Ask the agency to create both records, "
                           "then retry.", acm, delete_cert=True)
    raise Reschedule(await _validation_delay_s(), {"step": "validating"}, reason="validation")


async def _step_attaching(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    state = await _sync(ctx, acm, cf, await _edge_hosts(ctx.pool))
    if state.status in ("pending_cert", "updated") or not state.deployed:
        raise Reschedule(await _poll_s(), {**ctx.job.progress, "step": "attaching", "edge": state.status},
                         reason="edge")
    await ctx.pool.execute(ACTIVE_SQL, str(row["domain_id"]), state.cname_target, actor(ctx.job_id))
    logger.info("tripplanner_custom_domain_active", domain_id=str(row["domain_id"]))
    return {"domain_id": str(row["domain_id"]), "status": "active"}


async def _step_removing(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    domain_id = str(row["domain_id"])
    state = await _sync(ctx, acm, cf, await _edge_hosts(ctx.pool, exclude=row["domain"]))
    if state.status == "pending_cert":
        raise Reschedule(await _poll_s(), {**ctx.job.progress, "step": "removing", "edge": state.status},
                         reason="edge")
    # The host's own certificate and a replaced edge certificate can be deleted once CloudFront lets go of them.
    await asyncio.to_thread(tp_edge.delete_certificate, acm, row["certificate_arn"])
    if ctx.job.progress.get("retire_cert"):
        await asyncio.to_thread(tp_edge.delete_certificate, acm, ctx.job.progress["retire_cert"])
    await ctx.pool.execute(DELETE_SQL, domain_id)
    logger.info("tripplanner_custom_domain_removed", domain_id=domain_id)
    return {"domain_id": domain_id, "status": "removed"}


async def _step_active(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    await _sync(ctx, acm, cf, await _edge_hosts(ctx.pool))
    return {"domain_id": str(row["domain_id"]), "status": "active"}


async def _step_validating_then_attaching(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    out = await _step_validating(ctx, row, acm, cf)
    if out is not None:
        return out
    return await _step_attaching(ctx, await ctx.pool.fetchrow(ROW_SQL, str(row["domain_id"])), acm, cf)


# The step of each status that needs the edge configured (removing must run even after the config is gone).
_STEPS = {"pending": _step_pending, "validating": _step_validating_then_attaching, "attaching": _step_attaching,
          "active": _step_active}


async def _dispatch(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    status = row["status"]
    if status == "failed":
        return {"domain_id": str(row["domain_id"]), "status": "failed", "skipped": True}
    if status == "removing":
        return await _step_removing(ctx, row, acm, cf)
    step = _STEPS.get(status)
    if step is None:
        raise NonRetryable(f"unknown status {status!r}")
    if not tp_edge.configured():
        return await _fail(ctx, row, "edge_not_configured", "Own domains are not switched on in this environment.",
                           acm, delete_cert=False)
    return await step(ctx, row, acm, cf)


async def run_once(ctx: JobContext, acm: Any, cf: Any) -> dict:
    domain_id = (ctx.payload or {}).get("domain_id")
    if not isinstance(domain_id, str):
        raise NonRetryable("payload needs domain_id (str)")
    row = await ctx.pool.fetchrow(ROW_SQL, domain_id)
    if row is None:
        return {"domain_id": domain_id, "status": "gone"}
    try:
        return await _dispatch(ctx, row, acm, cf)
    except tp_edge.EdgeBusy as busy:
        raise Reschedule(await _poll_s(), {**(ctx.job.progress or {}), "busy": str(busy)},
                         reason="edge_busy") from None
    except tp_edge.EdgeError as e:
        if row["status"] == "removing":
            raise NonRetryable(f"{e.code}: {e.message}") from None
        current = await ctx.pool.fetchrow(ROW_SQL, domain_id) or row
        return await _fail(ctx, current, e.code, e.message, acm, delete_cert=False)


async def mark_terminal(pool, job_row: dict) -> None:
    """A job that ended without success leaves no host waiting: fail it (a removing host stays removing)."""
    payload = job_row.get("payload") or {}
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)
    domain_id = payload.get("domain_id")
    if not isinstance(domain_id, str):
        return
    await pool.execute(FAILED_SQL, domain_id, "job_failed",
                       f"The setup job ended: {str(job_row.get('error') or job_row.get('status'))[:300]}", False,
                       actor(str(job_row.get("id"))))


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=120, background=True, on_terminal=mark_terminal)
async def run(ctx: JobContext, *, acm: Any = None, cf: Any = None) -> dict:
    if acm is None or cf is None:
        acm, cf = await asyncio.to_thread(tp_edge.clients)
    result = await run_once(ctx, acm, cf)
    ctx.set_result(result)
    return result
