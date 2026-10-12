"""AA-764 (G5) — `t8_angles` job kind: generate + rank the 3 T8 angles for one request.

Moved out of `POST /v1/angle-gate/requests/{id}/goal`, which ran the LLM angle call and the Jev
answer checks inside the HTTP request (ADR 0003). The router now validates, records the goal and
enqueues this job (`services.acp_angle_gate.service.start_goal_generation()`); the portal polls
`GET /v1/angle-gate/requests/{id}` until the request leaves 'pending_goal'.

Payload: {"request_id", "tenant_id", "goal"}. `tenant_id` at the top level so the worker binds it
for llm_call_log / decide() (G1).
"""
from __future__ import annotations

from uuid import UUID

import structlog

from services.acp_angle_gate import service
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = service.T8_ANGLES_KIND


async def clear_goal(pool, job_row: dict) -> None:
    """on_terminal: the job ended without success — let the tenant choose a goal again."""
    request_id = (job_row.get("payload") or {}).get("request_id")
    if not request_id:
        return
    await service.clear_failed_generation(request_id, pool)
    logger.warning("t8_angles_job_failed", request_id=request_id, job_id=job_row.get("id"),
                   job_status=job_row.get("status"), error=(job_row.get("error") or "")[:300])


# S231 pilot: 15–19 s per request (one LLM call + up to 4 Jev checks).
@job_kind(KIND, concurrency=4, max_attempts=2, on_terminal=clear_goal, expected_seconds=120)
async def run(ctx: JobContext) -> dict:
    p = ctx.payload
    request_id, tenant_id, goal = p.get("request_id"), p.get("tenant_id"), p.get("goal")
    if not (request_id and tenant_id and goal):
        raise NonRetryable("payload needs request_id, tenant_id and goal")
    try:
        req = await service.fetch_request(UUID(tenant_id), UUID(request_id), ctx.pool)
    except service.RequestNotFoundError as e:
        raise NonRetryable(str(e))
    if req["status"] != "pending_goal":  # finished by an earlier attempt before the process died
        return {"skipped": True, "request_status": req["status"]}
    try:
        result = await service.set_goal_and_generate(UUID(tenant_id), UUID(request_id), goal, ctx.pool)
    except (service.InvalidGoalError, service.WrongStatusError) as e:
        raise NonRetryable(str(e))
    recommended = next((a["idx"] for a in result["angles"] if a.get("recommended")), None)
    return {"request_id": request_id, "angles": len(result["angles"]), "recommended_index": recommended}
