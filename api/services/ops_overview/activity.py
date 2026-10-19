"""AA-858 — TripPlanner traffic and the Booking funnel, read only through the producers' contract views.

CIS never reads ``booking.*`` / ``tripplanner.*`` tables (ADR 0008 §5; the X1 isolation probe expects 42501 for
``cis_app`` on them). The owners publish aggregate views (no id, no PII) in their own schema and grant SELECT to
``cis_app`` (contract ``ops.overview.v1``, AA-Ecosys docs/contracts/ops.overview.v1.md):

- ``tripplanner.v_ops_traffic_daily (day, metric, n)`` — tp-015: planner_session_started, trip_started,
  trip_edited, inquiry_sent.
- ``booking.v_ops_funnel_daily (day, metric, source_system, n)`` — bk-017: inquiry_received, inquiry_parked,
  inquiry_failed, trip_case_created, proposal_sent, confirmed, cancelled.
- ``booking.v_ops_case_status (status, n)`` — bk-017: not soft-deleted trip cases by current status.

Days are UTC. Unknown metric names are ignored (the contract only ever adds names). A missing view or grant makes
the card ``unavailable: contract_view_missing``; nothing is guessed.

AI searches (TripPlanner card) come from CIS's own ``shared.llm_call_log`` (stage ``tp_search_embed``). Only cache
misses are logged there, so it under-counts repeat searches; the response says so.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
import structlog

logger = structlog.get_logger()

TP_METRICS = ("planner_session_started", "trip_started", "trip_edited", "inquiry_sent")
TP_DAYS = 14
TP_SEARCH_STAGE = "tp_search_embed"

BK_FUNNEL = ("inquiry_received", "trip_case_created", "proposal_sent", "confirmed")
BK_EXTRA = ("cancelled",)
BK_ATTENTION = ("inquiry_parked", "inquiry_failed")
BK_WEEKS = 8
# booking.trip_cases status order (Booking trip_cases_status_chk); closed = completed / cancelled.
BK_STATUS_ORDER = ("new_inquiry", "discovery", "building", "proposal_sent", "confirmed", "supplier_confirming",
                   "invoice_sent", "payment_pending", "paid", "pre_departure", "on_trip", "completed", "cancelled")
BK_CLOSED = ("completed", "cancelled")

_CONTRACT_ERRORS = (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.InvalidSchemaNameError,
                    asyncpg.exceptions.InsufficientPrivilegeError, asyncpg.exceptions.UndefinedColumnError)

TP_VIEW_SQL = """
    SELECT day, metric, sum(n)::bigint AS n
      FROM tripplanner.v_ops_traffic_daily
     WHERE day >= $1 AND day <= $2
     GROUP BY day, metric
"""
TP_SEARCH_SQL = """
    SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*)::bigint AS n
      FROM shared.llm_call_log
     WHERE stage = $3
       AND created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
       AND created_at < (($2::date + 1)::timestamp AT TIME ZONE 'UTC')
     GROUP BY 1
"""
BK_WEEKLY_SQL = """
    SELECT date_trunc('week', day)::date AS week, metric, sum(n)::bigint AS n
      FROM booking.v_ops_funnel_daily
     WHERE day >= $1 AND day <= $2
     GROUP BY 1, 2
"""
BK_ATTENTION_SQL = """
    SELECT metric, sum(n)::bigint AS n
      FROM booking.v_ops_funnel_daily
     WHERE metric = ANY($1::text[])
     GROUP BY 1
"""
BK_STATUS_SQL = "SELECT status, n FROM booking.v_ops_case_status"


def _contract_missing(view: str, e: Exception) -> dict:
    logger.warning("ops_overview_contract_view_missing", view=view, error=repr(e)[:300])
    producer = {"tripplanner": "TripPlanner", "booking": "Booking"}.get(view.split(".")[0], view)
    return {"status": "unavailable", "reason": "contract_view_missing",
            "message": f"The data feed from {producer} is not set up on this environment yet.",
            "view": view}


def _today(now: Optional[datetime]) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def _pct_change(cur: int, prev: int) -> Optional[float]:
    if not prev:
        return None
    return round(100.0 * (cur - prev) / prev, 1)


async def tripplanner_activity(pool: Any, now: Optional[datetime] = None, days: int = TP_DAYS) -> dict:
    """Daily TripPlanner metrics for the last ``days`` UTC days (today included) + 7-day totals vs the 7 before."""
    today = _today(now)
    start = today - timedelta(days=days - 1)
    try:
        rows = await pool.fetch(TP_VIEW_SQL, start, today)
    except _CONTRACT_ERRORS as e:
        return _contract_missing("tripplanner.v_ops_traffic_daily", e)
    search_rows = await pool.fetch(TP_SEARCH_SQL, start, today, TP_SEARCH_STAGE)

    keys = TP_METRICS + ("ai_search",)
    series = {start + timedelta(days=i): {k: 0 for k in keys} for i in range(days)}
    for r in rows:
        if r["metric"] in TP_METRICS and r["day"] in series:
            series[r["day"]][r["metric"]] += int(r["n"])
    for r in search_rows:
        if r["day"] in series:
            series[r["day"]]["ai_search"] += int(r["n"])

    ordered = sorted(series.items())
    last7, prev7 = ordered[-7:], ordered[-14:-7]
    totals = {k: sum(v[k] for _, v in last7) for k in keys}
    prev = {k: sum(v[k] for _, v in prev7) for k in keys} if len(prev7) == 7 else None
    return {
        "status": "ok",
        "as_of": (now or datetime.now(timezone.utc)).isoformat(),
        "days": [{"day": d.isoformat(), **v} for d, v in ordered],
        "totals_7d": totals,
        "prev_7d": prev,
        "change_pct": {k: _pct_change(totals[k], prev[k]) for k in keys} if prev else None,
        "notes": {"ai_search": "Repeat searches answered from the cache are not counted."},
    }


async def booking_funnel(pool: Any, now: Optional[datetime] = None, weeks: int = BK_WEEKS) -> dict:
    """Booking funnel per ISO week (Monday, UTC) for the last ``weeks`` weeks, cases by status, inbox attention."""
    today = _today(now)
    this_monday = today - timedelta(days=today.weekday())
    first_monday = this_monday - timedelta(weeks=weeks - 1)
    try:
        weekly = await pool.fetch(BK_WEEKLY_SQL, first_monday, today)
        attention = await pool.fetch(BK_ATTENTION_SQL, list(BK_ATTENTION))
        statuses = await pool.fetch(BK_STATUS_SQL)
    except _CONTRACT_ERRORS as e:
        return _contract_missing("booking.v_ops_funnel_daily", e)

    keys = BK_FUNNEL + BK_EXTRA
    by_week = {first_monday + timedelta(weeks=i): {k: 0 for k in keys} for i in range(weeks)}
    for r in weekly:
        if r["metric"] in keys and r["week"] in by_week:
            by_week[r["week"]][r["metric"]] += int(r["n"])
    totals = {k: sum(w[k] for w in by_week.values()) for k in keys}

    by_status = {s: 0 for s in BK_STATUS_ORDER}
    for r in statuses:
        by_status[r["status"]] = by_status.get(r["status"], 0) + int(r["n"])
    open_total = sum(n for s, n in by_status.items() if s not in BK_CLOSED)
    att = {r["metric"]: int(r["n"]) for r in attention}

    def rate(a: str, b: str) -> Optional[float]:
        return round(100.0 * totals[b] / totals[a], 1) if totals[a] else None

    return {
        "status": "ok",
        "as_of": (now or datetime.now(timezone.utc)).isoformat(),
        "weeks": [{"week_start": w.isoformat(), **v} for w, v in sorted(by_week.items())],
        "totals": totals,
        "conversion_pct": {
            "inquiry_to_case": rate("inquiry_received", "trip_case_created"),
            "case_to_proposal": rate("trip_case_created", "proposal_sent"),
            "proposal_to_confirmed": rate("proposal_sent", "confirmed"),
        },
        "cases_by_status": [{"status": s, "n": n, "closed": s in BK_CLOSED} for s, n in by_status.items()],
        "open_cases": open_total,
        "inbox_attention": {"parked": att.get("inquiry_parked", 0), "failed": att.get("inquiry_failed", 0)},
    }
