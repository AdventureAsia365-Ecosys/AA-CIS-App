"""tenant.erasure.v1 — the tenant erasure requests of the CIS offboarding saga, for the partner CRM (AA-1093 lane L2).

Contract: root ``docs/contracts/tenant.erasure.v1.openapi.yaml`` (byte copy in ``docs/contracts/`` of this repo, checked
by ``tests/unit/test_aa1093_tenant_erasure_contract.py``). AA-Booking and TripPlanner read the same requests through
the catalog view catalog.v_tenant_erasure_v1 (CIS 410) and report through their own report views; the CRM has no
database access to CIS, so it uses these two routes:

  GET  /tenant/v1/erasure-requests?after=&limit=        cursor feed of the requests that changed, oldest change first,
                                                        each with the CRM step only (status waiting / active / done /
                                                        failed, its not_before, its approved plan hash); a request
                                                        comes again after each change of its phase, round or CRM step
                                                        (the row's writing transaction is the cursor; a late commit is
                                                        never skipped)
  POST /tenant/v1/erasure-requests/{request_id}/reports  the CRM's report (ErasureReport: dry_run, apply or verify of
                                                        its step); idempotent on report_id: 201 recorded, 200 the same
                                                        report again, 409 report_conflict / round_superseded /
                                                        phase_mismatch, 403 wrong_app (the CRM reports its own step)

Decision D2: the CRM erases after its 90-day window (stage 2) without the owner's click. Ids, codes, hashes, counts and
timestamps only: no name, e-mail, reason or actor in either direction.

Auth: the M2M client-credentials token of the ``aa-m2m-<env>`` pool, client = AA-Partner (CRM), scope
``cis.tenant/erasure`` (``api/core/m2m_auth.require_m2m``; AA-CIS-Infra identity_m2m.tf adds the scope). 401 no / bad
token, 403 another client or a token without the scope, 503 key set or configuration unavailable or CIS 408 not
applied. The API Gateway route ``/tenant/v1/{tenant_v1_proxy+}`` already carries these paths. Errors use the contract
Problem body ``{error, message}``.
"""
from __future__ import annotations

import base64
import json
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
MAX_LIMIT = 200
_PREFIX = "e1"
_START = (0, uuid.UUID(int=0))

require_erasure = m2m_auth.require_m2m(
    m2m_auth.SCOPE_TENANT_ERASURE, realm="tenant.erasure.v1",
    refused="This client may not read or report tenant erasure requests.", log_prefix="tenant_erasure")


class ErasureStep(BaseModel):
    app: Literal["crm"]
    status: Literal["waiting", "active", "done", "failed"]
    not_before: str
    approved_plan_hash: Optional[str] = None


class ErasureRequestItem(BaseModel):
    """One request as the CRM sees it (contract ErasureRequest)."""
    request_id: str
    tenant_id: str
    phase: Literal["dry_run", "awaiting_approval", "apply", "verify", "done", "failed", "cancelled"]
    round: int
    opened_at: str
    approval_expires_at: Optional[str] = None
    is_pilot: bool
    is_synthetic: bool
    steps: list[ErasureStep]
    updated_at: str
    cursor: str


class ErasureRequestPage(BaseModel):
    items: list[ErasureRequestItem]
    next_cursor: str


_SHA256 = r"^[0-9a-f]{64}$"
_CODE = r"^[a-z][a-z0-9_]{1,63}$"


class TableEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str = Field(..., pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$", max_length=127)
    action: Literal["delete", "cascade", "detach", "merge", "redact", "anonymise", "keep", "shred"]
    rows: int = Field(..., ge=0)
    digest: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32}$")


class ReportBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., pattern=_CODE)
    count: int = Field(..., ge=1)


class Census(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pii_columns_checked: int = Field(..., ge=0)
    pii_rows_found: int = Field(..., ge=0)


class ErasureReportIn(BaseModel):
    """The contract's ErasureReport (every field required, nullable where the contract says so)."""
    model_config = ConfigDict(extra="forbid")
    report_id: uuid.UUID
    request_id: uuid.UUID
    tenant_id: uuid.UUID
    app: Literal["cis", "crm", "booking", "tripplanner"]
    round: int = Field(..., ge=1)
    mode: Literal["dry_run", "apply", "verify"]
    status: Literal["ok", "blocked", "refused", "failed"]
    error_code: Optional[str] = Field(..., pattern=_CODE)
    plan_hash: Optional[str] = Field(..., pattern=_SHA256)
    tables: list[TableEntry] = Field(..., max_length=500)
    blockers: list[ReportBlocker] = Field(..., max_length=50)
    census: Optional[Census]
    job_id: Optional[uuid.UUID]
    reported_at: datetime


class ReportAccepted(BaseModel):
    report_id: str
    created: bool
    phase: Literal["dry_run", "awaiting_approval", "apply", "verify", "done", "failed", "cancelled"]


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
    request = {"done_apps": list(row["done_apps"] or []), "round_apps": list(row["round_apps"] or [])}
    return {
        "request_id": str(row["request_id"]), "tenant_id": str(row["tenant_id"]), "phase": row["phase"],
        "round": int(row["round"]), "opened_at": _iso(row["opened_at"]),
        "approval_expires_at": _iso(row["approval_expires_at"]), "is_pilot": bool(row["is_pilot"]),
        "is_synthetic": bool(row["is_synthetic"]),
        "steps": [{"app": "crm", "status": saga.step_status(request, "crm", bool(row["crm_failed"])),
                   "not_before": _iso(row["not_before_crm"]), "approved_plan_hash": row["approved_plan_hash"]}],
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


def _same(step: dict, body: ErasureReportIn) -> bool:
    """The stored report equals this body (idempotency of report_id)."""
    def plain(v):
        return json.loads(json.dumps(v, default=str))
    return (step["mode"] == body.mode and int(step["round"]) == body.round and step["status"] == body.status
            and step.get("error_code") == body.error_code and step.get("plan_hash") == body.plan_hash
            and plain(step.get("tables") or []) == plain([t.model_dump() for t in body.tables])
            and plain(step.get("blockers") or []) == plain([b.model_dump() for b in body.blockers])
            and plain(step.get("census")) == plain(body.census.model_dump() if body.census else None))


@router.post("/erasure-requests/{request_id}/reports",
             responses={200: {"model": ReportAccepted}, 201: {"model": ReportAccepted}})
async def report_erasure_step(request_id: str, request: Request,
                              caller: m2m_auth.M2MCaller = Depends(require_erasure)) -> JSONResponse:
    try:
        rid = str(uuid.UUID(request_id))
    except ValueError:
        raise M2MProblem(404, "not_found", "No erasure request has this id.") from None
    try:
        body = ErasureReportIn.model_validate(await request.json())
    except (ValidationError, ValueError):
        raise M2MProblem(422, "invalid", "The report body does not match tenant.erasure.v1 ErasureReport.") from None
    if body.app != "crm":
        raise M2MProblem(403, "wrong_app", "The CRM client may report only its own (crm) step.")
    if str(body.request_id) != rid:
        raise M2MProblem(422, "invalid", "request_id in the body differs from the path.")
    if body.mode in ("dry_run", "apply") and body.status == "ok" and not body.plan_hash:
        raise M2MProblem(422, "invalid", "An ok dry_run or apply report carries its plan_hash.")
    if body.mode == "verify" and body.status == "ok" and body.census is None:
        raise M2MProblem(422, "invalid", "An ok verify report carries its census.")
    report_id = str(body.report_id)
    try:
        async with _pool(request).acquire(timeout=ACQUIRE_TIMEOUT_S) as conn, conn.transaction():
            req = await store.get_request(conn, rid, for_update=True)
            if req is None:
                raise M2MProblem(404, "not_found", "No erasure request has this id.")
            if str(body.tenant_id) != req["tenant_id"]:
                raise M2MProblem(422, "invalid", "tenant_id differs from the request's tenant.")
            seen = await conn.fetchrow(
                f"SELECT {store.STEP_COLUMNS} FROM shared.tenant_erasure_step WHERE app = 'crm' "
                "AND report_id = $1::uuid", report_id)
            if seen is not None:
                if str(seen["request_id"]) == rid and _same(store._row(seen), body):
                    return JSONResponse(status_code=200, headers={"Cache-Control": NO_STORE},
                                        content={"report_id": report_id, "created": False, "phase": req["phase"]})
                raise M2MProblem(409, "report_conflict", "This report_id was recorded with another body.")
            apply_ok = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM shared.tenant_erasure_step WHERE request_id = $1::uuid AND round = $2 "
                "AND app = 'crm' AND mode = 'apply' AND status = 'ok') AS ok", rid, body.round)
            refusal = saga.report_refusal(req, app="crm", mode=body.mode, report_round=body.round,
                                          apply_ok=bool(apply_ok))
            if refusal is not None:
                raise M2MProblem(refusal.status, refusal.code, refusal.message)
            await store.upsert_step(conn, rid, body.round, "crm", body.mode, body.status, plan_hash=body.plan_hash,
                                    tables=[t.model_dump() for t in body.tables],
                                    blockers=[b.model_dump() for b in body.blockers],
                                    census=body.census.model_dump() if body.census else None,
                                    error_code=body.error_code, report_id=report_id,
                                    job_id=str(body.job_id) if body.job_id else None,
                                    reported_at=body.reported_at)
    except store.NOT_MIGRATED as exc:
        raise _unavailable(exc) from exc
    logger.info("tenant_erasure_reported", client_id=caller.client_id, request_id=rid, mode=body.mode,
                status=body.status, round=body.round)
    return JSONResponse(status_code=201, headers={"Cache-Control": NO_STORE},
                        content={"report_id": report_id, "created": True, "phase": req["phase"]})
