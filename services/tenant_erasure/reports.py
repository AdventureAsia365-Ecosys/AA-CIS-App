"""AA-1093 lane L2 — the report views of AA-Booking and TripPlanner (contract tenant.erasure.v1).

Each app owns its report view (booking.v_tenant_erasure_report_v1 from bk-057, tripplanner.v_tenant_erasure_report_v1
from tp-030) and grants SELECT on it to cis_app. Expected columns: the contract's ErasureReportRow (ids, codes, hashes,
counts only): report_id, request_id, round, mode (dry_run | apply | verify), status (ok | blocked | refused | failed),
error_code, plan_hash (sha256 hex), tables, blockers, census (verify), job_id, reported_at.
CIS reads every row of a request as JSON (``to_jsonb``), so a column an app adds later, or an optional one it lacks,
never breaks the read; saga.normalise_report keeps only the keys above. A view that does not exist yet, a schema
cis_app cannot use or a missing grant is ``app_not_ready``: a blocker of the request, never a skip. Rows of which none
is contract-shaped (saga.report_shape_unknown) are ``app_not_ready`` with the code ``report_shape_unknown``.
"""
from __future__ import annotations

from typing import Any

import asyncpg

REPORT_VIEWS = {
    "booking": "booking.v_tenant_erasure_report_v1",
    "tripplanner": "tripplanner.v_tenant_erasure_report_v1",
}
_NOT_READY = (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.InvalidSchemaNameError,
              asyncpg.exceptions.InsufficientPrivilegeError, asyncpg.exceptions.UndefinedColumnError)


class AppNotReady(Exception):
    def __init__(self, app: str, detail: str):
        super().__init__(app, detail)
        self.app = app
        self.detail = detail


async def read(conn, app: str, request_id: str) -> list[dict[str, Any]]:
    """The app's report rows of one request, as dicts (JSON). Raises AppNotReady. Runs under a savepoint, so a
    missing view never aborts the caller's transaction."""
    view = REPORT_VIEWS[app]
    try:
        async with conn.transaction():
            rows = await conn.fetch(f"SELECT to_jsonb(v) AS r FROM {view} v WHERE v.request_id = $1::uuid",
                                    request_id)
    except _NOT_READY as e:
        raise AppNotReady(app, f"report view {view} not readable ({type(e).__name__})") from None
    out = []
    for row in rows:
        r = row["r"]
        if isinstance(r, str):
            import json
            r = json.loads(r)
        if isinstance(r, dict):
            out.append(r)
    return out
