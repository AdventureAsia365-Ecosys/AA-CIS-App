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
    "request_id, tenant_id, mode, phase, round, opened_at, is_pilot, is_synthetic, not_before_cis, not_before_booking, "
    "not_before_tripplanner, not_before_crm, round_apps, done_apps, combined_fingerprint, fingerprint_at, "
    "approved_fingerprint, approved_by, approved_at, approval_expires_at, snapshot_id, blockers, receipt, created_by, "
    "created_at, updated_at, cancelled_at, done_at")
STEP_COLUMNS = ("request_id, round, app, mode, status, report_id, error_code, plan_hash, approved_plan_hash, tables, "
                "blockers, census, job_id, reported_at, updated_at")
# Not applied yet: the hooks skip, the routes answer 503.
NOT_MIGRATED = (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError)


class ErasureInProgress(Exception):
    """A restore after the apply started: the tenant's data is (partly) gone."""


def _row(r: Any) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    for k in ("blockers", "receipt", "tables", "census"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    for k in ("round_apps", "done_apps"):
        if k in d:
            d[k] = list(d[k] or [])
    for k in ("request_id", "tenant_id", "job_id", "report_id"):
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


def event_change(request: dict, op: str) -> dict:
    """tenant.entitlements.v1 1.7 ErasureRequestChange: {op: requested | cancelled, request_id}."""
    return {"op": op, "request_id": str(request["request_id"])}


# is_pilot / is_synthetic copied at T0 (the request outlives the tenant row; the apps' guards read the copy).
_TENANT_FLAGS_SQL = ("SELECT coalesce(is_pilot, false) AS is_pilot, (is_canary IS TRUE AND NOT is_pilot) "
                     "AS is_synthetic FROM shared.tenants WHERE tenant_id = $1::uuid")


async def create_request(conn, tenant_id: str, *, opened_at: datetime, mode: str, actor: str,
                         env: Optional[dict] = None) -> dict:
    """INSERT one request (phase dry_run, no app active until its not_before) and emit the event. Raises
    UniqueViolation when an open one exists."""
    nb = saga.schedule(opened_at, mode, env)
    flags = await conn.fetchrow(_TENANT_FLAGS_SQL, tenant_id)
    row = await conn.fetchrow(
        f"INSERT INTO shared.tenant_erasure_request (tenant_id, mode, opened_at, is_pilot, is_synthetic, "
        f"not_before_cis, not_before_booking, not_before_tripplanner, not_before_crm, created_by) "
        f"VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING {REQUEST_COLUMNS}",
        tenant_id, mode, opened_at, bool(flags and flags["is_pilot"]), bool(flags and flags["is_synthetic"]),
        nb["cis"], nb["booking"], nb["tripplanner"], nb["crm"], str(actor)[:64])
    req = _row(row)
    await emit(conn, tenant_id, event_change(req, "requested"))
    return req


async def on_offboard(conn, tenant_id: str, *, actor: str, offboarded_at: Optional[datetime] = None,
                      is_pilot: Optional[bool] = None, env: Optional[dict] = None) -> dict:
    """The T0 hook of POST /admin/tenants/{id}/offboard, inside its transaction (after the UPDATE that set
    cancelled_at; offboarded_at / is_pilot default to the tenant row). Returns what the route reports:
    {"status": "scheduled", ...} | {"status": "skipped", "reason": ...}. Never raises for a missing migration."""
    if offboarded_at is None or is_pilot is None:
        row = await conn.fetchrow(
            "SELECT cancelled_at, is_pilot FROM shared.tenants WHERE tenant_id = $1::uuid", tenant_id)
        offboarded_at = offboarded_at or row["cancelled_at"]
        is_pilot = bool(row["is_pilot"]) if is_pilot is None else is_pilot
    if is_pilot:
        return {"status": "skipped", "reason": "pilot tenant: never scheduled by the saga (decision D8)"}
    try:
        async with conn.transaction():
            req = await create_request(conn, tenant_id, opened_at=offboarded_at, mode="live", actor=actor, env=env)
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
            applied = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM shared.tenant_erasure_step WHERE request_id = $1::uuid "
                "AND mode = 'apply' AND status = 'ok') AS ok", req["request_id"])
            if req["phase"] not in saga.CANCELLABLE_PHASES or applied or req["done_apps"]:
                raise ErasureInProgress(f"the erasure of this tenant is in phase {req['phase']} and an app may already "
                                        "have erased its part")
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
                      plan_hash: Optional[str] = None, tables: Optional[list] = None, blockers: Optional[list] = None,
                      census: Optional[dict] = None, error_code: Optional[str] = None, report_id: Optional[str] = None,
                      job_id: Optional[str] = None, reported_at: Optional[datetime] = None) -> None:
    """Writes the newest report of (request, round, app, mode). approved_plan_hash is kept (set only by approve)."""
    await conn.execute(
        "INSERT INTO shared.tenant_erasure_step (request_id, round, app, mode, status, plan_hash, tables, blockers, "
        "census, error_code, report_id, job_id, reported_at) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, "
        "$8::jsonb, $9::jsonb, $10, $11::uuid, $12::uuid, $13) "
        "ON CONFLICT (request_id, round, app, mode) DO UPDATE SET status = EXCLUDED.status, "
        "plan_hash = EXCLUDED.plan_hash, tables = EXCLUDED.tables, blockers = EXCLUDED.blockers, "
        "census = EXCLUDED.census, error_code = EXCLUDED.error_code, report_id = EXCLUDED.report_id, "
        "job_id = coalesce(EXCLUDED.job_id, shared.tenant_erasure_step.job_id), "
        "reported_at = EXCLUDED.reported_at, updated_at = now()",
        request_id, int(round_), app, mode, status, plan_hash, json.dumps(tables or []), json.dumps(blockers or []),
        json.dumps(census) if census is not None else None, error_code, report_id, job_id, reported_at)


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
                      "round_apps", "done_apps",
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
# membership feed's S256 lesson: an unqualified ORDER BY txid next to txid::text sorts by the text). Every change of
# the CRM step (round start, approval, done) is a write of the request row, so it moves the row's txid.
CRM_FEED_SQL = """
    SELECT r.request_id, r.tenant_id, r.phase, r.round, r.opened_at, r.approval_expires_at, r.is_pilot,
           r.is_synthetic, r.round_apps, r.done_apps, r.not_before_crm, r.updated_at,
           CASE WHEN r.phase IN ('apply', 'verify') THEN s.approved_plan_hash END AS approved_plan_hash,
           EXISTS (SELECT 1 FROM shared.tenant_erasure_step f
                    WHERE f.request_id = r.request_id AND f.round = r.round AND f.app = 'crm'
                      AND f.status IN ('blocked', 'refused', 'failed')) AS crm_failed,
           r.txid::text AS txid_text
      FROM shared.tenant_erasure_request r
      LEFT JOIN shared.tenant_erasure_step s
             ON s.request_id = r.request_id AND s.round = r.round AND s.app = 'crm' AND s.mode = 'dry_run'
     WHERE r.txid < pg_snapshot_xmin(pg_current_snapshot())
       AND (r.txid, r.request_id) > ($1::text::xid8, $2::uuid)
     ORDER BY r.txid, r.request_id
     LIMIT $3
"""
