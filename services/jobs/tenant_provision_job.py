"""AA-1028 R1 slice 2 — job kind ``tenant_provision``: create a tenant with its subscriptions, or add add-ons to one,
as one idempotent action (root ADR 0019 d1; the saga's rules are in ``services/tenant_account/provision.py``).

Payload: ``services.tenant_account.provision.ProvisionRequest.to_payload()`` (no secret and no personal data: the
tenant's trading name, slug, tier, kind, add-on plans and seats, the requesting admin's id).

Enqueued by ``POST /admin/tenants/provision`` (create) and ``POST /admin/tenants/{tenant_id}/provision`` (attach),
idempotency key ``tenant_provision:<request_key>``: the same request twice is one job. Status:
``GET /admin/tenants/provision/{job_id}``.

Failures: a refused request (slug taken, unknown plan, offboarded tenant, ...) -> NonRetryable with its code; a lost
connection or a deadlock is retried (max_attempts 3). The database steps are one transaction, so a retry after a
rollback starts again and a retry after a commit returns the stored outcome (``resumed``).
"""
from __future__ import annotations

import structlog

from services.tenant_account import provision as pv
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = pv.KIND


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=60)
async def run(ctx: JobContext) -> dict:
    try:
        req = pv.parse(ctx.payload or {})
    except pv.ProvisionError as e:
        raise NonRetryable(str(e)) from e
    await ctx.progress(phase="provisioning", mode=req.mode)
    try:
        result = await pv.provision(ctx.pool, req, job_id=ctx.job_id)
    except pv.ProvisionError as e:
        logger.warning("tenant_provision_refused", job_id=ctx.job_id, code=e.code, mode=req.mode)
        raise NonRetryable(str(e)) from e
    await ctx.progress(phase="done", tenant_id=result["tenant_id"], steps=result["steps"],
                       events_written=result["events_written"], resumed=result["resumed"])
    logger.info("tenant_provision_done", job_id=ctx.job_id, mode=req.mode, tenant_id=result["tenant_id"],
                steps=",".join(result["steps"]), events=result["events_written"], resumed=result["resumed"])
    ctx.set_result(result)
    return result


async def enqueue_request(pool, req: pv.ProvisionRequest) -> tuple[str, bool]:
    """One job per request key: (job_id, created). A second call with the same key returns the first job."""
    created_by = f"admin:{req.requested_by}" if req.requested_by else "admin"
    return await enqueue(pool, KIND, req.to_payload(), idempotency_key=req.idempotency_key, created_by=created_by)
