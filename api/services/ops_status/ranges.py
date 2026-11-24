"""AA-1140 — the time range of the Ecosystem status page: presets 1 h / 6 h / 24 h / 7 d or a custom [from, to), the
chart step that follows the span, and the previous period of the same length (compare). Pure.

Steps (mockup v2): a span up to 90 minutes is drawn in 1-minute steps, up to 8 hours in 5-minute steps, up to 3 days
in hourly steps, longer in 6-hourly steps; so a chart has at most ~100 points and a CloudWatch query at most a few
hundred datapoints. These are drawing resolutions, not policy values. The longest custom range is the setting
``cis.ops.status_history_days`` (the samples' kept history), passed in by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

PRESETS: dict[str, timedelta] = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24),
                                 "7d": timedelta(days=7)}
DEFAULT_PRESET = "24h"
# (largest span, step): the first row whose span covers the range gives its step.
_STEPS: tuple[tuple[timedelta, int], ...] = (
    (timedelta(minutes=90), 60), (timedelta(hours=8), 300), (timedelta(days=3), 3600))
_LONG_STEP_S = 21600
MIN_SPAN = timedelta(minutes=10)  # technical-constant: a shorter custom range has fewer than two 5-minute samples


class RangeError(ValueError):
    """A range the page cannot show (bad dates, reversed, in the future, older than the kept history)."""


@dataclass(frozen=True)
class TimeRange:
    key: str                 # 1h | 6h | 24h | 7d | custom
    start: datetime
    end: datetime
    step_s: int

    @property
    def span(self) -> timedelta:
        return self.end - self.start

    @property
    def is_live(self) -> bool:
        """A preset ends now; a custom range ends at its own ``to``."""
        return self.key != "custom"

    def previous(self) -> "TimeRange":
        """The period of the same length right before (compare with the previous period)."""
        return TimeRange(self.key, self.start - self.span, self.start, self.step_s)

    def buckets(self) -> list[datetime]:
        """The start of every chart step in [start, end)."""
        out, t = [], self.start
        step = timedelta(seconds=self.step_s)
        while t < self.end:
            out.append(t)
            t += step
        return out

    def as_dict(self) -> dict:
        return {"key": self.key, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "step_s": self.step_s, "span_s": int(self.span.total_seconds())}


def step_for(span: timedelta) -> int:
    for limit, step in _STEPS:
        if span <= limit:
            return step
    return _LONG_STEP_S


def _floor(t: datetime, step_s: int) -> datetime:
    epoch = int(t.timestamp())
    return datetime.fromtimestamp(epoch - epoch % step_s, tz=timezone.utc)


def _parse(value: str, name: str) -> datetime:
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as e:
        raise RangeError(f"{name} is not an ISO date-time") from e
    if t.tzinfo is None:
        raise RangeError(f"{name} needs a time zone (e.g. +07:00)")
    return t.astimezone(timezone.utc)


def resolve(key: Optional[str], start: Optional[str], end: Optional[str], *, now: datetime,
            history_days: int) -> TimeRange:
    """The range of one request. A preset ends at the next step boundary after ``now`` (so the current step is in
    and every viewer in the same step shares one cache entry); a custom range is aligned to its step."""
    now = now.astimezone(timezone.utc)
    oldest = now - timedelta(days=history_days)
    key = (key or DEFAULT_PRESET).strip().lower()
    if key in PRESETS:
        span = PRESETS[key]
        step = step_for(span)
        stop = _floor(now, step) + timedelta(seconds=step)
        return TimeRange(key, stop - span, stop, step)
    if key != "custom":
        raise RangeError(f"unknown range {key!r}: use 1h, 6h, 24h, 7d or custom")
    if not start or not end:
        raise RangeError("a custom range needs from and to")
    a, b = _parse(start, "from"), _parse(end, "to")
    if b <= a:
        raise RangeError("to must be after from")
    if b - a < MIN_SPAN:
        raise RangeError("a custom range must be at least 10 minutes")
    if b > now + timedelta(minutes=5):
        raise RangeError("to is in the future")
    if a < oldest - timedelta(minutes=5):
        raise RangeError(f"history is kept {history_days} days: from must be within it")
    step = step_for(b - a)
    a, b = _floor(a, step), _floor(b, step) + (timedelta(seconds=step) if _floor(b, step) < b else timedelta(0))
    return TimeRange("custom", a, b, step)
