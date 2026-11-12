"""AA-1039 R6 slice 2 — admin Tenant content QA › Compliance (root ADR 0019 d20 / d21).

  GET  /admin/tenant-qa/compliance
      The flags of acp_shared.content_compliance_flag (CIS 360), newest first; filters state (open | resolved | all),
      kind (subject_gate | tour_retired), tenant_id; with the tenant name, the piece title and channel, the post URL
      and the counts per kind of the open flags.
  POST /admin/tenant-qa/compliance/{flag_id}/release
      Staff release of an open subject_gate flag for the text the gate read (resolution released, that text's sha256
      kept, so the publish gate lets exactly that text through); a reason and the flag's ``content_sha256`` are
      required. 409 when the piece's text changed since the check (the tenant edited it): the reviewed links are not
      the ones that would publish, so staff re-run the check (the next publish attempt) and review again.

Flags are admin data (memory: the tenant sees content only); tenants read the reason through the publish answer and
the Publish list, never this route. Reads every staff role may make (TENANT_READ); the release is QUALITY_WRITE and
audited. 503 until CIS 360 is applied.

AA-1087 (S278, root ADR 0006a A10b-3): the Dev-only E2E CIS admin (A10b) may release a flag of a SYNTHETIC tenant only
(``shared.tenants.is_canary IS TRUE`` and not the pilot, api/core/e2e_staff.is_synthetic_tenant): any other flag is
403 ``e2e_read_only`` right after the flag is read and before it is resolved or audited (log
``compliance_release_e2e_refused`` with ``reason``). Any other Dev-only E2E identity (A10d) is refused outright. Every
other staff member is unaffected.
"""
from __future__ import annotations

import json
from typing import Literal, Optional
from uuid import UUID

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core import e2e_staff
from api.core.staff_authz import request_staff_identity, staff_policy
from services.acp_shared import subject_gate
from services.acp_shared.audit_log import write_audit_log
from services.tour_lifecycle.fanout import MIGRATION

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/tenant-qa/compliance", tags=["admin-tenant-compliance"],
                   dependencies=[Depends(staff_policy)])

UNAVAILABLE = (f"Compliance is not available yet: migration {MIGRATION} (acp_shared.content_compliance_flag) is "
               "not applied.")
TEXT_CHANGED = ("The piece's text changed since this check, so the links shown are not the ones that would publish. "
                "Ask the agency to publish again (the check runs again), then review the new result.")
RELEASE_ACTION = "content_compliance.released"

LIST_SQL = """
    SELECT f.flag_id::text AS flag_id, f.tenant_id::text AS tenant_id, t.name AS tenant_name, f.kind, f.stage,
           f.piece_id::text AS piece_id, f.publish_id::text AS publish_id, f.tour_id::text AS tour_id, f.channel,
           COALESCE(f.external_url, pl.external_url) AS external_url, pl.status AS publish_status, f.codes, f.detail,
           f.tenant_message, f.action, f.draft_due_at, f.drafted_at, f.state, f.resolution, f.resolved_at,
           f.resolved_by, f.created_at, f.updated_at,
           COALESCE(cp.seo_title, ago.name) AS piece_title,
           COALESCE((SELECT pt.aa_name FROM gold_aa_internal.published_tours pt WHERE pt.tour_id = f.tour_id),
                    f.detail ->> 'tour_name') AS tour_name
      FROM acp_shared.content_compliance_flag f
      LEFT JOIN shared.tenants t ON t.tenant_id = f.tenant_id
      LEFT JOIN acp_shared.publish_log pl ON pl.publish_id = f.publish_id
      LEFT JOIN acp_shared.content_piece cp ON cp.piece_id = f.piece_id
      LEFT JOIN acp_shared.angle_gate_option ago ON ago.option_id = cp.angle_gate_option_id
     WHERE ($1::text = 'all' OR f.state = $1)
       AND ($2::text IS NULL OR f.kind = $2)
       AND ($3::uuid IS NULL OR f.tenant_id = $3)
     ORDER BY f.created_at DESC, f.flag_id
     LIMIT $4 OFFSET $5
"""
COUNT_SQL = """
    SELECT count(*) FROM acp_shared.content_compliance_flag f
     WHERE ($1::text = 'all' OR f.state = $1) AND ($2::text IS NULL OR f.kind = $2)
       AND ($3::uuid IS NULL OR f.tenant_id = $3)
"""
OPEN_COUNTS_SQL = """
    SELECT kind, count(*)::int AS n FROM acp_shared.content_compliance_flag WHERE state = 'open' GROUP BY kind
"""


def _iso(v):
    return v.isoformat() if v is not None else None


def _row(r) -> dict:
    d = dict(r)
    detail = d.get("detail")
    d["detail"] = json.loads(detail) if isinstance(detail, str) else dict(detail or {})
    d["codes"] = list(d.get("codes") or [])
    for k in ("draft_due_at", "drafted_at", "resolved_at", "created_at", "updated_at"):
        d[k] = _iso(d.get(k))
    return d


@router.get("")
async def list_flags(request: Request,
                     state: Literal["open", "resolved", "all"] = "open",
                     kind: Optional[Literal["subject_gate", "tour_retired"]] = None,
                     tenant_id: Optional[UUID] = None,
                     limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    pool = request.app.state.pool
    tid = str(tenant_id) if tenant_id else None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(LIST_SQL, state, kind, tid, limit, offset)
            total = await conn.fetchval(COUNT_SQL, state, kind, tid)
            counts = {r["kind"]: r["n"] for r in await conn.fetch(OPEN_COUNTS_SQL)}
    except asyncpg.UndefinedTableError:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return {"data": [_row(r) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset,
            "open_counts": {"subject_gate": counts.get("subject_gate", 0),
                            "tour_retired": counts.get("tour_retired", 0)}}


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=3, max_length=500)
    # the hash of the text the gate read (the flag's detail.content_sha256, which the page shows the links of)
    content_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")


RELEASE_FLAG_SQL = f"""
    SELECT f.flag_id::text AS flag_id, f.tenant_id::text AS tenant_id, f.piece_id::text AS piece_id, f.state, f.kind,
           f.detail ->> 'content_sha256' AS checked_sha256, cp.content_text
      FROM acp_shared.content_compliance_flag f
      JOIN acp_shared.content_piece cp ON cp.piece_id = f.piece_id
     WHERE f.flag_id = $1::uuid AND f.kind = '{subject_gate.KIND}'
     FOR UPDATE OF f, cp
"""
RELEASE_SQL = """
    UPDATE acp_shared.content_compliance_flag
       SET state = 'resolved', resolution = 'released', resolved_at = now(), resolved_by = $2, updated_at = now(),
           detail = detail || $3::jsonb
     WHERE flag_id = $1::uuid AND state = 'open'
    RETURNING flag_id::text AS flag_id, resolved_at
"""


async def _refuse_e2e(request: Request, conn, tenant_id: str, flag_id: UUID) -> None:
    """AA-1087 (A10b-3): the A10b session releases a synthetic tenant's flag only; any other Dev-only E2E identity
    never. 403 ``e2e_read_only`` before any write or audit row. No-op for every other staff member."""
    identity = request_staff_identity(request)
    if not getattr(identity, "e2e_read_only", False):
        return
    if not e2e_staff.has_allowlisted_writes(identity):
        logger.warning("compliance_release_e2e_refused", flag_id=str(flag_id), reason="not_allowlisted")
        raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)
    if not await e2e_staff.is_synthetic_tenant(conn, UUID(tenant_id)):
        logger.warning("compliance_release_e2e_refused", flag_id=str(flag_id), tenant_id=tenant_id,
                       reason="tenant_not_synthetic")
        raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)


@router.post("/{flag_id}/release")
async def release_flag(flag_id: UUID, body: ReleaseBody, request: Request,
                       x_admin_user_id: Optional[str] = Header(None)):
    actor = f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"
    reason = " ".join(body.reason.split())
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                flag = await conn.fetchrow(RELEASE_FLAG_SQL, str(flag_id))
                if flag is None:
                    raise HTTPException(status_code=404, detail="Flag not found (only a subject check can be released)")
                await _refuse_e2e(request, conn, flag["tenant_id"], flag_id)
                if flag["state"] != "open":
                    raise HTTPException(status_code=409, detail="This flag is already resolved")
                sha = subject_gate.content_sha256(flag["content_text"] or "")
                if not (body.content_sha256 == flag["checked_sha256"] == sha):
                    raise HTTPException(status_code=409, detail=TEXT_CHANGED,
                                        headers={"X-Error-Code": "content_changed_since_check"})
                row = await conn.fetchrow(RELEASE_SQL, str(flag_id), actor[:128],
                                          json.dumps({"content_sha256": sha, "release_reason": reason}))
    except asyncpg.UndefinedTableError:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    try:
        await write_audit_log(pool, tenant_id=flag["tenant_id"], actor=actor, action=RELEASE_ACTION,
                              resource_type="content_piece", resource_id=flag["piece_id"],
                              details={"flag_id": str(flag_id), "reason": reason, "content_sha256": sha})
    except Exception:  # noqa: BLE001 — the release stands; a lost audit row is logged
        logger.warning("compliance_release_audit_failed", flag_id=str(flag_id), exc_info=True)
    return {"flag_id": row["flag_id"], "state": "resolved", "resolution": "released",
            "resolved_at": _iso(row["resolved_at"]), "resolved_by": actor}


__all__ = ["router"]
