"""tenant.erasure.v1 — the tenant erasure requests of the CIS offboarding saga, for the partner CRM (AA-1093 lane L2).

AA-Booking and TripPlanner read the same requests through the catalog view catalog.v_tenant_erasure_v1 (CIS 410) and
report through their own report views; the CRM has no database access to CIS, so it uses these two routes:

  GET  /tenant/v1/erasure-requests?after=&limit=        cursor list of the requests as the CRM needs them, oldest
                                                        change first; a request comes again after each change of its
                                                        phase, round or approval (the row's writing transaction is the
                                                        cursor; a late commit is never skipped)
  POST /tenant/v1/erasure-requests/{request_id}/reports  the CRM's report for one phase: {"mode": dry_run | apply |
                                                        verify, "status": done | failed | plan_changed | refused,
                                                        "plan_hash"?, "counts"?, "pii_left"?, "detail"?, "round"?}

What the CRM does per phase: scheduled = offer the owner a full export, nothing else; dry_run = compute the plan of the
tenant and report its hash; awaiting_approval = nothing; apply = from its not_before and before approval_expires_at,
recompute, match approved_plan_hash, erase (crypto-shred included) and report; verify = report the census of its
personal-data columns for the tenant (pii_left). Decision D2: the CRM erases after its 90-day window without the
owner's click.

Ids, codes, hashes, counts and timestamps only: no name, e-mail, reason or actor in either direction.

Auth: the M2M client-credentials token of the ``aa-m2m-<env>`` pool, client = AA-Partner (CRM), scope
``cis.tenant/erasure`` (``api/core/m2m_auth.require_m2m``; AA-CIS-Infra identity_m2m.tf adds the scope). 401 no / bad
token, 403 another client or a token without the scope, 503 key set or configuration unavailable or CIS 408 not
applied. The API Gateway route ``/tenant/v1/{tenant_v1_proxy+}`` already carries these paths. Errors use the contract
Problem body ``{error, message[, reason]}``.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.core import m2m_auth
from api.core.m2m_auth import M2MProblem
from services.tenant_erasure import saga, store
from services.tenant_membership.reader import ACQUIRE_TIMEOUT_S

logger = structlog.get_logger()
router = APIRouter(prefix="/tenant/v1", tags=["tenant-erasure"])

NO_STORE = "no-store"
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
_PREFIX = "e1"
_START = (0, uuid.UUID(int=0))

require_erasure = m2m_auth.require_m2m(
    m2m_auth.SCOPE_TENANT_ERASURE, realm="tenant.erasure.v1",
    refused="This client may not read or report tenant erasure requests.", log_prefix="tenant_erasure")


class ErasureRequestItem(BaseModel):
    """One request as the CRM sees it (contract tenant.erasure.v1 ErasureRequest)."""
    request_id: str
    tenant_id: str
    phase: Literal["scheduled", "dry_run", "awaiting_approval", "apply", "verify", "done", "cancelled", "failed"]
    request_mode: Literal["live", "drill"]
    round: int
    not_before: str
    approved_plan_hash: Optional[str] = None
    approval_expires_at: Optional[str] = None
    updated_at: str
    cursor: str


class ErasureRequestPage(BaseModel):
    items: list[ErasureRequestItem]
    next_cursor: str


class ErasureReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["dry_run", "apply", "verify"]
    status: Literal["done", "failed", "plan_changed", "refused"]
    plan_hash: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32,128}$")
    counts: dict[str, int] = Field(default_factory=dict, max_length=200)
    pii_left: Optional[int] = Field(None, ge=0)
    detail: Optional[str] = Field(None, max_length=500)
    round: Optional[int] = Field(None, ge=1, le=100)


def encode_cursor(txid: int, request_id: uuid.UUID) -> str:
    raw = f"{_PREFIX}:{txid}:{request_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: Optional[str]) -> tuple[int, uuid.UUID]:
    if not value:
        return _START
    try:
        if len(value) > 200 or not value.isascii():
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        prefix, txid, rid = raw.split(":")
        if prefix != _PREFIX or not txid.isdigit() or int(txid) >= 2 ** 63:
            raise ValueError
        return int(txid), uuid.UUID(rid)
    except ValueError:
        raise M2MProblem(400, "invalid", "after is not a cursor this API issued") from None


def parse_limit(value: Optional[str]) -> int:
    if value is None or value == "":
        return DEFAULT_LIMIT
    if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= MAX_LIMIT:
        raise M2MProblem(400, "invalid", f"limit must be an integer from 1 to {MAX_LIMIT}")
    return int(value)


def _unavailable(exc: BaseException) -> M2MProblem:
    logger.error("tenant_erasure_unavailable", reason=type(exc).__name__)
    return M2MProblem(503, "unavailable", "Erasure requests are unavailable; retry later.",
                      headers={"Retry-After": "5"})


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def item_body(row) -> dict:
    return {
        "request_id": str(row["request_id"]), "tenant_id": str(row["tenant_id"]), "phase": row["phase"],
        "request_mode": row["mode"], "round": int(row["round"]), "not_before": _iso(row["not_before_crm"]),
        "approved_plan_hash": row["approved_plan_hash"], "approval_expires_at": _iso(row["approval_expires_at"]),
        "updated_at": _iso(row["updated_at"]),
        "cursor": encode_cursor(int(row["txid_text"]), uuid.UUID(str(row["request_id"]))),
    }


def _pool(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise _unavailable(RuntimeError("no pool"))
    return pool


@router.get("/erasure-requests", responses={200: {"model": ErasureRequestPage}})
async def list_erasure_requests(request: Request, after: Optional[str] = None, limit: Optional[str] = None,
                                caller: m2m_auth.M2MCaller = Depends(require_erasure)) -> JSONResponse:
    txid, rid = decode_cursor(after)
    size = parse_limit(limit)
    try:
        async with _pool(request).acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
            async with conn.transaction(readonly=True):
                rows = await conn.fetch(store.CRM_FEED_SQL, str(txid), rid, size)
    except store.NOT_MIGRATED as exc:
        raise _unavailable(exc) from exc
    items = [item_body(r) for r in rows]
    next_cursor = items[-1]["cursor"] if items else (after or encode_cursor(*_START))
    logger.info("tenant_erasure_served", op="listErasureRequests", client_id=caller.client_id, items=len(items))
    return JSONResponse(content={"items": items, "next_cursor": next_cursor}, headers={"Cache-Control": NO_STORE})


@router.post("/erasure-requests/{request_id}/reports")
async def report_erasure_step(request_id: str, request: Request,
                              caller: m2m_auth.M2MCaller = Depends(require_erasure)) -> JSONResponse:
    try:
        rid = str(uuid.UUID(request_id))
    except ValueError:
        raise M2MProblem(404, "not_found", "No erasure request has this id.") from None
    try:
        body = ErasureReportIn.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise M2MProblem(400, "invalid", "The report body is not valid.", reason=type(exc).__name__[:40]) from None
    if body.mode == "dry_run" and body.status == "done" and not body.plan_hash:
        raise M2MProblem(400, "invalid", "A done dry_run report carries its plan_hash.", reason="plan_hash")
    if body.mode == "verify" and body.status == "done" and body.pii_left is None:
        raise M2MProblem(400, "invalid", "A done verify report carries pii_left.", reason="pii_left")
    now = datetime.now(timezone.utc)
    try:
        async with _pool(request).acquire(timeout=ACQUIRE_TIMEOUT_S) as conn, conn.transaction():
            req = await store.get_request(conn, rid, for_update=True)
            if req is None:
                raise M2MProblem(404, "not_found", "No erasure request has this id.")
            if body.round is not None and body.round != int(req["round"]):
                raise M2MProblem(409, "conflict", "The report is for another round of this request.", reason="round")
            crm_apply = await conn.fetchval(
                "SELECT status FROM shared.tenant_erasure_step WHERE request_id = $1::uuid AND round = $2 "
                "AND app = 'crm' AND mode = 'apply'", rid, int(req["round"]))
            refusal = saga.report_refusal(req, mode=body.mode, now=now, crm_apply_done=crm_apply == "done")
            if refusal is not None:
                raise M2MProblem(refusal.status, "conflict", refusal.message, reason=refusal.code)
            await store.upsert_step(conn, rid, int(req["round"]), "crm", body.mode, body.status,
                                    plan_hash=body.plan_hash, counts=body.counts, pii_left=body.pii_left,
                                    detail=body.detail, reported_at=now)
    except store.NOT_MIGRATED as exc:
        raise _unavailable(exc) from exc
    logger.info("tenant_erasure_reported", client_id=caller.client_id, request_id=rid, mode=body.mode,
                status=body.status)
    return JSONResponse(content={"request_id": rid, "app": "crm", "mode": body.mode, "round": int(req["round"]),
                                 "status": body.status, "recorded": True}, headers={"Cache-Control": NO_STORE})
