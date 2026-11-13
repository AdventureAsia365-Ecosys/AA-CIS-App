"""AA-1093 lane L2 — shared.tenant_erasure_request / tenant_erasure_step (CIS 408) and the feed event
account.erasure_requested (tenant.entitlements.v1 1.7, CIS 409).

Every write is a conditional UPDATE on the phase it expects (WHERE phase = ...), so a restore racing the job, two
job runs or a double click can never move a request twice. The offboard / restore hooks run inside the route's own
transaction under a savepoint: before CIS 408 is applied they do nothing (the offboard still works).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Optional

import asyncpg
import structlog

from services.tenant_erasure import saga

logger = structlog.get_logger()

EVENT = "account.erasure_requested"
REQUEST_COLUMNS = (
    "request_id, tenant_id, mode, phase, round, offboarded_at, not_before_cis, not_before_booking, "
    "not_before_tripplanner, not_before_crm, combined_fingerprint, fingerprint_at, approved_fingerprint, approved_by, "
    "approved_at, approval_expires_at, snapshot_id, blockers, receipt, created_by, created_at, updated_at, "
    "cancelled_at, done_at")
STEP_COLUMNS = ("request_id, round, app, mode, status, plan_hash, approved_plan_hash, counts, pii_left, detail, "
                "job_id, reported_at, updated_at")
# Not applied yet: the hooks skip, the routes answer 503.
NOT_MIGRATED = (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError)


class ErasureInProgress(Exception):
    """A restore after the apply started: the tenant's data is (partly) gone."""


def _row(r: Any) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    for k in ("blockers", "receipt", "counts"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    for k in ("request_id", "tenant_id", "job_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


async def emit(conn, tenant_id: str, change: dict) -> bool:
    """One account.erasure_requested event (ids and timestamps, never a reason or actor). Under its own savepoint: a
    database without CIS 409 refuses the type (CheckViolation) and the request is still recorded."""
    try:
        async with conn.transaction():
            await conn.execute("SELECT shared.tenant_account_emit($1, $2::uuid, $3::jsonb)", EVENT, tenant_id,
                               json.dumps(change, default=str))
        return True
    except (asyncpg.exceptions.CheckViolationError, asyncpg.exceptions.UndefinedFunctionError) as e:
        logger.warning("tenant_erasure_event_not_emitted", tenant_id=tenant_id, reason=type(e).__name__)
        return False


def event_change(request: dict, status: str) -> dict:
    return {"request_id": str(request["request_id"]), "status": status, "mode": request["mode"],
            "not_before": {app: saga._iso(v) for app, v in saga.not_before(request).items()}}


async def create_request(conn, tenant_id: str, *, offboarded_at: datetime, mode: str, actor: str,
                         env: Optional[dict] = None) -> dict:
    """INSERT one request (phase scheduled) and emit the event. Raises UniqueViolation when an open one exists."""
    nb = saga.schedule(offboarded_at, mode, env)
    row = await conn.fetchrow(
        f"INSERT INTO shared.tenant_erasure_request (tenant_id, mode, offboarded_at, not_before_cis, "
        f"not_before_booking, not_before_tripplanner, not_before_crm, created_by) "
        f"VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8) RETURNING {REQUEST_COLUMNS}",
        tenant_id, mode, offboarded_at, nb["cis"], nb["booking"], nb["tripplanner"], nb["crm"], str(actor)[:64])
    req = _row(row)
    await emit(conn, tenant_id, event_change(req, "scheduled"))
    return req


async def on_offboard(conn, tenant_id: str, *, offboarded_at: datetime, actor: str, is_pilot: bool,
                      env: Optional[dict] = None) -> dict:
    """The T0 hook of POST /admin/tenants/{id}/offboard, inside its transaction. Returns what the route reports:
    {"status": "scheduled", ...} | {"status": "skipped", "reason": ...}. Never raises for a missing migration."""
    if is_pilot:
        return {"status": "skipped", "reason": "pilot tenant: never scheduled by the saga (decision D8)"}
    try:
        async with conn.transaction():
            req = await create_request(conn, tenant_id, offboarded_at=offboarded_at, mode="live", actor=actor, env=env)
    except NOT_MIGRATED:
        return {"status": "skipped", "reason": "erasure saga tables not migrated (CIS 408)"}
    except asyncpg.exceptions.UniqueViolationError:
        return {"status": "skipped", "reason": "an open erasure request already exists"}
    return {"status": "scheduled", "request_id": req["request_id"],
            "not_before": {app: saga._iso(v) for app, v in saga.not_before(req).items()}}


async def open_request(conn, tenant_id: str, *, for_update: bool = False) -> Optional[dict]:
    lock = " FOR UPDATE" if for_update else ""
    return _row(await conn.fetchrow(
        f"SELECT {REQUEST_COLUMNS} FROM shared.tenant_erasure_request WHERE tenant_id = $1::uuid "
        f"AND phase = ANY($2::text[]){lock}", tenant_id, list(saga.OPEN_PHASES)))


async def on_restore(conn, tenant_id: str) -> Optional[str]:
    """The hook of POST /admin/tenants/{id}/restore, inside its transaction: cancels the open request (returns its
    id) or raises ErasureInProgress once the apply started. None when there is none or CIS 408 is not applied."""
    try:
        async with conn.transaction():
            req = await open_request(conn, tenant_id, for_update=True)
            if req is None:
                return None
            if req["phase"] not in saga.CANCELLABLE_PHASES:
                raise ErasureInProgress(f"the erasure of this tenant is in phase {req['phase']}: the apply started")
            await conn.execute(
                "UPDATE shared.tenant_erasure_request SET phase = 'cancelled', cancelled_at = now() "
                "WHERE request_id = $1::uuid AND phase = ANY($2::text[])", req["request_id"],
                list(saga.CANCELLABLE_PHASES))
            await emit(conn, tenant_id, event_change(req, "cancelled"))
            return req["request_id"]
    except NOT_MIGRATED:
        return None


async def get_request(conn, request_id: str, *, for_update: bool = False) -> Optional[dict]:
    lock = " FOR UPDATE" if for_update else ""
    return _row(await conn.fetchrow(
        f"SELECT {REQUEST_COLUMNS} FROM shared.tenant_erasure_request WHERE request_id = $1::uuid{lock}", request_id))


async def list_requests(conn, *, limit: int = 50, tenant_id: Optional[str] = None) -> list[dict]:
    rows = await conn.fetch(
        f"SELECT {REQUEST_COLUMNS} FROM shared.tenant_erasure_request "
        f"WHERE ($2::uuid IS NULL OR tenant_id = $2::uuid) ORDER BY created_at DESC, request_id LIMIT $1",
        limit, tenant_id)
    return [_row(r) for r in rows]


async def steps(conn, request_id: str, round_: Optional[int] = None) -> list[dict]:
    rows = await conn.fetch(
        f"SELECT {STEP_COLUMNS} FROM shared.tenant_erasure_step WHERE request_id = $1::uuid "
        f"AND ($2::smallint IS NULL OR round = $2::smallint) ORDER BY round, app, mode", request_id, round_)
    return [_row(r) for r in rows]


def by_app_mode(rows: Iterable[dict]) -> dict[tuple[str, str], dict]:
    return {(r["app"], r["mode"]): r for r in rows}


async def upsert_step(conn, request_id: str, round_: int, app: str, mode: str, status: str, *,
                      plan_hash: Optional[str] = None, counts: Optional[dict] = None, pii_left: Optional[int] = None,
                      detail: Optional[str] = None, job_id: Optional[str] = None,
                      reported_at: Optional[datetime] = None) -> None:
    """Writes the step row of (request, round, app, mode). approved_plan_hash is kept (set only by approve)."""
    await conn.execute(
        "INSERT INTO shared.tenant_erasure_step (request_id, round, app, mode, status, plan_hash, counts, pii_left, "
        "detail, job_id, reported_at) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10::uuid, $11) "
        "ON CONFLICT (request_id, round, app, mode) DO UPDATE SET status = EXCLUDED.status, "
        "plan_hash = EXCLUDED.plan_hash, counts = EXCLUDED.counts, pii_left = EXCLUDED.pii_left, "
        "detail = EXCLUDED.detail, job_id = coalesce(EXCLUDED.job_id, shared.tenant_erasure_step.job_id), "
        "reported_at = EXCLUDED.reported_at, updated_at = now()",
        request_id, int(round_), app, mode, status, plan_hash, json.dumps(counts or {}), pii_left,
        (str(detail)[:500] if detail else None), job_id, reported_at)


async def move(conn, request_id: str, from_phases: Iterable[str], to_phase: str, **cols: Any) -> bool:
    """Conditional phase change (plus the given columns). False when the request was not in ``from_phases``."""
    sets, args = ["phase = $3"], [request_id, list(from_phases), to_phase]
    for k, v in cols.items():
        if k not in _MOVABLE:
            raise ValueError(f"column {k} cannot be set by move()")
        args.append(json.dumps(v, default=str) if k in ("blockers", "receipt") else v)
        cast = "::jsonb" if k in ("blockers", "receipt") else ""
        sets.append(f"{k} = ${len(args)}{cast}")
    tag = await conn.execute(
        f"UPDATE shared.tenant_erasure_request SET {', '.join(sets)} "
        f"WHERE request_id = $1::uuid AND phase = ANY($2::text[])", *args)
    return tag.endswith(" 1")


_MOVABLE = frozenset({"blockers", "receipt", "combined_fingerprint", "fingerprint_at", "approved_fingerprint",
                      "approved_by", "approved_at", "approval_expires_at", "snapshot_id", "round", "mode",
                      "not_before_cis", "not_before_booking", "not_before_tripplanner", "not_before_crm",
                      "cancelled_at", "done_at"})


async def set_blockers(conn, request_id: str, phase: str, blockers: list[dict]) -> None:
    """Records the current blockers without a phase change (only while the request is still in ``phase``)."""
    await conn.execute(
        "UPDATE shared.tenant_erasure_request SET blockers = $3::jsonb WHERE request_id = $1::uuid AND phase = $2 "
        "AND blockers IS DISTINCT FROM $3::jsonb", request_id, phase, json.dumps(blockers, default=str))


# ── the CRM feed (GET /tenant/v1/erasure-requests) ──────────────────────────────────────────────────────────────────

# Rows whose writing transaction is older than every open one (txid kept by the CIS 408 trigger), ordered by
# (txid, request_id): a late commit is never skipped. Every column the WHERE / ORDER BY use is qualified (the
# membership feed's S256 lesson: an unqualified ORDER BY txid next to txid::text sorts by the text).
CRM_FEED_SQL = """
    SELECT r.request_id, r.tenant_id, r.phase, r.mode, r.round, r.not_before_crm, s.approved_plan_hash,
           r.approval_expires_at, r.updated_at, r.txid::text AS txid_text
      FROM shared.tenant_erasure_request r
      LEFT JOIN shared.tenant_erasure_step s
             ON s.request_id = r.request_id AND s.round = r.round AND s.app = 'crm' AND s.mode = 'dry_run'
     WHERE r.txid < pg_snapshot_xmin(pg_current_snapshot())
       AND (r.txid, r.request_id) > ($1::text::xid8, $2::uuid)
     ORDER BY r.txid, r.request_id
     LIMIT $3
"""
