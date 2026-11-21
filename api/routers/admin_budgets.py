"""AA-649 — spend budgets (shared.spend_budget, migration 168) for the cost guard.

  - GET /admin/budgets                       — every budget row + today's (UTC) spend per provider.
  - PUT /admin/budgets/{provider}/{scope}    — create/update one row (admin secret required).
  - DELETE /admin/budgets/llm/{scope}        — AA-1025 A7: remove one central LLM cap (provider 'llm' only).

scope is 'global' (daily cap for the provider) or 'job:<kind>' (e.g. 'job:segment_research').
The admin Settings → Budgets UI (AA-665) is built on these two endpoints.

AA-1025 A7 (CIS migration 340): provider 'llm' is a central LLM cap, checked by the gateway before any attempt
(shared/llm_client/central_budget.py). Its scope is app:<app> | stage:<stage> | job:<kind> | eval | adhoc; it may set
per_month_usd (the UTC month) and, for job:<kind> only, per_run_usd (the running job's spend); AA-1126: not for
job:s1_rewrite, whose per-run cap is the setting cis.spend.run_cap_s1_rewrite_usd (422 names it). A provider row keeps
global | job:<kind> and no month cap. Before 340 the database refuses an 'llm' row (409 with the reason).

AA-1155 (spec task T11, R5.5): every route needs a verified Cognito session (staff_authz.require_cognito_session: a
forged x-admin-role with only the admin secret gets 403), and every change writes one acp_shared.audit_log row
(``spend_budget.updated`` / ``spend_budget.deleted``, resource ``<provider>/<scope>``) with the row before and after,
in the same transaction as the write.
"""
from __future__ import annotations

import json
from typing import Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from api.core.staff_authz import staff_policy, require_cognito_session
from api.routers.admin import verify_admin_secret
from api.services import settings_registry
from services.acp_shared.audit_log import write_audit_log
from shared.cost_guard import CENTRAL_PROVIDER, PROVIDERS, day_spend
from shared.llm_client import caps_settings, central_budget

logger = structlog.get_logger()

# S241 (GĐ −1): every route of this router needs the admin secret. Router-level so a new GET can not
# ship open again (the reads here were public until S241). The inline verify_admin_secret() calls in
# the write routes are kept as-is (same check, run twice).
# AA-882 (GĐ1): staff_policy narrows the writes listed in api/core/staff_authz.py POLICY by role.
router = APIRouter(prefix="/admin/budgets", tags=["admin-budgets"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy),
                                 Depends(require_cognito_session)])   # AA-1155 T11: Cognito only


class BudgetUpdate(BaseModel):
    per_run_usd: Optional[float] = Field(None, ge=0)
    per_day_usd: Optional[float] = Field(None, ge=0)
    # AA-1025 A7: the UTC month cap of a central LLM row (provider 'llm' only).
    per_month_usd: Optional[float] = Field(None, ge=0)
    # None = the default of the row's provider: a provider row stops hard, a central 'llm' row only alerts (tech lead
    # decision T-8, S263: Dev central caps must never block normal traffic unless someone asks for a hard stop).
    hard_stop: Optional[bool] = None
    alert_pct: int = Field(80, ge=1, le=100)


def _valid(provider: str, scope: str, body: Optional[BudgetUpdate] = None) -> None:
    if provider == CENTRAL_PROVIDER:
        if not central_budget.SCOPE_RE.match(scope):
            raise HTTPException(status_code=422, detail="an llm scope must be 'app:<app>', 'stage:<stage>', "
                                                        "'job:<kind>', 'eval' or 'adhoc'")
        if body is not None and body.per_run_usd is not None and not scope.startswith("job:"):
            raise HTTPException(status_code=422, detail="a per-run cap applies to an llm 'job:<kind>' scope only")
        run_class = caps_settings.run_class_of_scope(scope)
        if body is not None and body.per_run_usd is not None and run_class:
            # AA-1126 (review S283): one source of truth: this run's per-run cap is a Platform › Settings value.
            raise HTTPException(status_code=422, detail=f"the per-run cap of llm {scope} is the setting "
                                f"{caps_settings.RUN_CAP_KEYS[run_class]} on Platform › Settings (area Spend)")
        return
    if provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail=f"provider must be one of {[*PROVIDERS, CENTRAL_PROVIDER]}")
    if scope != "global" and not scope.startswith("job:"):
        raise HTTPException(status_code=422, detail="scope must be 'global' or 'job:<kind>'")
    if body is not None and body.per_month_usd is not None:
        raise HTTPException(status_code=422, detail="a month cap applies to an llm (central) row only")


def budget_row(raw: dict) -> dict:
    """One spend_budget row (a ``to_jsonb`` of the row: ``per_month_usd`` is None before migration 340). Pure."""
    row = {k: raw.get(k) for k in ("provider", "scope", "per_run_usd", "per_day_usd", "per_month_usd", "hard_stop",
                                   "alert_pct", "updated_at", "updated_by")}
    for key in ("per_run_usd", "per_day_usd", "per_month_usd"):
        row[key] = float(row[key]) if row[key] is not None else None
    return row


BEFORE_SQL = "SELECT to_jsonb(b) FROM shared.spend_budget b WHERE provider = $1 AND scope = $2"
AUDIT_UPDATED = "spend_budget.updated"
AUDIT_DELETED = "spend_budget.deleted"
AUDIT_RESOURCE = "spend_budget"


def _row_of(raw) -> Optional[dict]:
    """The spend_budget row of a ``to_jsonb`` value (text or dict), or None."""
    if not raw:
        return None
    row = budget_row(json.loads(raw) if isinstance(raw, str) else dict(raw))
    row["updated_at"] = str(row["updated_at"]) if row.get("updated_at") is not None else None
    return row


async def _audit(conn, action: str, provider: str, scope: str, actor: Optional[str], before, after) -> None:
    """AA-1155 T11: one audit row with the budget before and after (``after`` None for a delete)."""
    await write_audit_log(conn, tenant_id=None, actor=f"admin:{actor}" if actor else "admin", action=action,
                          resource_type=AUDIT_RESOURCE, resource_id=f"{provider}/{scope}",
                          details={"provider": provider, "scope": scope, "before": before, "after": after})


UPSERT_SQL = """
    INSERT INTO shared.spend_budget
        (provider, scope, per_run_usd, per_day_usd, hard_stop, alert_pct, updated_at, updated_by)
    VALUES ($1, $2, $3, $4, $5, $6, now(), $7)
    ON CONFLICT (provider, scope) DO UPDATE SET
        per_run_usd = excluded.per_run_usd, per_day_usd = excluded.per_day_usd,
        hard_stop = excluded.hard_stop, alert_pct = excluded.alert_pct,
        updated_at = now(), updated_by = excluded.updated_by
"""
UPSERT_CENTRAL_SQL = """
    INSERT INTO shared.spend_budget
        (provider, scope, per_run_usd, per_day_usd, per_month_usd, hard_stop, alert_pct, updated_at, updated_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7, now(), $8)
    ON CONFLICT (provider, scope) DO UPDATE SET
        per_run_usd = excluded.per_run_usd, per_day_usd = excluded.per_day_usd,
        per_month_usd = excluded.per_month_usd, hard_stop = excluded.hard_stop, alert_pct = excluded.alert_pct,
        updated_at = now(), updated_by = excluded.updated_by
"""


@router.get("", summary="Spend budgets + today's spend per provider")
async def list_budgets(request: Request):
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT to_jsonb(b) AS row FROM shared.spend_budget b ORDER BY b.provider, b.scope")
        spent = {p: round(await day_spend(conn, p), 4) for p in PROVIDERS}
    budgets = [budget_row(json.loads(r["row"]) if isinstance(r["row"], str) else dict(r["row"])) for r in rows]
    return {"budgets": budgets, "spent_today_usd": spent}


@router.put("/{provider}/{scope}", summary="Create or update one spend budget")
async def upsert_budget(provider: str, scope: str, body: BudgetUpdate, request: Request,
                        x_admin_secret: str = Header(None), x_admin_user_id: str = Header(None),
                        x_admin_role: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    _valid(provider, scope, body)
    pool = request.app.state.pool
    central = provider == CENTRAL_PROVIDER
    hard_stop = body.hard_stop if body.hard_stop is not None else not central
    try:
        async with pool.acquire() as conn, conn.transaction():
            raw = await conn.fetchval(BEFORE_SQL, provider, scope)
            audit_before = _row_of(raw)
            if settings_registry.tighten_only(x_admin_role):
                # S279 thresholds L1 (tech lead Q1): ai_ops may lower a cap or harden it, never raise or remove one
                before = budget_row(json.loads(raw) if isinstance(raw, str) else dict(raw)) if raw else None
                if before and central and caps_settings.run_class_of_scope(scope):
                    before["per_run_usd"] = None     # AA-1126: not enforced (a setting), so clearing it loosens nothing
                after = {**body.model_dump(), "hard_stop": hard_stop}
                worse = settings_registry.loosened(before, after, caps=("per_run_usd", "per_day_usd", "per_month_usd"),
                                                   stops=("hard_stop",), up_is_looser=("alert_pct",))
                if worse:
                    raise HTTPException(status_code=403, detail=f"{settings_registry.TIGHTEN_ONLY}: {', '.join(worse)}")
            if central:
                await conn.execute(UPSERT_CENTRAL_SQL, provider, scope, body.per_run_usd, body.per_day_usd,
                                   body.per_month_usd, hard_stop, body.alert_pct, x_admin_user_id or "admin")
            else:
                await conn.execute(UPSERT_SQL, provider, scope, body.per_run_usd, body.per_day_usd, hard_stop,
                                   body.alert_pct, x_admin_user_id or "admin")
            after = {"provider": provider, "scope": scope, **body.model_dump(), "hard_stop": hard_stop}
            if not central:
                after.pop("per_month_usd", None)
            await _audit(conn, AUDIT_UPDATED, provider, scope, x_admin_user_id, audit_before, after)
    except (asyncpg.CheckViolationError, asyncpg.UndefinedColumnError) as e:
        if not central:
            raise
        # Before CIS migration 340 the table takes no 'llm' row (provider CHECK) and has no per_month_usd column.
        raise HTTPException(status_code=409,
                            detail="central LLM budgets need CIS migration 340 on this database") from e
    if central:
        central_budget.invalidate()             # this process sees the new cap at once; the others within a minute
    logger.info("spend_budget_updated", provider=provider, scope=scope, admin_user=x_admin_user_id,
                **body.model_dump())
    out = body.model_dump()
    if not central:
        out.pop("per_month_usd", None)
    return {"provider": provider, "scope": scope, **out}


@router.delete("/llm/{scope}", summary="Remove one central LLM cap")
async def delete_central_budget(scope: str, request: Request, x_admin_user_id: str = Header(None),
                                x_admin_role: Optional[str] = Header(None)):
    """AA-1025 A7: drop one ``llm`` row (the gateway stops checking that scope within a minute). Provider rows are
    never deleted here (their absence means "no cap" for the jobs' RunBudget, set them with PUT instead). S279: removing
    a cap loosens it, so a tighten-only role (ai_ops) is refused."""
    _valid(CENTRAL_PROVIDER, scope)
    if settings_registry.tighten_only(x_admin_role):
        raise HTTPException(status_code=403, detail=f"{settings_registry.TIGHTEN_ONLY}: removing the cap")
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        before = _row_of(await conn.fetchval(BEFORE_SQL, CENTRAL_PROVIDER, scope))
        status = await conn.execute("DELETE FROM shared.spend_budget WHERE provider = $1 AND scope = $2",
                                    CENTRAL_PROVIDER, scope)
        if status.endswith(" 0"):
            raise HTTPException(status_code=404, detail=f"no central LLM cap for {scope}")
        await _audit(conn, AUDIT_DELETED, CENTRAL_PROVIDER, scope, x_admin_user_id, before, None)
    central_budget.invalidate()
    logger.info("spend_budget_deleted", provider=CENTRAL_PROVIDER, scope=scope, admin_user=x_admin_user_id)
    return {"provider": CENTRAL_PROVIDER, "scope": scope, "deleted": True}
