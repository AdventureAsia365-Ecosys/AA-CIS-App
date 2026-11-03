"""The tenant.entitlements.v1 event feed (AA-1028 R1): ``GET /tenant/v1/account-events?after=&limit=``.

Rows come from ``shared.tenant_account_event`` (CIS 353), which only ``shared.tenant_account_emit()`` writes (the
triggers on ``shared.tenants``, ``shared.tenant_subscription`` and the entitlement sync). The read follows the
tenant.membership.v1 feed exactly (``services/tenant_membership/feed.py``): order ``(txid, seq)``, a row is served
only when its writing transaction is older than every transaction still open, so a late commit is never skipped
(contracts rule 4). The cursor format and limits are that feed's (``v1:<txid>:<seq>``, base64url); a cursor of one
feed is meaningless on the other but well formed, which is harmless: it only moves the start.

The ``change`` column is stored in the contract shape (CIS 353 / 354 build it with ``jsonb_build_object``); the
writer's ``actor`` is never selected.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from services.tenant_account.reader import iso_utc
from services.tenant_membership.feed import START, decode_cursor, encode_cursor, parse_limit  # noqa: F401

# Every column the WHERE and ORDER BY use is qualified, and the xid8 goes out as ``txid_text``: an unqualified
# ``ORDER BY txid`` next to ``txid::text AS txid`` would sort by the text (the membership feed's S256 lesson).
FEED_SQL = """
    SELECT e.seq, e.txid::text AS txid_text, e.event_id, e.type, e.tenant_id, e.change, e.occurred_at
      FROM shared.tenant_account_event e
     WHERE e.txid < pg_snapshot_xmin(pg_current_snapshot())
       AND (e.txid, e.seq) > ($1::text::xid8, $2::bigint)
     ORDER BY e.txid, e.seq
     LIMIT $3
"""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def event_body(row: Any) -> dict:
    return {
        "event_id": str(row["event_id"]),
        "cursor": encode_cursor(int(row["txid_text"]), int(row["seq"])),
        "type": row["type"],
        "occurred_at": iso_utc(row["occurred_at"]),
        "tenant_id": str(row["tenant_id"]),
        "change": _json(row["change"]),
    }


async def list_events(conn: Any, after: Optional[str], limit: int) -> dict:
    """One AccountEventPage, oldest first; ``next_cursor`` is always a string."""
    txid, seq = decode_cursor(after)
    rows = await conn.fetch(FEED_SQL, str(txid), seq, limit)
    items = [event_body(r) for r in rows]
    if items:
        next_cursor = items[-1]["cursor"]
    else:
        next_cursor = after if after else encode_cursor(*START)
    return {"items": items, "next_cursor": next_cursor}
