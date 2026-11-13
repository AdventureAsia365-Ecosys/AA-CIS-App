"""AA-1093 lane L2 — the report views of AA-Booking and TripPlanner (contract tenant.erasure.v1).

Each app owns its report view (booking.v_tenant_erasure_report_v1 from bk-056, tripplanner.v_tenant_erasure_report_v1
from tp-030) and grants SELECT on it to cis_app. Expected columns (ids, codes, hashes, counts only):
  request_id uuid, round smallint, mode text (dry_run | apply | verify), status text (done | failed | plan_changed |
  refused), plan_hash text (hex), counts jsonb (per action), pii_left bigint (verify), detail text, reported_at
  timestamptz.
CIS reads every row of a request as JSON (``to_jsonb``), so a column an app adds later, or an optional one it lacks,
never breaks the read; saga.normalise_report keeps only the keys above. A view that does not exist yet, a schema
cis_app cannot use or a missing grant is ``app_not_ready``: a blocker of the request, never a skip.
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
