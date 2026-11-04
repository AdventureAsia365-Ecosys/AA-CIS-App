"""
acp.hitl.approved / acp.hitl.rejected payload + publish helper — AA-186.

Gate-as-event-boundary (ADR-2026-013 Decision 3): a stage completes -> an
acp_shared.acp_hitl_requests row is created (status='pending', or
status='approved' if auto-approved) -> the pipeline STOPS. Approving a gate
(manually via /v1/acp/gate/{stage}/approve, or via Gate 1 auto-approve)
publishes acp.hitl.approved, which the next-stage trigger (AA-187) consumes
to start the next stage. Rejecting publishes acp.hitl.rejected and nothing
consumes it to chain forward.
"""
import json
import os

import boto3
import structlog

from services.acp_shared.event_constants import ACPEventSource

logger = structlog.get_logger()

EVENTBRIDGE_BUS = os.environ.get("ACP_EVENTBRIDGE_BUS", "aa-cis-dev-acp-events")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-1")

# gate -> stage to trigger on approve. None for gate 3 (post-S4) — no
# acp_hitl_requests-driven next stage exists today (AA-186 scope: gate 1/2 only).
NEXT_STAGE_BY_GATE = {1: 3, 2: 4, 3: None}


def build_hitl_event_payload(run_id: str, stage: int, gate: int,
                             decision: str, reviewer_type: str) -> dict:
    """Build the acp.hitl.approved/rejected detail payload (AA-186 contract).

    reviewer_type is "aa_internal" | "tenant_self" (the actor namespace of the retired AA-181
    verify_tenant_api_key(), removed by AA-1036; the event contract keeps the two values) — a different namespace than
    acp_hitl_requests.reviewer_type ("aa_internal"|"tenant_admin") and
    _audit_actor_type()'s audit_log.actor_type enum ("hitl_reviewer"|"tenant_admin").
    Do not source this field from either of those.
    """
    return {
        "run_id": run_id,
        "stage": stage,
        "gate": gate,
        "decision": decision,
        "reviewer_type": reviewer_type,
        "next_stage": NEXT_STAGE_BY_GATE.get(gate) if decision == "approved" else None,
    }


def publish_hitl_event(detail_type: str, payload: dict) -> bool:
    """Publish to aa-cis-dev-acp-events. Logs and returns False on failure — never raises."""
    try:
        eb = boto3.client("events", region_name=AWS_REGION)
        resp = eb.put_events(Entries=[{
            "Source": ACPEventSource.HITL,
            "DetailType": detail_type,
            "Detail": json.dumps(payload),
            "EventBusName": EVENTBRIDGE_BUS,
        }])
    except Exception as exc:
        logger.error("hitl_event_publish_error", detail_type=detail_type, payload=payload, error=str(exc))
        return False
    if resp.get("FailedEntryCount", 0):
        logger.error("hitl_event_publish_failed", detail_type=detail_type, payload=payload, response=str(resp))
        return False
    logger.info("hitl_event_published", detail_type=detail_type, run_id=payload.get("run_id"),
                stage=payload.get("stage"), gate=payload.get("gate"),
                decision=payload.get("decision"))
    return True
