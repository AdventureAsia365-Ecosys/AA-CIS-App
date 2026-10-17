"""AA-727 — rerun waves as objects (shared.rerun_wave, migration 217).

  POST /admin/waves                 create a wave: expected tour set written BEFORE the run
                                    (explicit tour_ids, or every active raw tour of a country)
  GET  /admin/waves                 list (newest first), optional ?status=
  GET  /admin/waves/{id}/report     4-column report + states + failure class + Jev coverage + cost
  POST /admin/waves/{id}/close      close it (ends the job window)
  POST /admin/waves/check           evaluate every open wave; incomplete → one admin bell
                                    (called daily by the ops-probe Lambda, AA-CIS-Infra)

Starting the runs is unchanged (Rewrite page / run-tour-async / s1-batch); the report derives each
tour's state from what the pipeline already writes — see services/waves/report.py.
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from api.core.staff_authz import staff_policy
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret
from services.waves import report as wave_report

logger = structlog.get_logger()
# AA-882 (GĐ3): staff_policy narrows every route listed in api/core/staff_authz.py POLICY by role.
router = APIRouter(prefix="/admin/waves", tags=["admin-waves"], dependencies=[Depends(staff_policy)])

_STATUSES = ("open", "complete", "incomplete", "closed")

_COUNTRY_TOURS_SQL = """
    SELECT tour_id FROM silver_aa_internal.raw_tours
     WHERE tenant_id = $1::uuid AND source_status = 'active' AND deleted_at IS NULL
       AND lower(country) = lower($2)
     ORDER BY src_name
"""


class WaveCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    country: Optional[str] = Field(None, max_length=100)
    tour_ids: Optional[list[str]] = Field(None, max_length=2000)
    notes: Optional[str] = Field(None, max_length=2000)


@router.post("", summary="AA-727 — create a rerun wave (expected tour set)")
async def create_wave(body: WaveCreate, request: Request, x_admin_secret: str = Header(None),
                      x_admin_user_id: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        if body.tour_ids:
            ids = list(dict.fromkeys(t.strip() for t in body.tour_ids if t and t.strip()))
            found = await conn.fetch(
                "SELECT tour_id::text FROM silver_aa_internal.raw_tours WHERE tour_id::text = ANY($1::text[])", ids)
            missing = sorted(set(ids) - {r["tour_id"] for r in found})
            if missing:
                raise HTTPException(status_code=422, detail={"error": "unknown tour_ids", "tour_ids": missing[:20]})
        elif body.country:
            ids = [str(r["tour_id"]) for r in await conn.fetch(
                _COUNTRY_TOURS_SQL, wave_report.MASTER_TENANT_ID, body.country.strip())]
        else:
            raise HTTPException(status_code=422, detail="give tour_ids or a country")
        if not ids:
            raise HTTPException(status_code=422, detail="the expected set is empty")
        row = await conn.fetchrow(
            """
            INSERT INTO shared.rerun_wave (name, country, expected_tour_ids, notes, created_by)
            VALUES ($1, $2, $3::uuid[], $4, $5)
            RETURNING *
            """,
            body.name.strip(), (body.country or "").strip() or None, ids, body.notes,
            f"admin:{x_admin_user_id}" if x_admin_user_id else "admin")
    logger.info("rerun_wave_created", wave_id=str(row["id"]), name=row["name"], expected=len(ids))
    return wave_report._wave_dict(row)


@router.get("", summary="AA-727 — list rerun waves")
async def list_waves(request: Request, status: Optional[str] = Query(None, description="|".join(_STATUSES)),
                     limit: int = Query(50, ge=1, le=200), x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    if status and status not in _STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {_STATUSES}")
    rows = await request.app.state.pool.fetch(
        "SELECT * FROM shared.rerun_wave WHERE ($1::text IS NULL OR status = $1) "
        "ORDER BY created_at DESC LIMIT $2", status, limit)
    return {"data": [wave_report._wave_dict(r) for r in rows]}


@router.post("/check", summary="AA-727 — evaluate open waves; bell when one ends incomplete")
async def check_waves(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    ids = [str(r["id"]) for r in await pool.fetch("SELECT id FROM shared.rerun_wave WHERE status = 'open'")]
    out = []
    for wave_id in ids:
        try:
            async with pool.acquire() as conn:
                out.append({"wave_id": wave_id, **(await wave_report.check(conn, wave_id) or {})})
        except Exception as exc:      # one bad wave must not hide the others
            logger.warning("rerun_wave_check_failed", wave_id=wave_id, error=str(exc)[:300])
            out.append({"wave_id": wave_id, "error": str(exc)[:300]})
    return {"checked": len(out), "waves": out}


@router.get("/{wave_id}/report", summary="AA-727 — wave report (4 columns, states, Jev coverage, cost)")
async def wave_report_endpoint(wave_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        rep = await wave_report.report(conn, _uuid(wave_id))
    if rep is None:
        raise HTTPException(status_code=404, detail="wave not found")
    return rep


@router.post("/{wave_id}/close", summary="AA-727 — close a wave (ends its job window)")
async def close_wave(wave_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    row = await request.app.state.pool.fetchrow(
        "UPDATE shared.rerun_wave SET status = 'closed', closed_at = coalesce(closed_at, now()) "
        "WHERE id = $1::uuid RETURNING *", _uuid(wave_id))
    if row is None:
        raise HTTPException(status_code=404, detail="wave not found")
    return wave_report._wave_dict(row)


def _uuid(value: str) -> str:
    import uuid
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=404, detail="wave not found")


__all__ = ["router"]
