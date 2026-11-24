"""AA-1141 (P1 feature 7) — named saved views of the Ecosystem status page, per staff member (S285 review idea 7,
Grafana style): the page's view keys (the URL state: range, compare, app / severity / status / event filters, hidden
series, annotation types, sorts) under a name such as "Wave watch" or "AI spend".

Rows in ``acp_shared.ops_saved_view`` (CIS 482), read and written only by their owner (``staff_id`` = the signed-in
staff member; a request without one is refused). Saving a name that exists (ignoring case and outer spaces) replaces
that view. ``clean_query`` is pure and unit-tested: only the page's own keys, each a short string, so a saved view can
never carry anything else into the page.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import asyncpg

VIEW_KEYS = ("range", "from", "to", "compare", "apps", "sev", "st", "svc", "ev", "hide", "sort", "ann", "chg")
VALUE_MAX = 600  # technical-constant: the longest value of one view key (the URL state is short)
NAME_MAX = 80  # technical-constant: the table CHECK on the view name (CIS 482)
READ_ROWS = 100  # technical-constant: rows of a read
NOT_MIGRATED = "saved views need database migration 482"
_SAFE = re.compile(r"^[A-Za-z0-9_.:,\-+TZ ]*$")

LIST_SQL = f"""
    SELECT view_id::text AS id, name, query, created_at, updated_at
      FROM acp_shared.ops_saved_view
     WHERE staff_id = $1::uuid
     ORDER BY lower(name)
     LIMIT {READ_ROWS}
"""
UPSERT_SQL = """
    INSERT INTO acp_shared.ops_saved_view (staff_id, name, query)
    VALUES ($1::uuid, $2, $3::jsonb)
    ON CONFLICT (staff_id, lower(btrim(name))) DO UPDATE SET query = EXCLUDED.query, name = EXCLUDED.name,
                                                             updated_at = now()
    RETURNING view_id::text AS id, name, query, created_at, updated_at
"""
DELETE_SQL = "DELETE FROM acp_shared.ops_saved_view WHERE view_id = $1::uuid AND staff_id = $2::uuid RETURNING view_id"


class ViewError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.code, self.message = code, message

    def __str__(self) -> str:
        return self.message


def clean_name(name: Any) -> str:
    text = " ".join(str(name or "").split())
    if not 1 <= len(text) <= NAME_MAX:
        raise ViewError("bad_name", f"Name the view in 1 to {NAME_MAX} characters.")
    return text


def clean_query(query: Any) -> dict[str, str]:
    """The page's view keys only, each a short plain string (the URL state); anything else refused. Pure."""
    if not isinstance(query, dict):
        raise ViewError("bad_query", "A view is an object of the page's view keys.")
    out: dict[str, str] = {}
    for k, v in query.items():
        if k not in VIEW_KEYS:
            raise ViewError("bad_query", f"Unknown view key: {str(k)[:40]}")
        if v is None or v == "":
            continue
        s = str(v)
        if len(s) > VALUE_MAX or not _SAFE.match(s):
            raise ViewError("bad_query", f"The value of {k} is not a view value.")
        out[k] = s
    return out


def _row(r: Any) -> dict:
    q = r["query"]
    if isinstance(q, str):
        q = json.loads(q)
    return {"id": r["id"], "name": r["name"], "query": q, "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat()}


async def list_views(pool: Any, staff_id: str) -> dict:
    try:
        rows = await pool.fetch(LIST_SQL, staff_id)
    except asyncpg.exceptions.UndefinedTableError:
        return {"ready": False, "views": [], "reason": NOT_MIGRATED}
    return {"ready": True, "views": [_row(r) for r in rows]}


async def save_view(pool: Any, staff_id: str, name: Any, query: Any) -> dict:
    n, q = clean_name(name), clean_query(query)
    return _row(await pool.fetchrow(UPSERT_SQL, staff_id, n, json.dumps(q)))


async def delete_view(pool: Any, staff_id: str, view_id: str) -> Optional[str]:
    return await pool.fetchval(DELETE_SQL, view_id, staff_id)
