"""AA-649 — spend budgets (shared.spend_budget, migration 168) for the cost guard.

  - GET /admin/budgets                       — every budget row + today's (UTC) spend per provider.
  - PUT /admin/budgets/{provider}/{scope}    — create/update one row (admin secret required).

scope is 'global' (daily cap for the provider) or 'job:<kind>' (e.g. 'job:segment_research').
The admin Settings → Budgets UI (AA-665) is built on these two endpoints.
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret
from shared.cost_guard import PROVIDERS, day_spend

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/budgets", tags=["admin-budgets"])


class BudgetUpdate(BaseModel):
    per_run_usd: Optional[float] = Field(None, ge=0)
    per_day_usd: Optional[float] = Field(None, ge=0)
    hard_stop: bool = True
    alert_pct: int = Field(80, ge=1, le=100)


def _valid(provider: str, scope: str) -> None:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail=f"provider must be one of {list(PROVIDERS)}")
    if scope != "global" and not scope.startswith("job:"):
        raise HTTPException(status_code=422, detail="scope must be 'global' or 'job:<kind>'")


@router.get("", summary="AA-649 — spend budgets + today's spend per provider")
async def list_budgets(request: Request):
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT provider, scope, per_run_usd, per_day_usd, hard_stop, alert_pct, updated_at, updated_by "
            "FROM shared.spend_budget ORDER BY provider, scope")
        spent = {p: round(await day_spend(conn, p), 4) for p in PROVIDERS}
    budgets = []
    for r in rows:
        row = dict(r)
        for key in ("per_run_usd", "per_day_usd"):
            row[key] = float(row[key]) if row[key] is not None else None
        row["updated_at"] = row["updated_at"].isoformat() if row["updated_at"] else None
        budgets.append(row)
    return {"budgets": budgets, "spent_today_usd": spent}


@router.put("/{provider}/{scope}", summary="AA-649 — create or update one spend budget")
async def upsert_budget(provider: str, scope: str, body: BudgetUpdate, request: Request,
                        x_admin_secret: str = Header(None), x_admin_user_id: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    _valid(provider, scope)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO shared.spend_budget
                (provider, scope, per_run_usd, per_day_usd, hard_stop, alert_pct, updated_at, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, now(), $7)
            ON CONFLICT (provider, scope) DO UPDATE SET
                per_run_usd = excluded.per_run_usd, per_day_usd = excluded.per_day_usd,
                hard_stop = excluded.hard_stop, alert_pct = excluded.alert_pct,
                updated_at = now(), updated_by = excluded.updated_by
            """,
            provider, scope, body.per_run_usd, body.per_day_usd, body.hard_stop, body.alert_pct,
            x_admin_user_id or "admin",
        )
    logger.info("spend_budget_updated", provider=provider, scope=scope, admin_user=x_admin_user_id,
                **body.model_dump())
    return {"provider": provider, "scope": scope, **body.model_dump()}
