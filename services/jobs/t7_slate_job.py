"""AA-764 (G5) — `t7_slate` job kind: propose one tenant's Slate (T7 candidates + Debate).

Moved out of `GET /v1/slate`, which ran propose_slate() — including Debate's Jev + LLM brand-fit
calls — on every page load (S231 pilot: 2′48″–4′+, 504 at the gateway for all 4 tenants, and paid
every time). GET /v1/slate now only reads; it enqueues this job when the tenant has no proposal
yet or the last one is stale (`services.acp_shared.slate.request_slate_proposal()`), and
`POST /v1/slate/refresh` enqueues it on demand.

Payload: {"tenant_id"} — top level, so the worker binds it for llm_call_log / decide() (G1).
"""
from __future__ import annotations

from uuid import UUID

from services.acp_shared import slate
from shared.jobs.registry import JobContext, NonRetryable, job_kind

KIND = slate.T7_SLATE_KIND


# S231 pilot: 2′48″–4′+ per tenant (Debate on ~60 candidates).
@job_kind(KIND, concurrency=2, max_attempts=2, expected_seconds=600)
async def run(ctx: JobContext) -> dict:
    tenant_id = ctx.payload.get("tenant_id")
    if not tenant_id:
        raise NonRetryable("payload needs tenant_id")
    await ctx.progress(phase="proposing")
    result = await slate.propose_slate(UUID(tenant_id), ctx.pool)
    await ctx.progress(phase="done")
    return result
