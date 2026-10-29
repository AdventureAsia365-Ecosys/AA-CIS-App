"""The tenant.membership.v1 event feed (AA-978): ``GET /tenant/membership/v1/events?after=&limit=``.

Rows come from ``shared.tenant_membership_event`` (CIS 276), which only the triggers on ``shared.tenants`` and
``shared.tenant_members`` write. The order is ``(txid, seq)`` and a row is served only when its writing transaction
is older than every transaction still open (``txid < pg_snapshot_xmin(pg_current_snapshot())``, evaluated in the
same statement as the read). Every transaction that can still commit has a txid at or above that horizon, so a late
commit always sorts after a cursor already handed out: no event is skipped (contracts rule 4). Ordering by ``seq``
alone would skip one (the CIS 276 notes). The cost: a long open transaction anywhere on the instance holds the feed
back until it ends.

Cursor: opaque to the consumer, ``base64url("v1:<txid>:<seq>")`` without padding. ``next_cursor`` is always a string
(the contract requires it): the cursor of the last item, else the cursor the caller sent, else the start.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

from services.tenant_membership.reader import InvalidRequest, iso_utc

DEFAULT_LIMIT = 200
MAX_LIMIT = 500
MAX_CURSOR = 200
_PREFIX = "v1"
_MAX_TXID = 2 ** 64 - 1
_MAX_SEQ = 2 ** 63 - 1

# The xid8 goes out as text (asyncpg has no xid8 codec) under its own name, ``txid_text``, and every column the
# WHERE and ORDER BY use is qualified (``e.``). An unqualified ``ORDER BY txid`` next to ``txid::text AS txid`` sorts
# by the TEXT output column (Postgres resolves a bare ORDER BY name to an output column first), so '10' < '9': a page
# then ends on a cursor that is not its numeric maximum, the next page serves rows again and ``limit=1`` skips rows
# (the flaky test_feed_cursor_pages_one_by_one of S256). Guarded by the unit test on this statement.
FEED_SQL = """
    SELECT e.seq, e.txid::text AS txid_text, e.event_id, e.type, e.tenant_id, e.membership, e.occurred_at
      FROM shared.tenant_membership_event e
     WHERE e.txid < pg_snapshot_xmin(pg_current_snapshot())
       AND (e.txid, e.seq) > ($1::text::xid8, $2::bigint)
     ORDER BY e.txid, e.seq
     LIMIT $3
"""

START = (0, 0)


def encode_cursor(txid: int, seq: int) -> str:
    raw = f"{_PREFIX}:{txid}:{seq}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: Optional[str]) -> tuple[int, int]:
    """(txid, seq) of a cursor this API issued; :data:`START` for none."""
    if value is None or value == "":
        return START
    if len(value) > MAX_CURSOR or not value.isascii():
        raise InvalidRequest("after is not a cursor this API issued")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
    except ValueError as exc:  # binascii.Error and UnicodeDecodeError are ValueErrors
        raise InvalidRequest("after is not a cursor this API issued") from exc
    parts = raw.split(":")
    if len(parts) != 3 or parts[0] != _PREFIX or not all(p.isdigit() for p in parts[1:]):
        raise InvalidRequest("after is not a cursor this API issued")
    txid, seq = int(parts[1]), int(parts[2])
    if txid > _MAX_TXID or seq > _MAX_SEQ:
        raise InvalidRequest("after is not a cursor this API issued")
    return txid, seq


def parse_limit(value: Optional[str]) -> int:
    if value is None or value == "":
        return DEFAULT_LIMIT
    if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= MAX_LIMIT:
        raise InvalidRequest(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return int(value)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def event_body(row: Any) -> dict:
    return {
        "event_id": str(row["event_id"]),
        "cursor": encode_cursor(int(row["txid_text"]), int(row["seq"])),
        "type": row["type"],
        "occurred_at": iso_utc(row["occurred_at"]),
        "tenant_id": str(row["tenant_id"]),
        "membership": _json(row["membership"]),
    }


async def list_events(conn: Any, after: Optional[str], limit: int) -> dict:
    """One MembershipEventPage, oldest first."""
    txid, seq = decode_cursor(after)
    rows = await conn.fetch(FEED_SQL, str(txid), seq, limit)
    items = [event_body(r) for r in rows]
    if items:
        next_cursor = items[-1]["cursor"]
    else:
        next_cursor = after if after else encode_cursor(*START)
    return {"items": items, "next_cursor": next_cursor}
