"""AA-1142 (S292, P2) — the incident record of the Ecosystem status page (S285 review idea 10, PagerDuty / incident.io
style; the minimal ``ops_incident`` the S285 AI-assistant advisory names).

- **Declare** from a Needs-attention item (or by hand): title, SEV (sev1-sev3, root docs/ops/incident-response.md), the
  app, the item id, an optional summary; the person who declares owns it until someone takes it over. At most one
  unresolved incident per item (a partial unique index, CIS 484): declaring the same alert twice answers 409 with the
  open incident.
- **Update**: status (investigating, identified, monitoring, resolved; ``resolved_at`` set when resolved, cleared when
  reopened), SEV, owner (the caller takes it over), a note. Every change is an ``acp_shared.ops_incident_update`` row
  (its own trail) and an ``acp_shared.audit_log`` row (``ops_incident_declared`` / ``ops_incident_updated``).
- **Timeline** (:func:`timeline`): the incident's own trail merged with the ``acp_shared.ops_event`` rows (deploys,
  failed rollouts, alarms, service drops) of its app (every app for an incident of ``shared`` or without an app) and
  the acks of its item, from ``cis.ops.status_incident_lookback_min`` before it was declared until it was resolved
  (or now), newest last.
- **Write lesson**: the page links the lesson log of the root repo and copies a draft line
  (``lesson_draft``) in the lesson log's format.

Who: reads ops.read, writes ops.write and never the business owner (the router). Pure helpers unit-tested; the reads
and writes are small (indexes on the declared time, the open item and the incident id).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from shared import ops_status_thresholds as th

SEVERITIES = ("sev1", "sev2", "sev3")
STATUSES = ("investigating", "identified", "monitoring", "resolved")
APPS = ("cis", "tripplanner", "booking", "crm", "shared")
AUDIT_DECLARED, AUDIT_UPDATED = "ops_incident_declared", "ops_incident_updated"
RESOURCE_TYPE = "ops_incident"
LIST_ROWS = 50  # technical-constant: rows of a read (the incidents list on the page)
TIMELINE_ROWS = 300  # technical-constant: rows of a read (one incident's timeline)
NOT_MIGRATED = "the incident tables need database migration 484"
LESSONS_URL = "https://github.com/AdventureAsia365-Ecosys/AA-Ecosys/blob/master/skill/aa-lessons/lessons.md"

COLUMNS = ("incident_id, title, severity, status, app, item_id, owner, summary, declared_by, declared_at, updated_at, "
           "resolved_at")
INSERT_SQL = f"""
    INSERT INTO acp_shared.ops_incident (title, severity, status, app, item_id, owner, summary, declared_by,
                                         declared_at, updated_at)
    VALUES ($1, $2, 'investigating', $3, $4, $5, $6, $5, $7, $7)
    RETURNING {COLUMNS}
"""
UPDATE_ROW_SQL = """
    INSERT INTO acp_shared.ops_incident_update (incident_id, at, actor, kind, from_value, to_value, note)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
GET_SQL = f"SELECT {COLUMNS} FROM acp_shared.ops_incident WHERE incident_id = $1"
LOCK_SQL = f"SELECT {COLUMNS} FROM acp_shared.ops_incident WHERE incident_id = $1 FOR UPDATE"
OPEN_FOR_ITEM_SQL = ("SELECT incident_id FROM acp_shared.ops_incident WHERE item_id = $1 AND status <> 'resolved' "
                     "LIMIT 1")
SAVE_SQL = f"""
    UPDATE acp_shared.ops_incident SET status = $2, severity = $3, owner = $4, updated_at = $5, resolved_at = $6
     WHERE incident_id = $1
    RETURNING {COLUMNS}
"""
LIST_SQL = f"""
    SELECT {COLUMNS} FROM acp_shared.ops_incident
     WHERE ($1::boolean IS FALSE OR status <> 'resolved')
     ORDER BY (status = 'resolved'), declared_at DESC
     LIMIT {LIST_ROWS}
"""
UPDATES_SQL = """
    SELECT update_id, at, actor, kind, from_value, to_value, note FROM acp_shared.ops_incident_update
     WHERE incident_id = $1 ORDER BY at, update_id
"""
EVENTS_SQL = f"""
    SELECT event_id, occurred_at, ended_at, kind, severity, app, subject, title
      FROM acp_shared.ops_event
     WHERE occurred_at >= $1 AND occurred_at <= $2 AND ($3::text[] IS NULL OR app = ANY($3::text[]))
     ORDER BY occurred_at
     LIMIT {TIMELINE_ROWS}
"""
ACKS_SQL = f"""
    SELECT ack_id, action, until, reason, actor, created_at, cleared_at, cleared_by
      FROM acp_shared.ops_alert_ack
     WHERE item_id = $1 AND created_at >= $2 AND created_at <= $3
     ORDER BY created_at
     LIMIT {TIMELINE_ROWS}
"""


class IncidentError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 422, incident_id: Optional[int] = None) -> None:
        super().__init__(message)
        self.code, self.status, self.incident_id = code, status, incident_id


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)


def clean_text(v: Optional[str], lo: int, hi: int, field: str) -> Optional[str]:
    """Trimmed text within [lo, hi] characters (None for empty when lo is 0); IncidentError otherwise. Pure."""
    text = (v or "").strip()
    if not text and lo == 0:
        return None
    if not lo <= len(text) <= hi:
        raise IncidentError(f"bad_{field}", f"The {field} must be {lo}-{hi} characters")
    return text


def validate_declare(title: str, severity: str, app: Optional[str], summary: Optional[str]) -> dict:
    """The checked fields of a declaration. Pure."""
    if severity not in SEVERITIES:
        raise IncidentError("bad_severity", "SEV must be sev1, sev2 or sev3")
    if app is not None and app not in APPS:
        raise IncidentError("bad_app", "Unknown app")
    return {"title": clean_text(title, 3, 300, "title"), "severity": severity, "app": app,
            "summary": clean_text(summary, 0, 2000, "summary")}


def changes(row: dict, *, status: Optional[str], severity: Optional[str], take_over: bool, actor: str,
            now: datetime) -> tuple[dict, list[tuple[str, Optional[str], Optional[str]]]]:
    """The new (status, severity, owner, resolved_at) and the list of (kind, from, to) changes. Pure."""
    if status is not None and status not in STATUSES:
        raise IncidentError("bad_status", "Unknown status")
    if severity is not None and severity not in SEVERITIES:
        raise IncidentError("bad_severity", "SEV must be sev1, sev2 or sev3")
    new = {"status": status or row["status"], "severity": severity or row["severity"],
           "owner": actor if take_over else row["owner"], "resolved_at": row.get("resolved_at")}
    out = []
    if new["status"] != row["status"]:
        out.append(("status", row["status"], new["status"]))
        new["resolved_at"] = now if new["status"] == "resolved" else None
    if new["severity"] != row["severity"]:
        out.append(("severity", row["severity"], new["severity"]))
    if new["owner"] != row["owner"]:
        out.append(("owner", row["owner"], new["owner"]))
    return new, out


def _name(actor: Optional[str], names: Optional[dict]) -> Optional[str]:
    from api.services.ai_gateway import actor_view

    return actor_view(actor, names).get("updated_by_name") if actor else None


def view(row: dict, names: Optional[dict] = None) -> dict:
    """The API shape of one incident row (names of the actors when known). Pure."""
    names = names or {}
    return {"incident_id": row["incident_id"], "title": row["title"], "severity": row["severity"],
            "status": row["status"], "app": row.get("app"), "item_id": row.get("item_id"),
            "owner": row["owner"], "owner_name": _name(row["owner"], names), "summary": row.get("summary"),
            "declared_by": row["declared_by"], "declared_by_name": _name(row["declared_by"], names),
            "declared_at": _iso(row["declared_at"]), "updated_at": _iso(row["updated_at"]),
            "resolved_at": _iso(row.get("resolved_at"))}


def lesson_draft(inc: dict) -> str:
    """A draft line in the lesson log's format (skill/aa-lessons/lessons.md: principle — symptom → cause (where). Catch
    with: ...), for the person to finish. Pure."""
    app = inc.get("app") or "shared"
    return (f"<principle> — {inc['title']} ({inc['severity'].upper()}, {app}) → <cause> "
            f"(incident {inc['incident_id']}, {str(inc['declared_at'])[:10]}). Catch with: <guard>.")


def timeline(inc: dict, updates: list[dict], events: list[dict], acks: list[dict],
             names: Optional[dict] = None) -> list[dict]:
    """One list, oldest first: the incident's own updates, the ops events of its app and the acks of its item. Pure."""
    names = names or {}
    rows = [{"t": _iso(u["at"]), "kind": "update", "type": u["kind"], "from": u.get("from_value"),
             "to": u.get("to_value"), "note": u.get("note"), "by": u["actor"], "by_name": _name(u["actor"], names)}
            for u in updates]
    rows += [{"t": _iso(e["occurred_at"]), "t2": _iso(e.get("ended_at")), "kind": "event", "type": e["kind"],
              "severity": e["severity"], "app": e.get("app"), "text": e["title"], "id": f"ev-{e['event_id']}"}
             for e in events]
    rows += [{"t": _iso(a["created_at"]), "kind": "ack", "type": a["action"], "until": _iso(a.get("until")),
              "note": a.get("reason"), "by": a["actor"], "by_name": _name(a["actor"], names),
              "cleared_at": _iso(a.get("cleared_at"))} for a in acks]
    rows.sort(key=lambda r: r["t"] or "")
    return rows


def window(inc: dict, now: datetime, lookback_min: int) -> tuple[datetime, datetime]:
    start = _aware(inc["declared_at"]) - timedelta(minutes=lookback_min)
    end = _aware(inc["resolved_at"]) if inc.get("resolved_at") else now
    return start, end


def event_apps(app: Optional[str]) -> Optional[list[str]]:
    """The ops_event apps of an incident's timeline: its app and ``shared``; every app (None) for a shared one. Pure."""
    return None if app in (None, "shared") else [app, "shared"]


async def _names(conn: Any, actors: list[str]) -> dict:
    from api.services.ai_gateway import actor_names

    try:
        return await actor_names(conn, [a for a in actors if a])
    except asyncpg.PostgresError:   # names are a convenience: the actor id still shows
        return {}


async def _audit(conn: Any, actor: str, action: str, incident_id: int, details: dict) -> None:
    from services.acp_shared.audit_log import write_audit_log

    await write_audit_log(conn, tenant_id=None, actor=actor[:64], action=action, resource_type=RESOURCE_TYPE,
                          resource_id=str(incident_id), details=details)


async def declare(pool: Any, *, title: str, severity: str, app: Optional[str], item_id: Optional[str],
                  summary: Optional[str], actor: str, now: Optional[datetime] = None) -> dict:
    """Insert the incident, its ``declared`` update and the audit row in one transaction. IncidentError 409
    ``already_open`` when the item already has an unresolved incident."""
    now = _aware(now or datetime.now(timezone.utc))
    f = validate_declare(title, severity, app, summary)
    async with pool.acquire() as conn:
        if item_id:
            existing = await conn.fetchval(OPEN_FOR_ITEM_SQL, item_id)
            if existing:
                raise IncidentError("already_open", "This alert already has an open incident", status=409,
                                    incident_id=existing)
        try:
            async with conn.transaction():
                row = dict(await conn.fetchrow(INSERT_SQL, f["title"], f["severity"], f["app"], item_id, actor[:64],
                                               f["summary"], now))
                await conn.execute(UPDATE_ROW_SQL, row["incident_id"], now, actor[:64], "declared", None,
                                   f["severity"], f["summary"])
                await _audit(conn, actor, AUDIT_DECLARED, row["incident_id"],
                             {"severity": f["severity"], "app": f["app"], "item_id": item_id, "title": f["title"]})
        except asyncpg.exceptions.UniqueViolationError as e:   # a declaration of the same item won the race
            existing = await conn.fetchval(OPEN_FOR_ITEM_SQL, item_id)
            raise IncidentError("already_open", "This alert already has an open incident", status=409,
                                incident_id=existing) from e
        names = await _names(conn, [row["owner"]])
    return view(row, names)


async def update(pool: Any, incident_id: int, *, status: Optional[str], severity: Optional[str], take_over: bool,
                 note: Optional[str], actor: str, now: Optional[datetime] = None) -> dict:
    """Apply the changes and the note in one transaction (one update row each, one audit row). IncidentError 404 when
    there is no such incident, 422 when nothing changes."""
    now = _aware(now or datetime.now(timezone.utc))
    text = clean_text(note, 0, 2000, "note")
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                cur = await conn.fetchrow(LOCK_SQL, incident_id)
                if cur is None:
                    raise IncidentError("no_such_incident", "No such incident", status=404)
                cur = dict(cur)
                new, diff = changes(cur, status=status, severity=severity, take_over=take_over, actor=actor[:64],
                                    now=now)
                if not diff and not text:
                    raise IncidentError("nothing_to_change", "Change the status, SEV or owner, or add a note")
                row = dict(await conn.fetchrow(SAVE_SQL, incident_id, new["status"], new["severity"], new["owner"],
                                               now, new["resolved_at"]))
                for kind, old, to in diff:
                    await conn.execute(UPDATE_ROW_SQL, incident_id, now, actor[:64], kind, old, to, None)
                if text:
                    await conn.execute(UPDATE_ROW_SQL, incident_id, now, actor[:64], "note", None, None, text)
                await _audit(conn, actor, AUDIT_UPDATED, incident_id,
                             {"changes": [{"kind": k, "from": o, "to": t} for k, o, t in diff], "note": text})
        except asyncpg.exceptions.UniqueViolationError as e:   # reopened while the item has another open incident
            raise IncidentError("already_open", "This alert already has another open incident", status=409) from e
        names = await _names(conn, [row["owner"], row["declared_by"]])
    return view(row, names)


async def list_incidents(pool: Any, *, open_only: bool = False) -> dict:
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(LIST_SQL, open_only)]
        names = await _names(conn, [a for r in rows for a in (r["owner"], r["declared_by"])])
    return {"incidents": [view(r, names) for r in rows], "open": sum(1 for r in rows if r["status"] != "resolved"),
            "lessons_url": LESSONS_URL}


async def detail(pool: Any, incident_id: int, now: Optional[datetime] = None) -> Optional[dict]:
    """The incident, its timeline (window and rows) and the lesson draft; None when there is no such incident."""
    now = _aware(now or datetime.now(timezone.utc))
    async with pool.acquire() as conn:
        row = await conn.fetchrow(GET_SQL, incident_id)
        if row is None:
            return None
        inc = dict(row)
        start, end = window(inc, now, th.incident_lookback_min())
        updates = [dict(r) for r in await conn.fetch(UPDATES_SQL, incident_id)]
        events = [dict(r) for r in await conn.fetch(EVENTS_SQL, start, end, event_apps(inc.get("app")))]
        acks = ([dict(r) for r in await conn.fetch(ACKS_SQL, inc["item_id"], start, end)]
                if inc.get("item_id") else [])
        names = await _names(conn, [inc["owner"], inc["declared_by"]] + [u["actor"] for u in updates]
                             + [a["actor"] for a in acks])
    out = view(inc, names)
    return {**out, "window": {"start": start.isoformat(), "end": end.isoformat(),
                              "lookback_min": th.incident_lookback_min(),
                              "lookback_key": th.INCIDENT_LOOKBACK_MIN},
            "timeline": timeline(inc, updates, events, acks, names), "lesson_draft": lesson_draft(out),
            "lessons_url": LESSONS_URL}
