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
