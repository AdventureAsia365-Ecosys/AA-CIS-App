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
* `trusted_day_ranges(text, markers, duration)` → `{first: last}` — the subset of those ranges the day gap check
  and the v1/T2 per-day length list count (a separator after the range end, cut at the next marker and the duration).
* `output_days(text)` → `[(n, title, first_sentence)]` — the canonical "Day N — Title" blocks of a written
  itinerary in order, duplicates kept, a ranged block expanded to each day it covers.
* `canonical_blocks(text)` → `[CanonicalBlock(first, last, title, body)]` — the canonical blocks themselves, the one
  reader every written-itinerary parser builds on (itinerary_utils, output_days, places.validity, the SQL twin
  catalog._tour_day_text from CIS 289).

AA-990 (S253) — ONE ranged-heading rule. A heading "Day A-B" (hyphen, en dash or "to"; "Day" or "Days") covers every
day A..B when A < B <= A + 14 (`day_range`); otherwise (backwards "Day 15-12", a span over two weeks) it is the single
day A. Source markers (`source_day_markers`) and written headings (`CANONICAL_DAY_RE`) apply the same rule, so a
ranged day is never folded into the day before and a day count counts A..B as present. Before, the written-side
readers only knew "Day N —": "Day 12-15 — Beach leisure" was folded into Day 11 (and validate's dash normaliser
rewrote it to "Day 12 — 15 — Beach leisure", which read as Day 12 alone): Sri Lanka b45db4c8 lost days 13-15.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# AA-353 / S207 (moved from prompts.py, which re-exports them): line-anchored "Day N" markers, a leading
# bullet allowed, spelled-out numbers, ranges ("Days 1–3:"). Group 1 = the (first) day, group 2 = a range end.
# AA-990 N3 (S252): a leading quote is allowed too. Spreadsheet sources carry the cell's opening quote on the line
# ('"Day 5 / -Stage 4- ...'): Japan "TRANS-TOHOKU Bike Tour" (12 days) read as Days 1-4, Day 4's block swallowed
# Days 5-12, so the writer v2 day plan asked for 4 days, validate compared 4 with 4 and the day oracle filed
# "12 DAYS vs 4 markers" as a source contradiction; the 4-day page was published. Quotes only: an opening bracket
# would turn a source note ("(Day 14 optional extension available)") into a phantom day the v2 plan then asks for.
_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three twenty-four "
    "twenty-five twenty-six twenty-seven twenty-eight twenty-nine thirty".split())}
_DAY_MARKER_RE = re.compile(
    r"^[\s►▶•*#>\-–—·|\"'“”‘’„«»]*days?\s*#?\s*(\d{1,3}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
    + r")\b(?:\s*(?:[-–—]|to\b)\s*(\d{1,3})\b)?[*\s]*[:\-–—|.]?\s*",
    re.IGNORECASE)
# "Day 1 - 3 hours drive to Paro" is day 1 with a 3-hour drive, not days 1–3.
_RANGE_UNIT_RE = re.compile(r"^\s*(?:hours?|hrs?|h\b|km|kms|kilomet|miles?|mins?|minutes?|nights?|days?|m\b|ft\b|"
                            r"metres?|meters?|people|pax|persons?)", re.IGNORECASE)
# AA-990 review (S253): "Day 2 - 4-hour trek" / "Day 2 - 10-km walk" — a number joined to a word by a hyphen is a
# quantity, not a range end.
_JOINED_WORD_RE = re.compile(r"^[-–][^\W\d_]")
_MAX_RANGE_SPAN = 14


def _quantity_follows(rest: str) -> bool:
    """True when the text after a would-be range end makes that number a quantity ("3 hours", "4-hour")."""
    return bool(_RANGE_UNIT_RE.match(rest) or _JOINED_WORD_RE.match(rest))


def day_range(first: int, last: Optional[int]) -> tuple[int, int]:
    """AA-990 (S253): the days a heading "Day {first}-{last}" covers — (first, last) when first < last <= first + 14,
    else (first, first): a backwards range ("Day 15-12") or one spanning more than two weeks is read as the single
    day `first` (the day-count checks then report whatever that leaves missing), never a silent fold."""
    if last is not None and first < last <= first + _MAX_RANGE_SPAN:
        return first, last
    return first, first


# AA-990 (S253): the canonical heading of a WRITTEN itinerary ("Day N — Title", graph._process_itineraries /
# s1_writer_v2.serialize_days), with an optional range "Day 12-15 — Title" / "Days 12–15 — " / "Day 12 to 15 — ".
# The em dash is not a range separator here: it is the title separator. Group 1 = first day, group 2 = range end
# (validate with `day_range`), group 3 = title. SQL twin: catalog._tour_day_text (CIS 289) — keep them identical.
CANONICAL_HEAD = r"Days?\s+(\d+)(?:[ \t]*(?:[-–]|to)[ \t]*(\d{1,3}))?\s+—"
CANONICAL_DAY_RE = re.compile(r"^" + CANONICAL_HEAD + r"\s*(.*)$", re.MULTILINE)
# the same heading without groups, for splitters and normalisers (lookaheads)
CANONICAL_HEAD_NC = r"Days?\s+\d+(?:[ \t]*(?:[-–]|to)[ \t]*\d{1,3})?\s+—"


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
    "Days 1–3" gives (1, 3), a single day (n, n). A "range" followed by a unit ("Day 1 - 3 hours …", "Day 2 -
    4-hour trek") or one that runs backwards / spans more than two weeks is read as a single day."""
    out = []
    for idx, line in enumerate((text or "").splitlines()):
        stripped = line.strip()
        m = marker_match(stripped)
        if not m:
            continue
        first = _day_num(m.group(1))
        last = first
        if m.group(2):
            if not _quantity_follows(stripped[m.end(2):]):
                last = day_range(first, int(m.group(2)))[1]
        else:
            # S248 (S1-first step 1): a list of consecutive days ("Day 05 & 06:", "Day 11, 12 & 13:") is a range
            # too; before, days 6, 12 and 13 of such a source read as missing and the oracle charged the writer
            # with DAY_OUTPUT_EXTRA (golden s1-004).
            lst = _DAY_LIST_RE.match(stripped[m.end(1):])
            if lst and not _quantity_follows(stripped[m.end(1) + lst.end():]):
                nums = [first] + [int(n) for n in re.findall(r"\d{1,3}", lst.group(0))]
                if all(b == a + 1 for a, b in zip(nums, nums[1:])) and nums[-1] <= first + _MAX_RANGE_SPAN:
                    last = nums[-1]
        out.append((first, last, idx))
    return out


_DAY_LIST_RE = re.compile(r"(?:\s*(?:,|&|\band\b)\s*\d{1,3}\b)+", re.IGNORECASE)
# what may follow the end of a source range heading the day checks trust: nothing, or a heading separator
# (":", "|", ".", a dash followed by a space) — "Days 12-15: Beach", "Day 12 - 15 – Beach", "**Days 12-15**"
_RANGE_HEAD_END_RE = re.compile(r"[*\s]*(?:$|[:|.]|[-–—](?:\s|$))")


def trusted_day_ranges(text: str, markers: list[tuple[int, int, int]], duration=None) -> dict[int, int]:
    """AA-990 review (S253): {first: last} for the source range markers the day checks may count as every day they
    cover (`itinerary_day_gap` — a hard block on writer v2 — and the v1/T2 PER-DAY SOURCE LENGTH list). Stricter
    than `source_day_markers` (which feeds the shadow day oracle and the v2 day plan unchanged), because a range
    read from a title that starts with a number ("Day 3 - 5 temples of Hue in one day") would hold a correct page
    and push the writer to invent days. A range counts when:
    * a heading separator (or the line end) follows the range end — "Days 12-15: Beach", "Day 12-15 — Beach";
      a day list ("Day 11, 12 & 13:") is explicit and needs none;
    * and it is cut at the next marker's day minus 1 (when that marker starts later) and at the tour's exact
      day count (`parse_duration(duration).exact_days`), when known.
    A range cut down to its first day is left out (the day is in `day_word_counts` already)."""
    lines = (text or "").splitlines()
    cap = parse_duration(duration).exact_days if duration else None
    out: dict[int, int] = {}
    for i, (first, last, idx) in enumerate(markers):
        if last <= first or idx >= len(lines):
            continue
        stripped = lines[idx].strip()
        m = marker_match(stripped)
        if m is None or (m.group(2) and not _RANGE_HEAD_END_RE.match(stripped[m.end(2):])):
            continue
        nxt = markers[i + 1][0] if i + 1 < len(markers) else None
        if nxt is not None and nxt > first:
            last = min(last, nxt - 1)
        if cap:
            last = min(last, cap)
        if last > first:
            out[first] = last
    return out


_SENT_END_RE = re.compile(r"(?<=[.!?])\s")


@dataclass(frozen=True)
class CanonicalBlock:
    first: int
    last: int           # == first unless the heading is a valid range ("Day 12-15 —")
    title: str
    body: str

    @property
    def days(self) -> range:
        return range(self.first, self.last + 1)

    @property
    def ranged(self) -> bool:
        return self.last > self.first


def canonical_blocks(text: str) -> list[CanonicalBlock]:
    """The canonical "Day N — Title\\nBody" blocks of a written itinerary in order, duplicates kept; a heading
    "Day A-B —" gives one block covering A..B (`day_range`). [] when the text has no canonical heading."""
    text = text or ""
    matches = list(CANONICAL_DAY_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        first = int(m.group(1))
        last = day_range(first, int(m.group(2)) if m.group(2) else None)[1]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append(CanonicalBlock(first, last, m.group(3).strip(), text[m.end():end].strip()))
    return out


def output_days(text: str) -> list[tuple[int, str, str]]:
    """(day, title, first_sentence) of each canonical "Day N — Title\\nBody" block of a written itinerary,
    in order, duplicates kept (parse_canonical_itinerary_days keys by day and would hide a duplicate). AA-990: a
    ranged block ("Day 12-15 — Beach") gives one entry per day it covers, so the day oracle sees 12..15 present."""
    out = []
    for b in canonical_blocks(text):
        first = _SENT_END_RE.split(b.body, maxsplit=1)[0].strip() if b.body else ""
        out.extend((n, b.title, first) for n in b.days)
    return out
