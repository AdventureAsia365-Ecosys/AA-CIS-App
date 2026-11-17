"""AA-1113 (root ADR 0009 decision 10): the regex detectors of the free-text PII scrub, ported from the CRM's F3 pass 2.

Source: AA-Partner ``api/app/privacy/patterns.py`` with the folding helpers of ``api/app/privacy/text.py`` (AA-938,
AA-871; both unchanged on AA-Partner main 183a98c8, last changed in 4a7fa4df). The detectors and their order are the
CRM's; the code is copied, never imported across repos. A change here that the CRM did not make is a drift: port it
there too, or note why not in docs/implementation-notes/AA-1113.md.

What is found (kind):

- ``LINK``: signed or token-bearing URLs (``X-Amz-Signature``, ``token=``, ``sig=``, ``code=``, a long random path
  segment, a ``/c/`` client-portal link). Plain links (an agency's website, a catalog page) stay;
- ``SECRET``: JWTs;
- ``EMAIL``;
- ``DOB``: a date next to a birth cue, or any full date at least two years in the past (found, but the gateway scrub
  leaves dates alone, as the CRM log scrub does: prompts are full of trip dates);
- ``CARD``: 13-19 digits passing the Luhn check;
- ``NATIONAL_ID``: a 12-digit Vietnamese CCCD (leading 0), or 9 / 12 digits after an id cue (``CCCD``, ``ID card`` ...);
- ``PASSPORT``: one or two capitals and seven digits (``C1234567``), or 6-9 letters / digits after a passport cue;
- ``PHONE``: ``+`` and 8-15 digits; Vietnamese mobiles ``0[35789]`` + 8 digits and ``84`` forms; landlines ``02x``;
  North American ``(415) 555-0132``; 8-15 digits after a phone cue (``Tel``, ``SĐT``, ``Zalo``, ``WhatsApp`` ...);
- ``OTP``: 4-8 digits after a code cue (``OTP``, ``verification code``, ``mã xác nhận`` ...).

Digit groups separated by one space, dot or dash are read together ("0912 345 678"), and a run that does not classify
as a whole is tried window by window. A window holds at most :data:`MAX_WINDOW_DIGITS` digits, so the cost is linear in
the text. UUIDs and ``<KIND_n>`` placeholders are never touched, so ids in a prompt survive. Cues are matched in folded
text (no diacritics, lower case). Pure: no I/O, no logging of values; :func:`find` returns spans only.
"""

from __future__ import annotations

import datetime as dt
import functools
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit


# ── folding (AA-Partner app/privacy/text.py) ──

_SPECIAL = {"đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "æ": "ae", "Æ": "ae"}


def fold_char(ch: str) -> str:
    if ch in _SPECIAL:
        return _SPECIAL[ch]
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """``(folded, index)``: ``index[i]`` is the position in ``text`` of the character folded position ``i`` came from;
    ``index[len(folded)] == len(text)`` (so a match end maps too)."""
    out: list[str] = []
    index: list[int] = []
    for pos, ch in enumerate(text):
        folded = fold_char(ch)
        out.append(folded)
        index.extend([pos] * len(folded))
    index.append(len(text))
    return "".join(out), index


def original_span(index: list[int], start: int, end: int) -> tuple[int, int]:
    """The original ``[start, end)`` of the folded ``[start, end)``. Combining marks after the last base character
    (an NFD spelling) belong to the match."""
    o_start = index[start]
    o_end = index[end - 1] + 1 if end > start else o_start
    return o_start, o_end


# ── detectors (AA-Partner app/privacy/patterns.py) ──

# Highest priority first: an overlap keeps the earlier kind.
KINDS = ("LINK", "SECRET", "EMAIL", "DOB", "CARD", "NATIONAL_ID", "PASSPORT", "PHONE", "OTP")
_PRIORITY = {k: i for i, k in enumerate(KINDS)}


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str


PLACEHOLDER = re.compile(r"<([A-Z][A-Z_]{1,20})_(\d{1,4})>")
UUID = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")

URL = re.compile(r"\bhttps?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
_URL_TRAIL = ".,;:!?"
SIGNED_PARAMS = frozenset(
    {
        "signature",
        "sig",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "otp",
        "code",
        "key",
        "api_key",
        "apikey",
        "auth",
        "jwt",
        "session",
        "sid",
        "hash",
        "secret",
        "password",
    }
)
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-.~%=+]{24,}$")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
# Starts only where a local part can start, so a long run without "@" is scanned once (no quadratic backtracking).
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_RE = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?"
    r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
# All date patterns run on folded text (lower case, no diacritics).
NUM_DATE = re.compile(r"(?<![\d/.\-])(\d{1,2})([/.\-])(\d{1,2})\2(\d{4}|\d{2})(?![\d/.\-]*\d)")
ISO_DATE = re.compile(r"(?<![\d/.\-])(\d{4})([/.\-])(\d{1,2})\2(\d{1,2})(?![\d/.\-]*\d)")
DMY_TEXT = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_RE + r"\.?,?\s+(\d{4})\b")
MDY_TEXT = re.compile(r"\b" + _MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
VN_DATE = re.compile(r"\b(?:ngay\s+)?(\d{1,2})\s+thang\s+(\d{1,2})(?:\s*(?:,|nam)\s*|\s+)(\d{4})\b")
YEAR_AFTER_CUE = re.compile(
    r"\b(?:dob|d\.o\.b\.?|date of birth|year of birth|born(?: in)?|sinh nam|nam sinh|ns)\s*[:\-]?\s*((?:19|20)\d{2})\b"
)
DOB_CUE = re.compile(
    r"\b(?:dob|d\.o\.b|date of birth|birth\s?date|birthdate|born|birthday|ngay sinh|sinh ngay|sinh nam|nam sinh|ns)\b"
)
PHONE_CUE = re.compile(
    r"\b(?:tel|telephone|phone|mobile|cell|sdt|so dien thoai|dien thoai|dt|zalo|whatsapp|viber|hotline|call|goi"
    r"|lien he|lh|sms)\b"
)
ID_CUE = re.compile(
    r"\b(?:cccd|cmnd|can cuoc|chung minh|id card|national id|identity card|id no|id number|ssn|social security)\b"
)
PASSPORT_CUE = re.compile(r"\b(?:passport|ho chieu|pp)\b")
OTP_CUE = re.compile(
    r"\b(?:otp|one[- ]time|verification code|security code|confirmation code|confirm code|ma xac nhan|ma xac thuc"
    r"|ma otp|ma pin|pin|passcode|code|ma)\b"
)
PASSPORT_ALNUM = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\d{7}(?![A-Za-z0-9])")
PASSPORT_AFTER_CUE = re.compile(r"\b(?:passport|ho chieu|pp)\s*(?:no\.?|number|num|so)?\s*[:#.\-]?\s*([a-z0-9]{6,9})\b")
# A run of digit groups joined by single separators; the leading "+", "(0xx)" belong to the first group.
DIGIT_RUN = re.compile(r"(?<![\w+])\+?(?:\(\d{1,4}\)[ .\-]?)?\d(?:[ .\-]?(?:\(\d+\)|\d))*(?![\w])")
DIGIT_GROUP = re.compile(r"\+?(?:\(\d+\)|\d+)")
NANP = re.compile(r"^\(?\d{3}\)?[ .\-]?\d{3}[ .\-]\d{4}$")
CUE_WINDOW = 28
# No kind has more digits than a card (19): a longer window can never classify, so it is never built.
MAX_WINDOW_DIGITS = 19  # technical-constant: the longest kind (a card number) has 19 digits


_LUHN_DOUBLE = {str(d): (d * 2 - 9 if d > 4 else d * 2) for d in range(10)}


def luhn_ok(number: str) -> bool:
    odd = sum(int(c) for c in number[-1::-2])
    even = sum(_LUHN_DOUBLE[c] for c in number[-2::-2])
    return (odd + even) % 10 == 0


def protected_spans(text: str) -> list[tuple[int, int]]:
    """Placeholders and UUIDs: never matched by any detector (ids in a prompt survive, numbering stays intact)."""
    return [(m.start(), m.end()) for rx in (PLACEHOLDER, UUID) for m in rx.finditer(text)]


def find(text: str, today: dt.date | None = None) -> list[Span]:
    """Every pass-2 match in ``text``, non-overlapping, sorted by position."""
    today = today or dt.date.today()
    folded, index = fold_with_map(text)
    spans: list[Span] = []
    spans += _links(text)
    spans += [Span(m.start(), m.end(), "SECRET") for m in JWT.finditer(text)]
    spans += [Span(m.start(), m.end(), "EMAIL") for m in EMAIL.finditer(text)]
    spans += _dates(folded, index, today)
    spans += _digit_runs(text, folded, index)
    spans += [Span(m.start(), m.end(), "PASSPORT") for m in PASSPORT_ALNUM.finditer(text)]
    for m in PASSPORT_AFTER_CUE.finditer(folded):
        if any(c.isdigit() for c in m.group(1)):
            spans.append(Span(*original_span(index, m.start(1), m.end(1)), "PASSPORT"))
    return resolve(spans, protected_spans(text))


def resolve(spans: list[Span], protected: list[tuple[int, int]]) -> list[Span]:
    """Drop spans touching a protected range, then keep the highest-priority (then longest) of overlapping spans."""
    blocked = Ranges(protected)
    live = [s for s in spans if s.end > s.start and not blocked.overlaps(s.start, s.end)]
    live.sort(key=lambda s: (_PRIORITY.get(s.kind, len(KINDS)), -(s.end - s.start), s.start))
    chosen = Ranges()
    out: list[Span] = []
    for s in live:
        if not chosen.overlaps(s.start, s.end):
            chosen.add(s.start, s.end)
            out.append(s)
    return sorted(out, key=lambda s: s.start)


class Ranges:
    """Non-overlapping ``[start, end)`` ranges, sorted, with O(log n) overlap checks (the selections of every pass
    stay near-linear however many matches a text has). Ranges given at construction may overlap; they are merged."""

    def __init__(self, ranges: list[tuple[int, int]] | None = None) -> None:
        self.starts: list[int] = []
        self.ends: list[int] = []
        for start, end in sorted(r for r in (ranges or []) if r[1] > r[0]):
            if self.ends and start < self.ends[-1]:
                self.ends[-1] = max(self.ends[-1], end)
            else:
                self.starts.append(start)
                self.ends.append(end)

    def overlaps(self, start: int, end: int) -> bool:
        i = bisect_right(self.ends, start)
        return i < len(self.starts) and self.starts[i] < end

    def add(self, start: int, end: int) -> None:
        """Add a range that overlaps none of the present ones (check with :meth:`overlaps` first)."""
        i = bisect_left(self.starts, start)
        self.starts.insert(i, start)
        self.ends.insert(i, end)


def _links(text: str) -> list[Span]:
    out = []
    for m in URL.finditer(text):
        url = m.group(0).rstrip(_URL_TRAIL)
        if _is_signed(url):
            out.append(Span(m.start(), m.start() + len(url), "LINK"))
    return out


def _is_signed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return True  # unparsable: treat as a token-bearing link (fail closed)
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SIGNED_PARAMS or key.lower().startswith("x-amz-") or _TOKENISH.match(value):
            return True
    if parts.fragment and _TOKENISH.match(parts.fragment.split("=")[-1]):
        return True
    segments = [s for s in parts.path.split("/") if s]
    if segments[:1] == ["c"]:
        return True  # the client portal's links (proposal, OTP sign-in, forms) carry access keys
    return any(_TOKENISH.match(s) and any(c.isdigit() for c in s) and any(c.isalpha() for c in s) for s in segments)


_CLAUSE_END = re.compile(r"[,;|\n]|\.\s|\band\b|\bva\b")


def _cue_window(folded: str, start: int) -> str:
    """The text a cue for ``start`` may sit in: the same clause, at most CUE_WINDOW characters before it."""
    window = folded[max(0, start - CUE_WINDOW) : start]
    ends = list(_CLAUSE_END.finditer(window))
    return window[ends[-1].end() :] if ends else window


def _cue_before(folded: str, start: int, cue: re.Pattern[str]) -> bool:
    """A cue in the same clause, at most CUE_WINDOW characters before ``start`` ("DOB 1/2/1985, trip 3/4/2027": the
    trip date is not a birth date)."""
    return cue.search(_cue_window(folded, start)) is not None


def _valid(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _year4(raw: str, today: dt.date) -> int:
    if len(raw) == 4:
        return int(raw)
    y = int(raw)
    return 2000 + y if 2000 + y <= today.year else 1900 + y


def _dates(folded: str, index: list[int], today: dt.date) -> list[Span]:
    out: list[Span] = []

    def consider(start: int, end: int, candidates: list[dt.date | None], two_digit: bool = False) -> None:
        dates = [d for d in candidates if d is not None]
        if not dates:
            return
        cue = _cue_before(folded, start, DOB_CUE)
        past = all(d.year <= today.year - 2 for d in dates)
        if cue or (past and not two_digit):
            out.append(Span(*original_span(index, start, end), "DOB"))

    for m in NUM_DATE.finditer(folded):
        a, b, y = int(m.group(1)), int(m.group(3)), _year4(m.group(4), today)
        consider(m.start(), m.end(), [_valid(y, b, a), _valid(y, a, b)], two_digit=len(m.group(4)) == 2)
    for m in ISO_DATE.finditer(folded):
        consider(m.start(), m.end(), [_valid(int(m.group(1)), int(m.group(3)), int(m.group(4)))])
    for m in DMY_TEXT.finditer(folded):
        consider(m.start(), m.end(), [_valid(int(m.group(3)), _MONTHS[m.group(2)[:3]], int(m.group(1)))])
    for m in MDY_TEXT.finditer(folded):
        consider(m.start(), m.end(), [_valid(int(m.group(3)), _MONTHS[m.group(1)[:3]], int(m.group(2)))])
    for m in VN_DATE.finditer(folded):
        consider(m.start(), m.end(), [_valid(int(m.group(3)), int(m.group(2)), int(m.group(1)))])
    for m in YEAR_AFTER_CUE.finditer(folded):
        out.append(Span(*original_span(index, m.start(1), m.end(1)), "DOB"))
    return out


class _Cues:
    """The cues before one start position, each looked up once (every window of a start shares them)."""

    __slots__ = ("_window", "_seen")

    def __init__(self, folded: str, start: int) -> None:
        self._window = _cue_window(folded, start)
        self._seen: dict[str, bool] = {}

    def has(self, name: str, cue: re.Pattern[str]) -> bool:
        if name not in self._seen:
            self._seen[name] = cue.search(self._window) is not None
        return self._seen[name]

    def lengths(self, plus: bool) -> frozenset[int]:
        """The digit counts :func:`_classify` can accept for this start; other windows are not built at all."""
        phone = plus or self.has("phone", PHONE_CUE)
        otp = not phone and self.has("otp", OTP_CUE)
        return _lengths(phone, otp, self.has("passport", PASSPORT_CUE), self.has("id", ID_CUE))


@functools.cache
def _lengths(phone: bool, otp: bool, passport: bool, national_id: bool) -> frozenset[int]:
    n = set(range(10, 20))  # without a cue or a "+": VN phones (10-12), CCCD (12), NANP (10), cards (13-19)
    if phone:
        n.update(range(8, 16))
    if otp:
        n.update(range(4, 9))
    if passport:
        n.update(range(6, 10))
    if national_id:
        n.update((9, 12))
    return frozenset(n)


def _classify(raw: str, number: str, plus: bool, cues: _Cues, luhn: bool | None = None) -> str | None:
    """``luhn``: the Luhn check of ``number`` when the caller already knows it (prefix sums in :func:`_digit_runs`).

    The CRM's single function, split in three in the same rule order (the CIS complexity ratchet, AA-1097); the
    rules and their order are unchanged."""
    n = len(number)
    if plus and 8 <= n <= 15:
        return "PHONE"
    if 13 <= n <= 19 and (luhn if luhn is not None else luhn_ok(number)):
        return "CARD"
    return _classify_after_cue(n, cues) or _classify_by_shape(raw, number, cues)


def _classify_after_cue(n: int, cues: _Cues) -> str | None:
    """The kinds only a cue makes: an OTP, a passport, a national id of 9 or 12 digits."""
    if 4 <= n <= 8 and cues.has("otp", OTP_CUE) and not cues.has("phone", PHONE_CUE):
        return "OTP"
    if 6 <= n <= 9 and cues.has("passport", PASSPORT_CUE):
        return "PASSPORT"
    if n in (9, 12) and cues.has("id", ID_CUE):
        return "NATIONAL_ID"
    return None


def _classify_by_shape(raw: str, number: str, cues: _Cues) -> str | None:
    """The kinds told by length and prefix (CCCD, Vietnamese mobiles and landlines, ``84``, NANP), then a phone cue."""
    n = len(number)
    if n == 12 and number.startswith("0"):
        return "NATIONAL_ID"
    if n == 10 and number[0] == "0" and number[1] in "35789":
        return "PHONE"
    if n == 11 and number.startswith("02"):
        return "PHONE"
    if n in (11, 12) and number.startswith("84") and number[2] in "235789":
        return "PHONE"
    if n == 10 and NANP.match(raw.strip()):
        return "PHONE"
    if 8 <= n <= 15 and cues.has("phone", PHONE_CUE):
        return "PHONE"
    return None


def digit_groups(text: str) -> Iterator[list[tuple[int, int, int]]]:
    """Every run of digit groups in ``text``, as ``(start, end, digit count)`` per group."""
    for run in DIGIT_RUN.finditer(text):
        base = run.start()
        yield [
            (g.start() + base, g.end() + base, sum(c.isdigit() for c in g.group(0)))
            for g in DIGIT_GROUP.finditer(run.group(0))
        ]


def window_limits(groups: list[tuple[int, int, int]], i: int, max_digits: int) -> int:
    """The largest ``j`` such that ``groups[i:j]`` holds at most ``max_digits`` digits (``i`` when one group is
    already longer). Callers try the windows ``groups[i:j]`` from this ``j`` down, so a start group costs at most
    ``max_digits`` windows and a run is scanned in linear time."""
    total, j = 0, i
    while j < len(groups) and total + groups[j][2] <= max_digits:
        total += groups[j][2]
        j += 1
    return j


def _digit_runs(text: str, folded: str, index: list[int]) -> list[Span]:
    # Digits fold to themselves, so positions in `folded` equal positions in `text` only when nothing before them
    # expanded; cue lookups therefore go through the folded position of the run's start.
    rev = {orig: i for i, orig in reversed(list(enumerate(index)))}
    out: list[Span] = []
    for groups in digit_groups(text):
        run_digits = "".join(c for c in text[groups[0][0] : groups[-1][1]] if c.isdigit())
        offsets = [0]  # offsets[k] = digits before group k
        for g in groups:
            offsets.append(offsets[-1] + g[2])
        # Luhn in O(1) per window: prefix sums with even positions plain / odd doubled (luhn_a) and the reverse
        # (luhn_b); the window's last digit decides which one applies.
        luhn_a, luhn_b = [0], [0]
        for p, c in enumerate(run_digits):
            plain, double = int(c), _LUHN_DOUBLE[c]
            luhn_a.append(luhn_a[-1] + (plain if p % 2 == 0 else double))
            luhn_b.append(luhn_b[-1] + (double if p % 2 == 0 else plain))
        i = 0
        while i < len(groups):
            matched = False
            start = groups[i][0]
            plus = text[start] == "+"
            cues = _Cues(folded, rev.get(start, start))
            lengths = cues.lengths(plus)
            for j in range(window_limits(groups, i, MAX_WINDOW_DIGITS), i, -1):
                n = offsets[j] - offsets[i]
                if n < 4:
                    break  # shorter windows only get shorter: no kind has fewer than 4 digits
                if n not in lengths:
                    continue
                a, b = offsets[i], offsets[j]
                number = run_digits[a:b]
                end = groups[j - 1][1]
                sums = luhn_a if (b - 1) % 2 == 0 else luhn_b
                kind = _classify(text[start:end], number, plus, cues, luhn=(sums[b] - sums[a]) % 10 == 0)
                if kind:
                    out.append(Span(start, end, kind))
                    i, matched = j, True
                    break
            if not matched:
                i += 1
    return out
