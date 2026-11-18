"""AA-1126 L1 / L4 (S283) — refuse a tenant request that would start new AI writing while its plan's AI allowance for
this month is used up.

The gateway already holds every T-stage call of such a tenant (``shared/llm_client/tenant_budget.py``: the job ends
``stopped_budget`` and waits). Without this check the Content studio queued the work anyway and showed "Writing
failed" with a Retry that could only stop again. Now the request answers **402** with the distinct code
``ai_quota_exhausted`` (``tenant_budget.quota_refusal``: code, plain message, the day it resets), so the studio shows
the reason and its Upgrade link and keeps Retry off until the allowance resets.

Never blocks on a read error (L4): when the budget cannot be read the request goes on (logged); the gateway's own
check decides at call time (fail closed on Dev, degrade with a warning on prod, ``cis.spend.environment``).
``LLM_TENANT_BUDGETS=off`` switches this check off with the gateway's.
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import HTTPException

from shared import cost_guard
from shared.llm_client import tenant_budget

logger = structlog.get_logger()

PAYMENT_REQUIRED = 402
# The budget read (tests/unit/conftest.py replaces it: no database in unit tests).
reader = cost_guard.tenant_budget_check


async def allowance_used(conn: Any, tenant_id: str) -> bool:
    """True when ``tenant_id``'s AI budget holds new T-stage work now (hard stop on, spend at or past its approval
    threshold). False without a budget, with the check switched off, or when the budget cannot be read (L4)."""
    if not tenant_budget.enabled():
        return False
    try:
        budget = await reader(conn, str(tenant_id))
    except Exception as exc:  # noqa: BLE001 — L4: a read error never blocks the request
        logger.warning("tenant_ai_allowance_check_unreadable", tenant_id=str(tenant_id), error=str(exc)[:200])
        return False
    return budget is not None and budget.needs_approval


async def refuse_if_allowance_used(pool: Any, tenant_id: str, *, action: str) -> None:
    """402 ``ai_quota_exhausted`` when the tenant's plan AI allowance is used up (see the module docstring)."""
    async with pool.acquire() as conn:
        used = await allowance_used(conn, tenant_id)
    if used:
        logger.info("tenant_ai_allowance_refused", tenant_id=str(tenant_id), action=action)
        raise HTTPException(status_code=PAYMENT_REQUIRED, detail=tenant_budget.quota_refusal())
