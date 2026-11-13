"""AA-1093 lane L2 — the report views of AA-Booking and TripPlanner (contract tenant.erasure.v1).

Each app owns its report view (booking.v_tenant_erasure_report_v1 from bk-057, tripplanner.v_tenant_erasure_report_v1
from tp-030) and grants SELECT on it to cis_app. Expected columns: the contract's ErasureReportRow (ids, codes, hashes,
counts only): report_id, request_id, round, mode (dry_run | apply | verify), status (ok | blocked | refused | failed),
error_code, plan_hash (sha256 hex), tables, blockers, census (verify), job_id, reported_at.
CIS reads every row of a request as JSON (``to_jsonb``), so a column an app adds later, or an optional one it lacks,
never breaks the read; saga.normalise_report keeps only the keys above. A view that does not exist yet, a schema
cis_app cannot use, a missing grant or a request_id column that is not uuid is ``app_not_ready``: a blocker of the
request, never a skip. A view that lacks a column the contract requires (REQUIRED_COLUMNS, read from the catalog
before any row, so an empty view is caught too) is ``app_not_ready`` with the code ``report_shape_unknown``; rows of
which none is contract-shaped (saga.report_shape_unknown) are the second line of defence.
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg

REPORT_VIEWS = {
    "booking": "booking.v_tenant_erasure_report_v1",
    "tripplanner": "tripplanner.v_tenant_erasure_report_v1",
}
# The contract's ErasureReportRow: every column is required (docs/contracts/tenant.erasure.v1.openapi.yaml).
REQUIRED_COLUMNS = ("report_id", "request_id", "tenant_id", "app", "round", "mode", "status", "error_code",
                    "plan_hash", "tables", "blockers", "census", "job_id", "reported_at")
_NOT_READY = (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.InvalidSchemaNameError,
              asyncpg.exceptions.InsufficientPrivilegeError, asyncpg.exceptions.UndefinedColumnError,
              asyncpg.exceptions.UndefinedFunctionError, asyncpg.exceptions.DatatypeMismatchError)
# The view's columns from the catalog: NULL relation (no such view) = no rows. to_regclass raises
# InsufficientPrivilegeError for a schema cis_app cannot use (app_not_ready too).
_COLUMNS_SQL = ("SELECT a.attname::text AS name FROM pg_catalog.pg_attribute a "
                "WHERE a.attrelid = to_regclass($1) AND a.attnum > 0 AND NOT a.attisdropped")
NOT_READY_CODE = "app_not_ready"
SHAPE_CODE = "report_shape_unknown"


class AppNotReady(Exception):
    """``code``: app_not_ready (no readable view) or report_shape_unknown (the view is outside the contract)."""

    def __init__(self, app: str, detail: str, code: str = NOT_READY_CODE):
        super().__init__(app, detail, code)
        self.app = app
        self.detail = detail
        self.code = code


async def _check_shape(conn, app: str, view: str) -> None:
    names = {r["name"] for r in await conn.fetch(_COLUMNS_SQL, view)}
    if not names:
        raise AppNotReady(app, f"report view {view} does not exist")
    missing = [c for c in REQUIRED_COLUMNS if c not in names]
    if missing:
        raise AppNotReady(app, f"report view {view} lacks the contract column(s) {', '.join(missing)}", SHAPE_CODE)


async def read(conn, app: str, request_id: str) -> list[dict[str, Any]]:
    """The app's report rows of one request, as dicts (JSON). Raises AppNotReady. Runs under a savepoint, so a
    missing view never aborts the caller's transaction."""
    view = REPORT_VIEWS[app]
    try:
        async with conn.transaction():
            await _check_shape(conn, app, view)
            rows = await conn.fetch(f"SELECT to_jsonb(v) AS r FROM {view} v WHERE v.request_id = $1::uuid",
                                    request_id)
    except _NOT_READY as e:
        raise AppNotReady(app, f"report view {view} not readable ({type(e).__name__})") from None
    out = []
    for row in rows:
        r = row["r"]
        if isinstance(r, str):
            r = json.loads(r)
        if isinstance(r, dict):
            out.append(r)
    return out
