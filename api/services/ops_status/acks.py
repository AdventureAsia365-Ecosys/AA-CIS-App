"""AA-1141 (P1 feature 2) — alert ack / snooze / mute on the Ecosystem status page: who handles a Needs-attention item,
why, and until when (S285 review idea 2, PagerDuty / Opsgenie style).

- **Ack** sets an item aside for ``cis.ops.status_ack_hours`` (then it counts as open again if it is still there);
  **Snooze** for 1 h, 4 h or until ``cis.ops.status_snooze_tomorrow_hour`` the next day on the viewer's clock (the page
  computes that time and sends it); **Mute** until an end time the person picks, at most
  ``cis.ops.status_mute_max_days`` ahead. A reason of at least ``cis.ops.status_ack_reason_min_chars`` characters is
  required for each. Everything ends by itself at ``until``; an item that resolves simply leaves the list.
- Rows live in ``acp_shared.ops_alert_ack`` (CIS 482); one row of an item is in force at a time (a new ack clears the
  previous one in the same transaction). Every write is also an ``acp_shared.audit_log`` row (``ops_alert_ack`` /
  ``ops_alert_ack_cleared``).
- Who: the route is ``ops.write`` (the role matrix: admin), and the API also refuses the business owner (tech lead
  AI10: the business owner reads, never acts on ops); ``can_ack`` tells the page whether to show the buttons.
- The verdict reads the rows in force (:func:`apply`): the item keeps its place in the list with ``status`` acked /
  snoozed / muted and an ``ack`` block ("Acked by <name> at <time>, until <time>: <reason>"), and leaves the open count.

``validate`` and ``apply`` are pure and unit-tested; the reads and writes are small (an index on the rows in force).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from shared import ops_status_thresholds as th

ACTIONS = ("ack", "snooze", "mute")
STATE_OF = {"ack": "acked", "snooze": "snoozed", "mute": "muted"}
SNOOZE_HOURS = (1, 4)   # the spec's two fixed snooze choices (S285 review); "until tomorrow" uses a setting
AUDIT_SET, AUDIT_CLEARED = "ops_alert_ack", "ops_alert_ack_cleared"
RESOURCE_TYPE = "ops_alert"
HISTORY_ROWS = 50  # technical-constant: rows of a read (the recent ack trail on the page)
NOT_MIGRATED = "the ack table needs database migration 482"

IN_FORCE_SQL = """
    SELECT ack_id, item_id, action, until, reason, actor, created_at
      FROM acp_shared.ops_alert_ack
     WHERE cleared_at IS NULL AND until > $1
     ORDER BY created_at DESC
"""
HISTORY_SQL = f"""
    SELECT ack_id, item_id, app, severity, title, action, until, reason, actor, created_at, cleared_at, cleared_by,
           cleared_reason
      FROM acp_shared.ops_alert_ack
     WHERE created_at >= $1
     ORDER BY created_at DESC
     LIMIT {HISTORY_ROWS}
"""
CLEAR_SQL = """
    UPDATE acp_shared.ops_alert_ack SET cleared_at = $2, cleared_by = $3, cleared_reason = $4
     WHERE item_id = $1 AND cleared_at IS NULL AND until > $2
    RETURNING ack_id
"""
INSERT_SQL = """
    INSERT INTO acp_shared.ops_alert_ack (item_id, app, severity, title, action, until, reason, actor, created_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    RETURNING ack_id
"""


class AckError(ValueError):
    """A refused ack: ``code`` for the page (422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.code, self.message = code, message

    def __str__(self) -> str:
        return self.message


def _aware(t: datetime) -> datetime:
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def validate(action: str, until: Optional[datetime], reason: str, now: datetime, rules: dict) -> datetime:
    """The end time of an ack / snooze / mute, or AckError. ``rules`` = ``th.acks()``. Pure.

    ack: ``until`` is ignored, the end is now + ack_hours. snooze: ``until`` within 1 h .. (mute_max_days) ahead (the
    page sends now + 1 h, now + 4 h or the next morning). mute: ``until`` after now and at most mute_max_days ahead."""
    if action not in ACTIONS:
        raise AckError("bad_action", "Pick ack, snooze or mute.")
    text = (reason or "").strip()
    need = int(rules["reason_min_chars"])
    if len(text) < need:
        raise AckError("reason_too_short", f"Write a reason of at least {need} characters.")
    if len(text) > 2000:
        raise AckError("reason_too_long", "Keep the reason under 2000 characters.")
    now = _aware(now)
    if action == "ack":
        return now + timedelta(hours=int(rules["ack_hours"]))
    if until is None:
        raise AckError("until_missing", "Pick when it ends.")
    end = _aware(until)
    if end <= now + timedelta(minutes=1):
        raise AckError("until_past", "The end time must be in the future.")
    if end > now + timedelta(days=int(rules["mute_max_days"])):
        raise AckError("until_too_far", f"The end time can be at most {int(rules['mute_max_days'])} days ahead.")
    return end


def in_force(rows: list[dict], now: datetime) -> dict[str, dict]:
    """{item_id: newest row in force} (not cleared, not ended). Pure."""
    now = _aware(now)
    out: dict[str, dict] = {}
    for r in rows:
        if r.get("cleared_at") or _aware(r["until"]) <= now:
            continue
        cur = out.get(r["item_id"])
        if cur is None or _aware(r["created_at"]) > _aware(cur["created_at"]):
            out[r["item_id"]] = r
    return out


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)


def _name_of(actor: Optional[str], names: Optional[dict]) -> Optional[str]:
    from api.services.ai_gateway import actor_view

    return actor_view(actor, names).get("updated_by_name")


def ack_view(r: dict, names: Optional[dict] = None) -> dict:
    """The ``ack`` block of an item: action, by (actor id and name), at, until, reason. Pure."""
    return {"action": r["action"], "state": STATE_OF[r["action"]], "by": r.get("actor"),
            "by_name": _name_of(r.get("actor"), names), "at": _iso(r.get("created_at")), "until": _iso(r.get("until")),
            "reason": r.get("reason")}


def apply(items: list[dict], acks: dict[str, dict], names: Optional[dict] = None) -> list[dict]:
    """The attention items with their ack in force: ``status`` acked / snoozed / muted and the ``ack`` block. An item
    the API already counts as acknowledged (a read ops check bell) keeps "acked". Pure."""
    out = []
    for i in items:
        a = acks.get(i["id"])
        if a is None:
            out.append(i)
            continue
        out.append({**i, "status": STATE_OF[a["action"]], "ack": ack_view(a, names)})
    return out


# ── reads and writes ─────────────────────────────────────────────────────────────────────────────────────────────

async def read_in_force(pool: Any, now: datetime) -> tuple[dict[str, dict], dict]:
    """The rows in force and the staff names of their actors. ({}, {}) before CIS 482 (the page then shows no ack)."""
    from api.services.ai_gateway import actor_names

    try:
        rows = [dict(r) for r in await pool.fetch(IN_FORCE_SQL, _aware(now))]
    except asyncpg.exceptions.UndefinedTableError:
        return {}, {}
    acks = in_force(rows, now)
    try:
        async with pool.acquire() as conn:
            names = await actor_names(conn, [a["actor"] for a in acks.values()])
    except asyncpg.PostgresError:   # the names are a convenience: the ack still shows ("a staff member")
        names = {}
    return acks, names


async def history(pool: Any, since: datetime) -> list[dict]:
    """The ack trail since ``since`` (newest first), with names. [] before CIS 482."""
    from api.services.ai_gateway import actor_names

    try:
        rows = [dict(r) for r in await pool.fetch(HISTORY_SQL, _aware(since))]
    except asyncpg.exceptions.UndefinedTableError:
        return []
    async with pool.acquire() as conn:
        try:
            names = await actor_names(conn, [r["actor"] for r in rows] + [r["cleared_by"] for r in rows])
        except asyncpg.PostgresError:
            names = {}
    return [{**ack_view(r, names), "id": r["ack_id"], "item_id": r["item_id"], "app": r.get("app"),
             "severity": r.get("severity"), "title": r.get("title"), "cleared_at": _iso(r.get("cleared_at")),
             "cleared_by_name": _name_of(r.get("cleared_by"), names) if r.get("cleared_by") else None,
             "cleared_reason": r.get("cleared_reason")} for r in rows]


async def set_ack(pool: Any, *, item: dict, action: str, until: Optional[datetime], reason: str, actor: str,
                  now: Optional[datetime] = None) -> dict:
    """Write one ack / snooze / mute of ``item`` (an attention item of the status page): validate, clear the one in
    force, insert, audit, in one transaction. AckError on a refused value; UndefinedTableError before CIS 482."""
    from services.acp_shared.audit_log import write_audit_log

    now = _aware(now or datetime.now(timezone.utc))
    end = validate(action, until, reason, now, th.acks())
    text = reason.strip()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cleared = await conn.fetch(CLEAR_SQL, item["id"], now, actor[:64], f"replaced by a new {action}")
            ack_id = await conn.fetchval(INSERT_SQL, item["id"], item.get("app"), item.get("severity")
                                         if item.get("severity") in ("sev1", "sev2", "sev3") else None,
                                         (item.get("title") or "")[:300] or None, action, end, text, actor[:64], now)
            await write_audit_log(conn, tenant_id=None, actor=actor[:64], action=AUDIT_SET,
                                  resource_type=RESOURCE_TYPE, resource_id=item["id"],
                                  details={"ack_id": ack_id, "action": action, "until": end.isoformat(),
                                           "reason": text, "severity": item.get("severity"), "app": item.get("app"),
                                           "replaced": [r["ack_id"] for r in cleared]})
    return {"ack_id": ack_id, "item_id": item["id"], "action": action, "state": STATE_OF[action],
            "until": end.isoformat(), "at": now.isoformat()}


async def clear(pool: Any, *, item_id: str, actor: str, reason: Optional[str] = None,
                now: Optional[datetime] = None) -> Optional[dict]:
    """End the ack in force of ``item_id`` now (the item counts as open again). None when nothing was in force."""
    from services.acp_shared.audit_log import write_audit_log

    now = _aware(now or datetime.now(timezone.utc))
    note = (reason or "").strip()[:2000] or None
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(CLEAR_SQL, item_id, now, actor[:64], note)
            if not rows:
                return None
            await write_audit_log(conn, tenant_id=None, actor=actor[:64], action=AUDIT_CLEARED,
                                  resource_type=RESOURCE_TYPE, resource_id=item_id,
                                  details={"ack_ids": [r["ack_id"] for r in rows], "reason": note})
    return {"item_id": item_id, "cleared_at": now.isoformat(), "cleared": len(rows)}
