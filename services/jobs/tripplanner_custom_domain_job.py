"""AA-1090 TripPlanner R8 — job kind ``tripplanner_custom_domain``: one tenant host from added to served, and from
removed to gone (``services/tenant_account/tripplanner_domain.py`` queues it; ``tp_edge.py`` holds the AWS calls), plus
the daily edge check of the agency distribution.

Payload: {"domain_id": "<uuid>"} (one job per editor action: add, remove, retry, a re-queued removal) or
{"check": "edge_health"} (the daily check, queued by the api's ops metrics sampler, one per UTC day). A domain job works
on the row's CURRENT status, so a remove queued while an add job waits simply wins. The job polls by rescheduling
itself (the runner's ``Reschedule``: the attempt is not counted, the row keeps its id), every
``cis.tripplanner.custom_domain_poll_seconds``.

    pending     request the host's ACM certificate (us-east-1, DNS validation, tagged; requested_at = now) or reuse
                the one a failed attempt left while ACM still validates it -> once ACM shows the validation record:
                store it with the agency distribution's domain name (the two DNS records Tenant 360 shows) -> validating
    validating  ISSUED -> attaching; still PENDING_VALIDATION past the setting
                cis.tripplanner.custom_domain_validation_timeout_hours after requested_at (reset by a retry and by a
                new request) -> failed validation_timeout (certificate deleted); FAILED / timed out / revoked -> failed
                (certificate deleted)
    attaching   sync the edge (tp_edge.sync_edge: edge certificate naming every attaching / active host, then one
                UpdateDistribution) -> once the distribution reports Deployed: active
    active      sync the edge (a no-op when it is in step) and end
    removing    when the host is an alias of the distribution: sync the edge without it (back to no alias and the
                default certificate when no host is left); delete the host's certificate (certificate_arn cleared at
                once), delete the row. A removal that cannot finish (the edge switched off while the host is served,
                a refusal) keeps the row removing with failure_code / failure_message; staff remove it again, which
                queues a fresh job.
    failed      nothing (staff retry or remove it)

One agency's DNS never blocks another's change. A re-issued edge certificate that is still PENDING_VALIDATION past
``cis.tripplanner.custom_domain_edge_pending_hours`` names the hosts whose record ACM cannot find
(``EdgeState.pending_names``): the job fails and detaches exactly those (failure_code ``validation_record_gone``, a
staff alert) and requests the certificate again without them. The daily edge check reads the served certificate's
renewal: a host that blocks ACM's managed renewal raises the alert at once, and once the certificate expires within
``cis.tripplanner.custom_domain_renewal_grace_days`` that host is failed and detached the same way, so the other
agencies never serve an expired certificate. The public TripPlanner host is on its own distribution and is never
affected.

``concurrency=1``: every change of the one agency distribution is serialised (domain jobs and the daily check alike). A
concurrent change (ETag mismatch) or a certificate CloudFront still holds reschedules; a failure no wait can fix (an
alias another distribution serves, a refused certificate) fails the host with its code. On a job that ends without
success the host is failed (``job_failed``), or a removing host keeps removing with that failure for a re-queue. $0 for
AWS (public ACM certificates and alternate domain names are free; each poll is one free API read). No LLM.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from services.tenant_account import tp_edge
from services.tenant_account import tripplanner_domain as tpd
from shared import ops_checks, settings
from shared.jobs.registry import (RESCHEDULE_MAX_COUNT, RESCHEDULE_MAX_DELAY_SECONDS, JobContext, NonRetryable,
                                  Reschedule, enqueue, job_kind)

logger = structlog.get_logger()

KIND = tpd.KIND
POLL_KEY = "cis.tripplanner.custom_domain_poll_seconds"
TIMEOUT_KEY = "cis.tripplanner.custom_domain_validation_timeout_hours"
EDGE_PENDING_KEY = "cis.tripplanner.custom_domain_edge_pending_hours"
RENEWAL_GRACE_KEY = "cis.tripplanner.custom_domain_renewal_grace_days"
HEALTH_CHECK = "edge_health"
ALERT_CHECK = "tripplanner_custom_domain_dns"
SCHEDULER = "scheduler:aa-1090"
GONE_CODE = "validation_record_gone"
GONE_MESSAGE = ("The agency's certificate validation record is no longer in its DNS, so the shared certificate of the "
                "agency domains could not be issued or renewed with this host. Ask the agency to put the record back, "
                "then retry.")

ROW_SQL = f"SELECT {tpd.COLUMNS} FROM shared.tenant_tripplanner_domain WHERE domain_id = $1::uuid"
EDGE_HOSTS_SQL = """
    SELECT domain FROM shared.tenant_tripplanner_domain WHERE status IN ('attaching', 'active') ORDER BY domain
"""
# A new certificate restarts the validation window (a retry after validation_timeout must get a full window).
SET_CERT_SQL = """
    UPDATE shared.tenant_tripplanner_domain SET certificate_arn = $2, requested_at = now(), updated_at = now(),
           updated_by = $3
     WHERE domain_id = $1::uuid
"""
CLEAR_CERT_SQL = """
    UPDATE shared.tenant_tripplanner_domain SET certificate_arn = NULL, updated_at = now(), updated_by = $3
     WHERE domain_id = $1::uuid AND certificate_arn = $2
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
# The hosts whose validation record is gone: failed and so off the edge set (the next sync detaches their alias).
DETACH_SQL = """
    UPDATE shared.tenant_tripplanner_domain
       SET status = 'failed', failure_code = $2, failure_message = left($3, 500), updated_at = now(), updated_by = $4
     WHERE domain = ANY($1::text[]) AND status IN ('attaching', 'active')
    RETURNING domain_id, tenant_id, domain
"""
# A removal that cannot finish: the row stays removing and says why (Tenant 360 offers "Retry removal").
REMOVE_STUCK_SQL = """
    UPDATE shared.tenant_tripplanner_domain
       SET failure_code = $2, failure_message = left($3, 500), updated_at = now(), updated_by = $4
     WHERE domain_id = $1::uuid AND status = 'removing'
"""
DELETE_SQL = "DELETE FROM shared.tenant_tripplanner_domain WHERE domain_id = $1::uuid AND status = 'removing'"
_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


def actor(job_id: str) -> str:
    return f"job:{KIND}:{job_id}"[:64]


async def _poll_s() -> int:
    return int(await settings.aget(POLL_KEY))


async def _spread_delay_s(window_s: int) -> int:
    """The wait between two looks over a window: the poll setting, but never so short that the whole window needs
    more than half of the runner's reschedule budget (RESCHEDULE_MAX_COUNT): 72 h at 120 s would be 2,160 looks, and
    the runner fails a job after 600. Capped at the runner's longest reschedule."""
    spread = -(-window_s // (RESCHEDULE_MAX_COUNT // 2))
    return min(RESCHEDULE_MAX_DELAY_SECONDS, max(await _poll_s(), spread))


async def _validation_delay_s() -> int:
    return await _spread_delay_s(int(await settings.aget(TIMEOUT_KEY)) * 3600)


async def _edge_pending_delay_s() -> int:
    return await _spread_delay_s(int(await settings.aget(EDGE_PENDING_KEY)) * 3600)


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


async def _alert(ctx: JobContext, summary: str, details: dict) -> None:
    """The staff alert (admin bell + at most one e-mail per UTC day). Never stops the job."""
    try:
        await ops_checks.raise_alert(ctx.pool, ops_checks.Finding(ALERT_CHECK, summary, details))
    except Exception as exc:  # noqa: BLE001 — the detach already happened; the row shows the failure
        logger.warning("tripplanner_custom_domain_alert_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")


async def _detach(ctx: JobContext, hosts: list[str], why: str) -> list[str]:
    """Fail the attaching / active ``hosts`` whose validation record is gone (off the edge set from now on)."""
    rows = await ctx.pool.fetch(DETACH_SQL, hosts, GONE_CODE, GONE_MESSAGE, actor(ctx.job_id))
    detached = sorted(r["domain"] for r in rows)
    if detached:
        logger.warning("tripplanner_custom_domain_detached", hosts=detached, why=why)
        await _alert(ctx, f"{len(detached)} agency domain(s) taken off line: validation record gone",
                     {"hosts": detached, "why": why, "action": "Ask each agency to put its certificate validation "
                      "record back, then retry the domain on Tenant 360."})
    return detached


async def _pending_too_long(state: tp_edge.EdgeState, now: datetime) -> bool:
    if state.pending_since is None:
        return False
    since = state.pending_since if state.pending_since.tzinfo else state.pending_since.replace(tzinfo=timezone.utc)
    return now - since > timedelta(hours=int(await settings.aget(EDGE_PENDING_KEY)))


async def _sync(ctx: JobContext, acm: Any, cf: Any, *, exclude: Optional[str] = None) -> tp_edge.EdgeState:
    """sync_edge over the current edge hosts, with the job's memory: the edge certificate an earlier run requested, the
    replaced one to delete. A pending edge certificate past the setting detaches the hosts that block it and is
    requested again without them (at most once per run)."""
    progress = dict(ctx.job.progress or {})
    retire = progress.get("retire_cert")
    if retire:
        await asyncio.to_thread(tp_edge.delete_certificate, acm, retire)   # EdgeBusy while CloudFront still holds it
        progress.pop("retire_cert", None)
    hosts = await _edge_hosts(ctx.pool, exclude)
    state = await asyncio.to_thread(tp_edge.sync_edge, acm, cf, hosts, pending_arn=progress.get("pending_edge_cert"))
    if state.status == "pending_cert" and await _pending_too_long(state, datetime.now(timezone.utc)):
        blocked = [h for h in state.pending_names if h in hosts]
        if blocked and await _detach(ctx, blocked, "edge_certificate_pending"):
            try:
                await asyncio.to_thread(tp_edge.delete_certificate, acm, state.pending_arn)
            except Exception as exc:  # noqa: BLE001 — a leftover pending certificate is free and tagged
                logger.warning("tripplanner_custom_domain_cert_delete_failed", arn_kind="pending_edge",
                               error=f"{type(exc).__name__}: {str(exc)[:200]}")
            hosts = await _edge_hosts(ctx.pool, exclude)
            state = await asyncio.to_thread(tp_edge.sync_edge, acm, cf, hosts)
    progress["pending_edge_cert"] = state.pending_arn
    if state.retired:
        progress["retire_cert"] = state.retired
    ctx.job.progress = progress
    return state


async def _reschedule_edge(ctx: JobContext, state: tp_edge.EdgeState, step: str) -> None:
    delay = await (_edge_pending_delay_s() if state.status == "pending_cert" else _poll_s())
    raise Reschedule(delay, {**ctx.job.progress, "step": step, "edge": state.status,
                             "pending_names": state.pending_names}, reason="edge")


async def _host_certificate(ctx: JobContext, row: Any, acm: Any) -> str:
    """The host's certificate: the stored one while ACM can still issue it, else a new request (the old one deleted)."""
    domain_id, arn = str(row["domain_id"]), row["certificate_arn"]
    if arn:
        cert = await asyncio.to_thread(tp_edge.describe_or_none, acm, arn)
        if cert is not None and cert.get("Status") in (tp_edge.ISSUED, tp_edge.PENDING):
            return arn
        await asyncio.to_thread(tp_edge.delete_certificate, acm, arn)
    arn = await asyncio.to_thread(tp_edge.request_host_certificate, acm, row["domain"], domain_id)
    await ctx.pool.execute(SET_CERT_SQL, domain_id, arn, actor(ctx.job_id))
    return arn


async def _step_pending(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    domain_id, domain = str(row["domain_id"]), row["domain"]
    arn = await _host_certificate(ctx, row, acm)
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
    domain_id = str(row["domain_id"])
    state = await _sync(ctx, acm, cf)
    current = await ctx.pool.fetchrow(ROW_SQL, domain_id)
    if current is None or current["status"] != "attaching":   # detached by the pending check, or removed meanwhile
        return {"domain_id": domain_id, "status": current["status"] if current else "gone"}
    if state.status in ("pending_cert", "updated") or not state.deployed:
        await _reschedule_edge(ctx, state, "attaching")
    await ctx.pool.execute(ACTIVE_SQL, domain_id, state.cname_target, actor(ctx.job_id))
    logger.info("tripplanner_custom_domain_active", domain_id=domain_id)
    return {"domain_id": domain_id, "status": "active"}


async def _removal_stuck(ctx: JobContext, row: Any, code: str, message: str) -> dict:
    await ctx.pool.execute(REMOVE_STUCK_SQL, str(row["domain_id"]), code, message, actor(ctx.job_id))
    logger.warning("tripplanner_custom_domain_removal_stuck", domain_id=str(row["domain_id"]), code=code)
    return {"domain_id": str(row["domain_id"]), "status": "removing", "failure_code": code}


async def _step_removing(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    domain_id = str(row["domain_id"])
    if tp_edge.configured():
        # Only a host the distribution serves needs an edge change: a pending / validating / failed host never
        # waits for (or is blocked by) another agency's certificate.
        if await asyncio.to_thread(tp_edge.serves, cf, row["domain"]):
            state = await _sync(ctx, acm, cf, exclude=row["domain"])
            if state.status == "pending_cert":
                await _reschedule_edge(ctx, state, "removing")
    elif row["validated_at"] is not None:
        return await _removal_stuck(ctx, row, "edge_not_configured",
                                    "Own domains are switched off in this environment while this host may still be "
                                    "served; remove it again once they are back on.")
    # The host's own certificate and a replaced edge certificate can be deleted once CloudFront lets go of them.
    if row["certificate_arn"]:
        await asyncio.to_thread(tp_edge.delete_certificate, acm, row["certificate_arn"])
        await ctx.pool.execute(CLEAR_CERT_SQL, domain_id, row["certificate_arn"], actor(ctx.job_id))
    retire = (ctx.job.progress or {}).get("retire_cert")
    if retire:
        await asyncio.to_thread(tp_edge.delete_certificate, acm, retire)
        ctx.job.progress = {k: v for k, v in ctx.job.progress.items() if k != "retire_cert"}
    await ctx.pool.execute(DELETE_SQL, domain_id)
    logger.info("tripplanner_custom_domain_removed", domain_id=domain_id)
    return {"domain_id": domain_id, "status": "removed"}


def _settling(ctx: JobContext, state: tp_edge.EdgeState) -> bool:
    """The edge is still moving: a certificate to wait for, an update just sent, or a replaced certificate to delete."""
    return state.status in ("pending_cert", "updated") or bool((ctx.job.progress or {}).get("retire_cert"))


async def _step_active(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    state = await _sync(ctx, acm, cf)
    if _settling(ctx, state):
        await _reschedule_edge(ctx, state, "active")
    current = await ctx.pool.fetchrow(ROW_SQL, str(row["domain_id"]))
    return {"domain_id": str(row["domain_id"]), "status": current["status"] if current else "gone"}


async def _step_validating_then_attaching(ctx: JobContext, row: Any, acm: Any, cf: Any) -> dict:
    out = await _step_validating(ctx, row, acm, cf)
    if out is not None:
        return out
    return await _step_attaching(ctx, await ctx.pool.fetchrow(ROW_SQL, str(row["domain_id"])), acm, cf)


# The step of each status that needs the edge configured (removing decides that itself).
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


async def run_domain(ctx: JobContext, domain_id: str, acm: Any, cf: Any) -> dict:
    row = await ctx.pool.fetchrow(ROW_SQL, domain_id)
    if row is None:
        return {"domain_id": domain_id, "status": "gone"}
    try:
        return await _dispatch(ctx, row, acm, cf)
    except tp_edge.EdgeBusy as busy:
        raise Reschedule(await _poll_s(), {**(ctx.job.progress or {}), "busy": str(busy)},
                         reason="edge_busy") from None
    except tp_edge.EdgeError as e:
        current = await ctx.pool.fetchrow(ROW_SQL, domain_id) or row
        if current["status"] == "removing":
            return await _removal_stuck(ctx, current, e.code, e.message)
        return await _fail(ctx, current, e.code, e.message, acm, delete_cert=False)


# ── the daily edge check ────────────────────────────────────────────────────────────────────────────────────────

async def run_health(ctx: JobContext, acm: Any, cf: Any) -> dict:
    """Read the served edge certificate's renewal; alert on hosts that block it and, once it expires within the grace
    setting, detach them and re-issue the certificate without them."""
    out: dict = {"check": HEALTH_CHECK}
    if not tp_edge.configured():
        return {**out, "skipped": "edge_not_configured"}
    if (ctx.job.progress or {}).get("step") == "health_resync":   # an earlier run detached hosts: finish the swap
        return {**out, **await _health_resync(ctx, acm, cf)}
    try:
        health = await asyncio.to_thread(tp_edge.edge_health, acm, cf)
    except tp_edge.EdgeBusy as busy:
        raise Reschedule(await _poll_s(), {"busy": str(busy)}, reason="edge_busy") from None
    out.update(cert=bool(health.cert_arn), status=health.status, renewal_status=health.renewal_status,
               not_after=health.not_after.isoformat() if health.not_after else None, blocking=health.blocking)
    if health.cert_arn is None or (not health.blocking and health.status in (None, tp_edge.ISSUED)):
        return out
    now = datetime.now(timezone.utc)
    grace = timedelta(days=int(await settings.aget(RENEWAL_GRACE_KEY)))
    due = health.not_after is None or health.not_after - now <= grace or health.status != tp_edge.ISSUED
    if not health.blocking:
        await _alert(ctx, f"The agency-edge certificate is {health.status}",
                     {**out, "action": "Check ACM (us-east-1)."})
        return out
    if not due:
        await _alert(ctx, f"{len(health.blocking)} agency domain(s) block the certificate renewal",
                     {"hosts": health.blocking, "not_after": out["not_after"], "action": "Ask each agency to put its "
                      "certificate validation record back; the domain is taken off line when the certificate is "
                      "within the renewal grace days of its expiry."})
        return {**out, "detached": []}
    detached = await _detach(ctx, health.blocking, "edge_certificate_renewal")
    ctx.job.progress = {**(ctx.job.progress or {}), "step": "health_resync"}
    return {**out, "detached": detached, **await _health_resync(ctx, acm, cf)}


async def _health_resync(ctx: JobContext, acm: Any, cf: Any) -> dict:
    try:
        state = await _sync(ctx, acm, cf)
    except tp_edge.EdgeBusy as busy:
        raise Reschedule(await _poll_s(), {**ctx.job.progress, "busy": str(busy)}, reason="edge_busy") from None
    if _settling(ctx, state):
        await _reschedule_edge(ctx, state, "health_resync")
    return {"edge": state.status}


def health_key(day: date) -> str:
    return f"{KIND}:{HEALTH_CHECK}:{day.isoformat()}"


async def enqueue_daily_health(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, Optional[str]]:
    """Today's (UTC) edge check (idempotent per day). Nothing while custom domains are off. Called by the ops metrics
    sampler on every tick: one indexed EXISTS once the day's run exists."""
    if not tp_edge.configured():
        return None, False, None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    key = health_key(now.date())
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, key
    job_id, created = await enqueue(pool, KIND, {"check": HEALTH_CHECK}, idempotency_key=key, created_by=SCHEDULER)
    return job_id, created, key


async def run_once(ctx: JobContext, acm: Any, cf: Any) -> dict:
    payload = ctx.payload or {}
    if payload.get("check") == HEALTH_CHECK:
        return await run_health(ctx, acm, cf)
    domain_id = payload.get("domain_id")
    if not isinstance(domain_id, str):
        raise NonRetryable("payload needs domain_id (str) or check = edge_health")
    return await run_domain(ctx, domain_id, acm, cf)


async def mark_terminal(pool, job_row: dict) -> None:
    """A job that ended without success leaves no host waiting: fail it, or record the failure on a removing host (it
    stays removing, Tenant 360 offers the removal again). The daily check has no host."""
    payload = job_row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    domain_id = payload.get("domain_id")
    if not isinstance(domain_id, str):
        return
    message = f"The setup job ended: {str(job_row.get('error') or job_row.get('status'))[:300]}"
    who = actor(str(job_row.get("id")))
    await pool.execute(FAILED_SQL, domain_id, "job_failed", message, False, who)
    await pool.execute(REMOVE_STUCK_SQL, domain_id, "job_failed", message, who)


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=120, background=True, on_terminal=mark_terminal)
async def run(ctx: JobContext, *, acm: Any = None, cf: Any = None) -> dict:
    if acm is None or cf is None:
        acm, cf = await asyncio.to_thread(tp_edge.clients)
    result = await run_once(ctx, acm, cf)
    result = json.loads(json.dumps(result, default=str))
    ctx.set_result(result)
    return result
