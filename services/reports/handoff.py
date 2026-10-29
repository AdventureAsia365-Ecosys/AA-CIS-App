"""AA-1003 R2 (TP-03) — inquiry handoff reconciliation: did every TripPlanner inquiry reach the Booking inbox?

Reads only the two contract views cis_app may read (contract ops.overview.v1, root docs/architecture/
db-schema-reference.md): tripplanner.v_ops_traffic_daily (metric inquiry_sent: TripPlanner outbox rows of event
inquiry.submitted) and booking.v_ops_funnel_daily (inquiry_received / inquiry_parked / inquiry_failed per inbox
source; trip_case_created per case source_system). Both count per UTC day (their own definition), so every number
here is per UTC day too. No PII: aggregates only.

What the views cannot say (they have no inquiry key): which inquiry is missing, its age, and "received but no trip
case after 24 h". That needs a key-level contract view in each owner's schema (a tp-NNN and a bk-NNN migration that
grant cis_app SELECT on idempotency keys and timestamps only); until then the gap is a count. The request is recorded
in docs/implementation-notes/AA-1003.md ("R2 follow-up requests"); CIS never reads tripplanner.v_inquiry_outbox or
booking.inbox_events with another login.

The views belong to TripPlanner and Booking: a revoked grant or a view the owner dropped or reshaped surfaces as one
of CONTRACT_ERRORS. The KPI computer turns it into kpis.Unavailable (the daily chain goes on); the hourly check and
the TP-03 run fail, so the failure shows in the OPS-12 jobs and reports sections.

Three users:
- the hourly check (job kind inquiry_handoff_check, services/jobs/report_ops_jobs.py): the gap over the last
  WINDOW_DAYS UTC days, today included; a gap that is still there at the next hourly check (an hour later, well past
  the 15-minute delivery allowance) raises one admin bell and one ops alert e-mail (shared/ops_alerts.py). Worker
  variable INQUIRY_HANDOFF_ALERTS (default on; off / false / 0 / no) turns the bell and the e-mail off without a code
  change: the check still runs and records the gap, the result says the alert was switched off;
- the KPI snapshot (services/reports/kpis.py, `inquiry_handoff`): K-TP-HANDOFFGAP, K-BK-PARKED, K-BK-INQ2CASE of a
  closed UTC day;
- the TP-03 daily report (report_generate) and the OPS-12 digest line.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

import asyncpg

TP = "tripplanner"
WINDOW_DAYS = 7
CHECK_KIND = "inquiry_handoff_check"
GAP_EVENT = "platform.inquiry_handoff.gap"
FUNNEL_METRICS = ("inquiry_received", "inquiry_parked", "inquiry_failed", "trip_case_created")
SOURCES = ("tripplanner.v_ops_traffic_daily", "booking.v_ops_funnel_daily")
# A contract view CIS may no longer read as written: grant revoked, view dropped, a column renamed by its owner.
CONTRACT_ERRORS: tuple[type[BaseException], ...] = (asyncpg.InsufficientPrivilegeError, asyncpg.UndefinedTableError,
                                                    asyncpg.UndefinedColumnError)

SENT_SQL = """
    SELECT day, n FROM tripplanner.v_ops_traffic_daily
     WHERE metric = 'inquiry_sent' AND day >= $1 AND day <= $2
"""

FUNNEL_SQL = """
    SELECT day, metric, source_system, n FROM booking.v_ops_funnel_daily
     WHERE metric = ANY($3::text[]) AND day >= $1 AND day <= $2
"""

# The previous hourly check (its idempotency key names the UTC hour).
PREVIOUS_CHECK_SQL = "SELECT result FROM shared.job WHERE idempotency_key = $1 AND status = 'succeeded'"

# Hourly checks of a period (job_kind_created_idx), newest first.
CHECKS_SQL = """
    SELECT created_at, status, result FROM shared.job
     WHERE kind = $1 AND created_at >= $2 AND created_at < $3
     ORDER BY created_at DESC
     LIMIT $4
"""

# A real gap bell in the last 24 h blocks a new one unless the gap grew (is_read is one flag for all staff, so the
# throttle reads created_at, as the Jev low-credit bell).
LAST_GAP_BELL_SQL = """
    SELECT (payload ->> 'gap')::int AS gap FROM shared.notifications
     WHERE event_type = $1 AND created_at >= now() - interval '24 hours'
     ORDER BY created_at DESC LIMIT 1
"""
GAP_BELL_SQL = """
    INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, 'report', 'TP-03', $3::jsonb, $4::text[])
"""
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"   # the platform sentinel tenant of the other ops bells
GAP_BELL_ROLES = ["admin"]
_GAP_BELL_LOCK_KEY = 1_003_020_003


ALERTS_ENV = "INQUIRY_HANDOFF_ALERTS"
_OFF = ("0", "false", "off", "no")


def alerts_enabled(environ: Optional[dict] = None) -> bool:
    """The bell + e-mail of a persistent gap; on unless INQUIRY_HANDOFF_ALERTS says off."""
    env = os.environ if environ is None else environ
    return (env.get(ALERTS_ENV) or "on").strip().lower() not in _OFF


@dataclass(frozen=True)
class DayCounts:
    day: date
    sent: int = 0           # TripPlanner inquiry_sent
    received: int = 0       # Booking inquiry_received, source tripplanner
    parked: int = 0         # Booking inquiry_parked, every source
    failed: int = 0         # Booking inquiry_failed, every source
    cases: int = 0          # Booking trip_case_created, source_system tripplanner

    @property
    def gap(self) -> int:
        return max(0, self.sent - self.received)

    def as_dict(self) -> dict:
        return {**asdict(self), "day": self.day.isoformat(), "gap": self.gap}


def utc_today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def build_days(first: date, last: date, sent_rows: Iterable, funnel_rows: Iterable) -> list[DayCounts]:
    """Every UTC day of [first, last] (zeros where a view has no row), oldest first. Pure."""
    acc: dict[date, dict] = {}
    for r in sent_rows:            # summed: the contract allows several rows per day and metric (as ops overview)
        bucket = acc.setdefault(r["day"], {})
        bucket["sent"] = bucket.get("sent", 0) + int(r["n"] or 0)
    for r in funnel_rows:
        d, metric, src, n = r["day"], r["metric"], r["source_system"], int(r["n"] or 0)
        bucket = acc.setdefault(d, {})
        if metric == "inquiry_received" and src == TP:
            bucket["received"] = bucket.get("received", 0) + n
        elif metric == "inquiry_parked":
            bucket["parked"] = bucket.get("parked", 0) + n
        elif metric == "inquiry_failed":
            bucket["failed"] = bucket.get("failed", 0) + n
        elif metric == "trip_case_created" and src == TP:
            bucket["cases"] = bucket.get("cases", 0) + n
    out, d = [], first
    while d <= last:
        out.append(DayCounts(day=d, **acc.get(d, {})))
        d += timedelta(days=1)
    return out


async def read_days(conn, first: date, last: date) -> list[DayCounts]:
    sent = await conn.fetch(SENT_SQL, first, last)
    funnel = await conn.fetch(FUNNEL_SQL, first, last, list(FUNNEL_METRICS))
    return build_days(first, last, sent, funnel)


def totals(days: list[DayCounts]) -> dict:
    sent = sum(d.sent for d in days)
    received = sum(d.received for d in days)
    return {"sent": sent, "received": received, "gap": max(0, sent - received),
            "parked": sum(d.parked for d in days), "failed": sum(d.failed for d in days),
            "cases": sum(d.cases for d in days)}


def inq_to_case(day: DayCounts) -> Optional[float]:
    return round(day.cases / day.received, 4) if day.received else None


# ── hourly check ────────────────────────────────────────────────────────────────────────────────────────────────


def hour_of(now: Optional[datetime] = None) -> datetime:
    t = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return t.replace(minute=0, second=0, microsecond=0)


def check_key(hour: datetime) -> str:
    return f"{CHECK_KIND}:{hour.astimezone(timezone.utc):%Y-%m-%dT%H}"


def evaluate(days: list[DayCounts], previous: Optional[dict]) -> dict:
    """The check's result from the window's days and the previous hour's result (None: no previous check). Pure.

    `persistent`: a gap now AND at the previous check, an hour apart — the alert condition (an inquiry still in
    flight at one check is delivered by the next one)."""
    t = totals(days)
    prev_gap = (previous or {}).get("gap")
    prev_gap = prev_gap if isinstance(prev_gap, int) else None
    return {
        "window": {"first": days[0].day.isoformat(), "last": days[-1].day.isoformat(), "days": len(days),
                   "basis": "UTC days (contract views)"},
        **t,
        "previous_gap": prev_gap,
        "persistent": t["gap"] > 0 and (prev_gap or 0) > 0,
        "mismatch_days": [d.as_dict() for d in days if d.sent != d.received],
        "days": [d.as_dict() for d in days],
    }


def gap_message(result: dict) -> str:
    w = result["window"]
    return (f"TripPlanner sent {result['sent']} inquiries and the Booking inbox received {result['received']} from "
            f"TripPlanner between {w['first']} and {w['last']} (UTC days): {result['gap']} not delivered, still "
            f"missing an hour after the previous check. Open Platform › Reports › TP-03 and the Booking inbox.")


async def maybe_raise_gap_bell(conn, result: dict) -> bool:
    """One locked check-then-insert: a bell when none was raised in 24 h, or the gap grew since the last one."""
    payload = json.dumps({"message": gap_message(result), "gap": result["gap"], "sent": result["sent"],
                          "received": result["received"], "window": result["window"]})
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _GAP_BELL_LOCK_KEY)
        last = await conn.fetchrow(LAST_GAP_BELL_SQL, GAP_EVENT)
        if last is not None and (last["gap"] or 0) >= result["gap"]:
            return False
        await conn.execute(GAP_BELL_SQL, PLATFORM_TENANT_ID, GAP_EVENT, payload, GAP_BELL_ROLES)
    return True


def _json(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


async def previous_result(conn, hour: datetime) -> Optional[dict]:
    row = await conn.fetchrow(PREVIOUS_CHECK_SQL, check_key(hour - timedelta(hours=1)))
    return _json(row["result"]) if row else None


async def recent_checks(conn, start: datetime, end: datetime, limit: int = 48) -> list[dict]:
    rows = await conn.fetch(CHECKS_SQL, CHECK_KIND, start, end, limit)
    return [{"created_at": r["created_at"], "status": r["status"], "result": _json(r["result"])} for r in rows]


# ── TP-03 daily report ──────────────────────────────────────────────────────────────────────────────────────────

REPORT_KPIS = ("K-TP-HANDOFFGAP", "K-BK-PARKED", "K-BK-INQ2CASE")


def render_report(report, day: date, days: list[DayCounts], kpi_rows: list[dict], checks: list[dict],
                  generated_at: datetime):
    """TP-03 for the 7 UTC days ending `day`. Pure. Returns generate.Rendered."""
    from services.reports.generate import Rendered, fmt

    t = totals(days)
    today = days[-1]
    gap_days = [d for d in days if d.gap]
    checks_gap = [c for c in checks if (c["result"] or {}).get("gap")]
    persistent = [c for c in checks if (c["result"] or {}).get("persistent")]
    failed_checks = [c for c in checks if c["status"] != "succeeded"]
    status = "gap" if today.gap or persistent else ("parked" if today.parked or today.failed else "ok")
    headline = (f"{report.id} {day.isoformat()}: "
                + {"gap": f"{today.gap} inquiries not received" if today.gap else "a gap persisted at an hourly check",
                   "parked": f"{today.parked + today.failed} inquiries parked or failed",
                   "ok": f"all {today.sent} inquiries received"}[status])
    output = {"report_id": report.id, "name": report.name_en, "period": {"start": day.isoformat(),
                                                                         "end": day.isoformat()},
              "basis": "UTC days (contract views ops.overview.v1)", "generated_at": generated_at.isoformat(),
              "headline": headline, "status": status, "day": today.as_dict(), "window": t,
              "inquiry_to_case": handoff_ratio(today), "days": [d.as_dict() for d in days],
              "hourly_checks": {"count": len(checks), "with_gap": len(checks_gap), "persistent": len(persistent),
                                "failed": len(failed_checks)},
              "kpis": sorted({r["kpi_id"] for r in kpi_rows}), "sources": list(SOURCES),
              "not_measured": ["Parked and failed inquiries with age", "Received but no trip case after 24 h"]}
    md = [f"# {report.id} {report.name_en} — UTC day {day.isoformat()}", "",
          f"Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')} from the TripPlanner and Booking contract views "
          "(aggregates per UTC day; no inquiry content).", "",
          f"**{headline}.**", "",
          "## Inquiries sent vs received per day", "",
          "| UTC day | Sent (TripPlanner) | Received (Booking) | Gap | Parked | Failed | Trip cases "
          "| Inquiry to case |",
          "|---|---|---|---|---|---|---|---|"]
    md += [f"| {d.day.isoformat()} | {d.sent} | {d.received} | {d.gap} | {d.parked} | {d.failed} | {d.cases} | "
           f"{fmt(handoff_ratio(d), 'ratio')} |" for d in days]
    md += ["", f"Seven days: sent {t['sent']}, received {t['received']}, gap {t['gap']}.", "",
           "## Mismatches over the last 7 days", ""]
    md += [f"- {d.day.isoformat()}: sent {d.sent}, received {d.received}" for d in days if d.sent != d.received] \
        or ["- None."]
    md += ["", "## Hourly checks (last 24 h)", "",
           f"{len(checks)} checks, {len(checks_gap)} saw a gap, {len(persistent)} saw it persist (alert), "
           f"{len(failed_checks)} did not finish.", "",
           "## Not measured yet", "",
           "- Parked and failed inquiries with their age, and inquiries received with no trip case after 24 h: they "
           "need a key-level contract view of the Booking inbox (requested; the views here hold counts only).", ""]
    if gap_days:
        md += ["## What to do", "",
               "Check the TripPlanner outbox delivery and the Booking inbox (parked / failed rows) for the days "
               "above; a day boundary can shift one inquiry to the next day (sent 23:59 UTC, received after "
               "midnight), so a gap of 1 matched by a surplus the next day is not a loss.", ""]
    return Rendered(headline[:1000], output, "\n".join(md))


def handoff_ratio(d: DayCounts) -> Optional[float]:
    return inq_to_case(d)


async def render(conn, reg, report, first: date, last: date, now: datetime):
    from services.reports import store

    days = await read_days(conn, last - timedelta(days=WINDOW_DAYS - 1), last)
    rows = await store.kpi_series(conn, list(REPORT_KPIS), last - timedelta(days=WINDOW_DAYS - 1), last)
    checks = await recent_checks(conn, now - timedelta(hours=24), now)
    return render_report(report, last, days, rows, checks, now)
