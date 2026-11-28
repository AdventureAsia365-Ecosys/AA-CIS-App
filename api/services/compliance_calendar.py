"""AA-1184 (S297) — the Calendar tab of Platform › Compliance readiness: the ops calendar as the root collector
published it, read from the evidence (no table of its own).

The root repo keeps every dated and recurring ops duty in docs/ops/calendar.yaml. Its daily compliance collector
(AA-Ecosys tools/compliance/collect.py, tools/ops/ops_calendar.py) evaluates the calendar for the run's UTC day and
publishes the check ``ops_calendar_overdue`` in the sealed ``root_daily`` bundle: ``fail`` the day a line is past its
due date and not done, ``pass`` otherwise, ``error`` when the calendar does not validate. compliance.py ingests it like
every other check into compliance.evidence; this module shapes its newest row for the tab:

  detail = {"calendar_updated", "today", "lines", "overdue", "due_soon", "open", "counts": {state: n},
            "rows": ["id|kind|state|due|last|owner|title|check|extra|note", ...], "drill": bool,
            ["truncated": true], ["automated_omitted": true]}

A row is a "|"-separated string (to fit the 8 KiB detail limit; the calendar refuses "|" in its text): kind and owner
are one- or two-letter codes (KIND_CODES / OWNER_CODES, the same maps as the root tool), dates are ISO or empty, the
note is only sent for a line that needs attention. A line whose evidence is a compliance check (``check``) is graded by
that check: the answer adds the check's current state from the same evaluation as the Overview tab.

The line states are the collector's for its ``today``; when the evidence goes stale or the collector falls silent the
check itself turns stale / error (compliance_score.check_state) and the tab says so above the table.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional

from api.services import compliance

CHECK_ID = "ops_calendar_overdue"
ROW_FIELDS = ("id", "kind", "state", "due", "last", "owner", "title", "check", "extra", "note")
KIND_CODES = {"R": "recurring", "D": "deadline", "T": "trigger", "A": "automated"}
OWNER_CODES = {"TL": "tech_lead", "BO": "business_owner", "ED": "engineer_on_duty", "AG": "agent"}
LINE_STATES = ("overdue", "due_soon", "open", "ok", "done", "scheduled", "watched", "waiting", "automated")
ATTENTION = ("overdue", "due_soon", "open")
MAX_ROWS = 400   # technical-constant: refuse to shape an absurd row list (the 8 KiB detail holds about 60)


def parse_row(row: Any) -> Optional[dict]:
    """One published row as a dict of ROW_FIELDS (empty cells None); None for anything that is not a well-formed row
    (unknown kind, owner or state)."""
    if not isinstance(row, str) or not row:
        return None
    cells = row.split("|", len(ROW_FIELDS) - 1)
    cells += [""] * (len(ROW_FIELDS) - len(cells))
    out = {k: (v.strip() or None) for k, v in zip(ROW_FIELDS, cells)}
    if out["kind"] not in KIND_CODES or out["owner"] not in OWNER_CODES or out["state"] not in LINE_STATES:
        return None
    if not out["id"] or not out["title"]:
        return None
    out["kind"], out["owner"] = KIND_CODES[out["kind"]], OWNER_CODES[out["owner"]]
    for k in ("due", "last"):
        if out[k] is not None:
            try:
                out[k] = date.fromisoformat(out[k]).isoformat()
            except ValueError:
                out[k] = None
    return out


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def _order(line: dict) -> tuple:
    return (LINE_STATES.index(line["state"]), line["due"] or "9999-12-31", line["id"])


def shape(row: Optional[Mapping[str, Any]], check_state: Any, states: Mapping[str, Any],
          now: Optional[datetime] = None) -> dict:
    """The tab's answer from the newest evidence row of the check (``row``; None = never published), that check's
    evaluated state (a compliance_score.CheckState or None) and every check's state (``states``, for watched lines).
    Pure: no I/O."""
    now = now or datetime.now(timezone.utc)
    if row is None:
        return {"generated_at": now.isoformat(), "available": False, "check": None, "calendar": None, "lines": []}
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    lines = []
    for raw in (detail.get("rows") or [])[:MAX_ROWS]:
        line = parse_row(raw)
        if line is None:
            continue
        ref = line["check"]
        st = states.get(ref) if ref else None
        line["check_state"] = getattr(st, "state", None) if st is not None else ("missing" if ref else None)
        lines.append(line)
    lines.sort(key=_order)
    counts = {k: int(v) for k, v in (detail.get("counts") or {}).items() if k in LINE_STATES and isinstance(v, int)}
    check = {
        "check_id": CHECK_ID,
        "status": row.get("status"),
        "state": getattr(check_state, "state", None) if check_state is not None else row.get("status"),
        "reason": getattr(check_state, "reason", None) if check_state is not None else row.get("summary"),
        "summary": row.get("summary"),
        "source": row.get("source"),
        "collected_at": _iso(row.get("collected_at")),
        "expires_at": _iso(getattr(check_state, "expires_at", None)) if check_state is not None else None,
        "link": row.get("link"),
        "sealed": bool(row.get("object_key")),
    }
    calendar = {
        "updated": detail.get("calendar_updated"),
        "today": detail.get("today"),
        "lines": len(lines),
        "counts": counts,
        "overdue": sum(1 for ln in lines if ln["state"] == "overdue"),
        "attention": sum(1 for ln in lines if ln["state"] in ATTENTION),
        "drill": bool(detail.get("drill")),
        "truncated": bool(detail.get("truncated")),
        "automated_omitted": bool(detail.get("automated_omitted")),
        "errors": detail.get("errors") if isinstance(detail.get("errors"), int) else None,
    }
    return {"generated_at": now.isoformat(), "available": True, "check": check, "calendar": calendar, "lines": lines}


async def calendar(pool: Any, now: Optional[datetime] = None) -> dict:
    """The Calendar tab: the newest ``ops_calendar_overdue`` evidence (a sealed row first, the same read as the
    Overview tab) shaped by :func:`shape`."""
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        data, result, _ = await compliance._evaluate(conn, now)   # noqa: SLF001 - the shared read of the page
    return shape(data["latest"].get(CHECK_ID), result["checks"].get(CHECK_ID), result["checks"], now)
