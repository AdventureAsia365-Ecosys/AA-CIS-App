"""
/v1/account/tour-retirement — AA-1039 follow-up (tech lead decision S276, root ADR 0019 d20 amended): the agency's
setting "Automatically move posts about a discontinued tour to draft after N days" (Content studio › Settings).

  GET   /v1/account/tour-retirement   {"auto_draft": bool, "notice_days": int, "min_days": 1, "max_days": 30,
                                       "default_notice_days": 7}
  PATCH /v1/account/tour-retirement   {"auto_draft": bool, "notice_days": 1..30 (optional)} -> the same shape

Off by default: with no agency row (or only the platform row) a retirement changes nothing on the agency's site; the
fan-out writes notice-only flags and the agency moves pages to draft itself with one click
(services/tour_lifecycle/fanout.py, POST /v1/publish-log/{publish_id}/move-to-draft). Turning it on makes the next
retirements write ``draft_after_notice`` with the chosen number of days; turning it off also stops the automatic
drafts already scheduled (the draft job re-reads the setting). Stored on ``acp_shared.tour_retirement_policy`` (the
agency's own row: ``auto_draft`` CIS 395, ``notice_days`` CIS 360).

Tenant-JWT-gated (``get_tenant``; a staff token is 403); the tenant id comes ONLY from the verified token. The PATCH is
a ``/v1/account`` write, so the tenant role middleware lets only the Content studio owner make it (editor, viewer and a
"view as tenant" session get 403). Main pool (cis_app; the table has no tenant-pool grant) with the tenant condition in
every statement. Each change writes one audit row ``tenant.tour_retirement_settings_changed`` (before / after). 503
while CIS 395 is not applied.
"""
from __future__ import annotations

import uuid
from typing import Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.routers.v1_tours import get_tenant
from services.acp_publish.retired_pages import actor_of
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log
from services.tour_lifecycle.fanout import DEFAULT_NOTICE_DAYS, POLICY_MIGRATION

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/account", tags=["tenant-account"])

MIN_DAYS = 1
MAX_DAYS = 30
UNAVAILABLE = "This setting is not available yet."

# The agency's own row and the platform row (its notice period is the default shown when the agency has no row).
_READ_SQL = """
    SELECT tenant_id IS NOT NULL AS own, notice_days, auto_draft
      FROM acp_shared.tour_retirement_policy
     WHERE tenant_id = $1::uuid OR tenant_id IS NULL
     ORDER BY (tenant_id IS NULL)
"""

# The unique index tour_retirement_policy_scope_key is on COALESCE(tenant_id, zero uuid): the conflict target names it.
_UPSERT_SQL = """
    INSERT INTO acp_shared.tour_retirement_policy (tenant_id, notice_days, auto_draft, updated_by)
    VALUES ($1::uuid, $2, $3, $4)
    ON CONFLICT ((COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)))
    DO UPDATE SET notice_days = EXCLUDED.notice_days, auto_draft = EXCLUDED.auto_draft,
                  updated_by = EXCLUDED.updated_by, updated_at = now()
    RETURNING notice_days, auto_draft
"""


class TourRetirementSettings(BaseModel):
    auto_draft: bool
    notice_days: int
    min_days: int = MIN_DAYS
    max_days: int = MAX_DAYS
    default_notice_days: int = DEFAULT_NOTICE_DAYS


class TourRetirementSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_draft: bool
    notice_days: Optional[int] = Field(None, ge=MIN_DAYS, le=MAX_DAYS)


def _tenant_id(tenant: dict) -> str:
    sub = tenant.get("sub")
    try:
        return str(uuid.UUID(str(sub)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="Tenant token required")


def _shape(auto_draft: bool, notice_days: Optional[int]) -> dict:
    days = int(notice_days) if notice_days else DEFAULT_NOTICE_DAYS
    return {"auto_draft": bool(auto_draft), "notice_days": min(max(days, MIN_DAYS), MAX_DAYS),
            "min_days": MIN_DAYS, "max_days": MAX_DAYS, "default_notice_days": DEFAULT_NOTICE_DAYS}


async def _current(conn, tenant_id: str) -> dict:
    rows = await conn.fetch(_READ_SQL, tenant_id)
    own = next((r for r in rows if r["own"]), None)
    platform = next((r for r in rows if not r["own"]), None)
    if own is not None:
        return _shape(own["auto_draft"], own["notice_days"])
    return _shape(False, platform["notice_days"] if platform else None)


def _unavailable(exc: Exception) -> HTTPException:
    logger.error("tour_retirement_settings_unavailable", reason=type(exc).__name__, migration=POLICY_MIGRATION)
    return HTTPException(status_code=503, detail=UNAVAILABLE)


@router.get("/tour-retirement", response_model=TourRetirementSettings,
            summary="The agency's setting for posts about a discontinued tour (automatic draft, off by default)")
async def get_tour_retirement_settings(request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_id(tenant)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await _current(conn, tenant_id)
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as exc:
        raise _unavailable(exc) from exc


@router.patch("/tour-retirement", response_model=TourRetirementSettings,
              summary="Turn automatic drafts of posts about a discontinued tour on or off (Content studio owner)")
async def set_tour_retirement_settings(body: TourRetirementSettingsIn, request: Request,
                                       tenant=Depends(get_tenant)):
    tenant_id = _tenant_id(tenant)
    actor = actor_of(tenant, tenant_id)
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                before = await _current(conn, tenant_id)
                days = body.notice_days if body.notice_days is not None else before["notice_days"]
                row = await conn.fetchrow(_UPSERT_SQL, tenant_id, days, body.auto_draft, actor)
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as exc:
        raise _unavailable(exc) from exc
    after = _shape(row["auto_draft"], row["notice_days"])
    logger.info("tour_retirement_settings_changed", tenant_id=tenant_id, auto_draft=after["auto_draft"],
                notice_days=after["notice_days"])
    if (before["auto_draft"], before["notice_days"]) != (after["auto_draft"], after["notice_days"]):
        try:  # MAIN: audit_log has no tenant-pool grant; best-effort (the setting is the record that counts)
            await write_audit_log(
                pool, tenant_id=tenant_id, actor=actor, action=TenantAuditAction.TOUR_RETIREMENT_SETTINGS_CHANGED,
                resource_type="tenant", resource_id=tenant_id,
                details={"before": {"auto_draft": before["auto_draft"], "notice_days": before["notice_days"]},
                         "after": {"auto_draft": after["auto_draft"], "notice_days": after["notice_days"]}})
        except Exception as exc:  # noqa: BLE001
            logger.warning("tour_retirement_settings_audit_failed", tenant_id=tenant_id, error=str(exc))
    return after
