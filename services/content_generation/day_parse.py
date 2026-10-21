"""AA-925 (Q-2) — the one place a tour's duration and day markers are read.

A leaf module: it imports nothing from prompts, graph or seo_meta_utils (they import it), so the day
oracle (services/quality/day_oracle.py) and the writer path can share one parser without a cycle.

Before AA-925 there were two duration readers that disagreed: `prompts._estimate_day_count` took the first
integer ("3 hours" → 3 days, so a 3-hour ride was split into Day 1–4 by the even-split fallback; "13 Nights
14 days" → 13) and `seo_meta_utils.tour_days` read days/nights but not "7D/6N", weeks or sub-day forms.
Both are now thin wrappers over `parse_duration` (lesson: one value computed on two code paths must use
one function).

* `parse_duration(text)` → `Duration(days, nights, sub_day, hours, ambiguous, range)`.
* `source_day_markers(text)` → `[(day_from, day_to, line_idx)]` — the source "Day N" lines with ranges
  expanded ("Days 1–3:" → (1, 3)); the marker regex is the one `parse_source_day_word_counts` splits on.
* `output_days(text)` → `[(n, title, first_sentence)]` — the canonical "Day N — Title" blocks of a written
  itinerary in order, duplicates kept (the itinerary_utils regex).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# AA-353 / S207 (moved from prompts.py, which re-exports them): line-anchored "Day N" markers, a leading
# bullet allowed, spelled-out numbers, ranges ("Days 1–3:"). Group 1 = the (first) day, group 2 = a range end.
_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three twenty-four "
    "twenty-five twenty-six twenty-seven twenty-eight twenty-nine thirty".split())}
_DAY_MARKER_RE = re.compile(
    r"^[\s►▶•*#>\-–—·|]*days?\s*#?\s*(\d{1,3}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
    + r")\b(?:\s*[-–—]\s*(\d{1,3})\b)?[*\s]*[:\-–—|.]?\s*",
    re.IGNORECASE)
# "Day 1 - 3 hours drive to Paro" is day 1 with a 3-hour drive, not days 1–3.
_RANGE_UNIT_RE = re.compile(r"^\s*(?:hours?|hrs?|h\b|km|kms|kilomet|miles?|mins?|minutes?|nights?|days?|m\b|ft\b|"
                            r"metres?|meters?|people|pax|persons?)", re.IGNORECASE)
_MAX_RANGE_SPAN = 14


def _day_num(token: str) -> int:
    t = (token or "").lower()
    return int(t) if t.isdigit() else _NUM_WORDS.get(t, 0)


@dataclass(frozen=True)
class Duration:
    days: Optional[int] = None
    nights: Optional[int] = None
    sub_day: bool = False          # "3 hours", "half day": shorter than one day
    hours: Optional[float] = None
    ambiguous: bool = False        # "10–12 days": a range, not one number
    range: Optional[tuple[int, int]] = None

    @property
    def day_count(self) -> Optional[int]:
        """Days this duration states: days, else nights + 1, else 1 for a sub-day tour, else the low end of
        a range; None when nothing usable was found."""
        if self.days:
            return self.days
        if self.nights:
            return self.nights + 1
        if self.sub_day:
            return 1
        if self.range:
            return self.range[0]
        return None

    @property
    def exact_days(self) -> Optional[int]:
        """A day count the oracle can compare against: None for a range or a sub-day tour."""
        return None if (self.ambiguous or self.sub_day) else self.day_count


_NUM = r"(\d{1,3}|" + "|".join(sorted((w for w in _NUM_WORDS if w != "zero"), key=len, reverse=True)) + r")"
_RANGE_DAYS_RE = re.compile(r"\b(\d{1,3})\s*(?:-|–|—|to)\s*(\d{1,3})\s*(?:-\s*)?days?\b", re.IGNORECASE)
_DAYS_RE = re.compile(_NUM + r"\s*(?:-\s*)?days?\b", re.IGNORECASE)
_NIGHTS_RE = re.compile(_NUM + r"\s*(?:-\s*)?nights?\b", re.IGNORECASE)
_DN_RE = re.compile(r"\b(\d{1,3})\s*d\s*/?\s*(\d{1,3})\s*n\b", re.IGNORECASE)        # 7D/6N, 7D6N
_D_ONLY_RE = re.compile(r"\b(\d{1,3})\s*d\b(?!\s*/?\s*\d)", re.IGNORECASE)            # 7D
_WEEKS_RE = re.compile(_NUM + r"\s*(?:-\s*)?weeks?\b", re.IGNORECASE)
_HOURS_RE = re.compile(r"\b(\d{1,2}(?:[.,]\d)?)\s*(?:-\s*)?(?:hours?|hrs?|h)\b", re.IGNORECASE)
_HALF_DAY_RE = re.compile(r"\bhalf[\s-]?day\b", re.IGNORECASE)
_FULL_DAY_RE = re.compile(r"\b(?:full[\s-]?day|day[\s-](?:trip|tour|excursion))\b", re.IGNORECASE)


def _n(token: str) -> int:
    return _day_num(token)


def parse_duration(text) -> Duration:
    """Read a free-text duration ("14 days/13 nights", "13 Nights 14 days", "7D/6N", "2 weeks", "half day",
    "full day", "3 hours", "1 day", "10–12 days"). An empty/unknown value → Duration() (day_count None)."""
    s = " ".join(str(text or "").split())
    if not s:
        return Duration()
    m = _RANGE_DAYS_RE.search(s)
    if m and int(m.group(2)) > int(m.group(1)):
        lo, hi = int(m.group(1)), int(m.group(2))
        nm = _NIGHTS_RE.search(s)
        return Duration(nights=_n(nm.group(1)) if nm else None, ambiguous=True, range=(lo, hi))
    m = _DN_RE.search(s)
    if m:
        return Duration(days=int(m.group(1)), nights=int(m.group(2)))
    days_m, nights_m = _DAYS_RE.search(s), _NIGHTS_RE.search(s)
    days = _n(days_m.group(1)) if days_m else None
    nights = _n(nights_m.group(1)) if nights_m else None
    if days or nights:
        return Duration(days=days or None, nights=nights or None)
    m = _WEEKS_RE.search(s)
    if m and _n(m.group(1)):
        return Duration(days=_n(m.group(1)) * 7)
    m = _D_ONLY_RE.search(s)
    if m and int(m.group(1)):
        return Duration(days=int(m.group(1)))
    if _HALF_DAY_RE.search(s):
        return Duration(sub_day=True, hours=4.0)
    m = _HOURS_RE.search(s)
    if m:
        hours = float(m.group(1).replace(",", "."))
        if hours < 24:
            return Duration(sub_day=True, hours=hours)
        return Duration(days=max(1, round(hours / 24)))
    if _FULL_DAY_RE.search(s):
        return Duration(days=1)
    return Duration()


def marker_match(line: str):
    """The `_DAY_MARKER_RE` match of a stripped source line whose day is > 0, else None."""
    m = _DAY_MARKER_RE.match((line or "").strip())
    return m if m and _day_num(m.group(1)) > 0 else None


def source_day_markers(text: str) -> list[tuple[int, int, int]]:
    """(day_from, day_to, line_idx) for every source line that starts with a "Day N" marker; a range
    "Days 1–3" gives (1, 3), a single day (n, n). A "range" followed by a unit ("Day 1 - 3 hours …") or one
    that runs backwards / spans more than two weeks is read as a single day."""
    out = []
    for idx, line in enumerate((text or "").splitlines()):
        stripped = line.strip()
        m = marker_match(stripped)
        if not m:
            continue
        first = _day_num(m.group(1))
        last = first
        if m.group(2):
            end = int(m.group(2))
            if first < end <= first + _MAX_RANGE_SPAN and not _RANGE_UNIT_RE.match(stripped[m.end(2):]):
                last = end
        out.append((first, last, idx))
    return out


_SENT_END_RE = re.compile(r"(?<=[.!?])\s")


def output_days(text: str) -> list[tuple[int, str, str]]:
    """(day, title, first_sentence) of each canonical "Day N — Title\\nBody" block of a written itinerary,
    in order, duplicates kept (parse_canonical_itinerary_days keys by day and would hide a duplicate)."""
    from .itinerary_utils import _CANONICAL_DAY_RE

    text = text or ""
    matches = list(_CANONICAL_DAY_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        first = _SENT_END_RE.split(body, maxsplit=1)[0].strip() if body else ""
        out.append((int(m.group(1)), m.group(2).strip(), first))
    return out
