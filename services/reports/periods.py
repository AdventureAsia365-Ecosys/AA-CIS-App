"""AA-1003 R1 — report periods. Every report day is an ICT (UTC+7, Vietnam: no daylight saving) calendar day.

A day's bounds are computed here and bound as timestamptz, so no SQL depends on the database time zone or its tz data.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

ICT = timezone(timedelta(hours=7), "ICT")
# Oldest day kpi_snapshot_daily and a manual run accept: shared.llm_call_log keeps 180 days of raw rows
# (retention_llm_call_log) and shared.job keeps failed rows 90 days, so a value older than this would undercount.
BACKFILL_DAYS = 90
# The daily chain starts once the ICT day before is closed and the ledgers have settled (00:30 UTC = 07:30 ICT,
# automation-plan.md §2).
SNAPSHOT_AFTER_UTC = time(0, 30)
SETTLE = timedelta(hours=7, minutes=30)      # end of an ICT day (17:00 UTC) + SETTLE = 00:30 UTC
# A daily report counts as overdue when its expected period has no succeeded run this long after the snapshot time.
OVERDUE_GRACE = timedelta(hours=3)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_ict(now: Optional[datetime] = None) -> date:
    return (now or now_utc()).astimezone(ICT).date()


def latest_closed_day(now: Optional[datetime] = None) -> date:
    """The most recent ICT day that has ended."""
    return today_ict(now) - timedelta(days=1)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of an ICT day as aware datetimes."""
    start = datetime.combine(day, time(0, 0), tzinfo=ICT)
    return start, start + timedelta(days=1)


def range_bounds(first: date, last: date) -> tuple[datetime, datetime]:
    """[start of `first`, end of `last`) in ICT."""
    return day_bounds(first)[0], day_bounds(last)[1]


def oldest_allowed_day(now: Optional[datetime] = None) -> date:
    return today_ict(now) - timedelta(days=BACKFILL_DAYS)


def parse_day(value) -> date:
    """A date or 'YYYY-MM-DD' -> date. ValueError otherwise (bound parameters need a real date, lessons S248)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) == 10:
        return date.fromisoformat(value)
    raise ValueError(f"{value!r} is not a day YYYY-MM-DD")


def snapshot_day(now: Optional[datetime] = None) -> date:
    """The ICT day the daily chain snapshots now: day D from 00:30 UTC of D + 1 (07:30 ICT), when D has been closed
    for 7.5 hours (SETTLE). Before that time of the UTC day it is still the day before D."""
    return latest_closed_day((now or now_utc()) - SETTLE)


# ── AA-1003 R3: weekly and monthly periods ─────────────────────────────────────────────────────────────────────
# Weekly reports cover an ISO week (Monday to Sunday); monthly reports a calendar month. The daily chain creates them:
# the snapshot of Sunday D (00:30 UTC of Monday) chains the weekly reports for D - 6 .. D, and the snapshot of the day
# before business day 2 (00:30 UTC of business day 2) chains the monthly reports for the month before.
# Business day N = the Nth Monday-Friday of the month (no holiday calendar until prod, automation-plan.md §2).
MONTHLY_BUSINESS_DAY = 2
_SCHEDULE_LOOKBACK_DAYS = 70        # far enough back to find the last monthly chain day from any day
EVENT_DEFAULT_DAYS = 7              # R4: a manual run of an event report covers the last 7 closed days


def iso_week(day: date) -> tuple[date, date]:
    """(Monday, Sunday) of the ISO week holding `day`."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    """(first day, last day) of the calendar month holding `day`."""
    first = day.replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def previous_month(day: date) -> tuple[date, date]:
    """(first, last) of the calendar month before the one holding `day`."""
    return month_bounds(day.replace(day=1) - timedelta(days=1))


def business_day(year: int, month: int, n: int = MONTHLY_BUSINESS_DAY) -> date:
    """The nth Monday-Friday of the month."""
    d, seen = date(year, month, 1), 0
    while True:
        if d.weekday() < 5:
            seen += 1
            if seen == n:
                return d
        d += timedelta(days=1)


def scheduled_period(schedule: str, day: date) -> Optional[tuple[date, date]]:
    """The period a scheduled chain at the snapshot of ICT day `day` creates for a report of this schedule, or None
    when that schedule has nothing due that day. daily/live: the day itself; weekly: the ISO week when `day` is its
    Sunday; monthly: the previous month when the next day is business day 2."""
    if schedule in ("daily", "live"):
        return day, day
    if schedule == "weekly":
        return (day - timedelta(days=6), day) if day.weekday() == 6 else None
    if schedule == "monthly":
        nxt = day + timedelta(days=1)
        return previous_month(nxt) if nxt == business_day(nxt.year, nxt.month) else None
    return None


def latest_scheduled(schedule: str, now: Optional[datetime] = None) -> Optional[tuple[tuple[date, date], date]]:
    """((first, last), chain day) of the newest period the scheduled chain should have created by now; None for a
    schedule the chain does not run (quarterly, event, ...)."""
    day = snapshot_day(now)
    for _ in range(_SCHEDULE_LOOKBACK_DAYS):
        period = scheduled_period(schedule, day)
        if period is not None:
            return period, day
        day -= timedelta(days=1)
    return None


def default_period(schedule: str, now: Optional[datetime] = None) -> tuple[date, date]:
    """The period of a manual run without dates: the newest closed period of the report's cadence (the last full ISO
    week / calendar month that has ended); the last EVENT_DEFAULT_DAYS closed days for an event report (R4: CIS-06,
    CIS-07, DATA-03 cover what happened in a window); a single day for daily and other reports."""
    closed = latest_closed_day(now)
    if schedule == "event":
        return closed - timedelta(days=EVENT_DEFAULT_DAYS - 1), closed
    if schedule == "weekly":
        monday, sunday = iso_week(closed)
        return (monday, sunday) if sunday == closed else (monday - timedelta(days=7), monday - timedelta(days=1))
    if schedule == "monthly":
        first, last = month_bounds(closed)
        return (first, last) if last == closed else previous_month(closed)
    day = snapshot_day(now)
    return day, day
