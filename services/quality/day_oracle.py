"""AA-925 (Q-2) — the S1 day-count oracle (class `struct.day_structure`, design §6.4). Deterministic, $0.

Compares the SOURCE itinerary's day markers and stated duration with the WRITTEN itinerary's day blocks:

| code | fires when |
|---|---|
| DAY_SUBDAY_SPLIT | the duration is shorter than a day ("3 hours", "half day") and the output has more than one day |
| DAY_OUTPUT_DUPLICATE | an output day number appears twice |
| DAY_OUTPUT_ORDER | output day numbers go backwards |
| DAY_OUTPUT_GAP | the output skips a day number the source has (or, with no source markers, any internal gap) |
| DAY_OUTPUT_EXTRA | an output day the source does not have (or past the stated duration) |
| DAY_DEPARTURE_MIDTRIP | a day that is not the last is titled as the departure |
| DAY_COUNT_VS_DURATION | the output's day count differs from the stated duration, the source being consistent |
| SOURCE_DAY_CONTRADICTION | the source contradicts itself ("14 days" but 12 day markers): class |
|   | `source.contradiction`, the writer is not charged and the count check is not run |
| DAY_CHECK_SKIPPED | no source markers and no usable duration: nothing to compare (a `degraded` entry, AA-925) |

A day the source itself lacks is never charged to the writer: `ITINERARY_DAY_COUNT_MISMATCH` (graph.validate_node)
still owns "a source day is missing from the output". Every code ships in `shadow` (`DAY_ORACLE_MODES`): it is
recorded in `quality_shadow` and changes no score or verdict until its precision is read on stored tours
(scripts/aa925/day_oracle_report.py, D5: ≥ 0.70 with the Wilson bound reported) and the code is flipped to
`enforce` — A1 only, never a tenant rewrite (T2 writes from the master text, lesson AA-800).

Pure: imports only day_parse and prompts.parse_source_day_word_counts (the one day splitter).
"""
from __future__ import annotations

import re
from typing import Optional

from services.content_generation.day_parse import output_days, parse_duration
from services.content_generation.prompts import parse_source_day_word_counts

DETECTOR = "day_oracle"
CLASS_DAY = "struct.day_structure"
CLASS_SOURCE = "source.contradiction"

DAY_SUBDAY_SPLIT = "DAY_SUBDAY_SPLIT"
DAY_OUTPUT_DUPLICATE = "DAY_OUTPUT_DUPLICATE"
DAY_OUTPUT_ORDER = "DAY_OUTPUT_ORDER"
DAY_OUTPUT_GAP = "DAY_OUTPUT_GAP"
DAY_OUTPUT_EXTRA = "DAY_OUTPUT_EXTRA"
DAY_DEPARTURE_MIDTRIP = "DAY_DEPARTURE_MIDTRIP"
DAY_COUNT_VS_DURATION = "DAY_COUNT_VS_DURATION"
SOURCE_DAY_CONTRADICTION = "SOURCE_DAY_CONTRADICTION"
DAY_CHECK_SKIPPED = "DAY_CHECK_SKIPPED"

# shadow | enforce. Enforce applies to A1 only (graph.validate_node); flipping a code needs its precision read
# (D5) in the same PR. Every code ships in shadow.
DAY_ORACLE_MODES: dict[str, str] = {
    DAY_SUBDAY_SPLIT: "shadow",
    DAY_OUTPUT_DUPLICATE: "shadow",
    DAY_OUTPUT_ORDER: "shadow",
    DAY_OUTPUT_GAP: "shadow",
    DAY_OUTPUT_EXTRA: "shadow",
    DAY_DEPARTURE_MIDTRIP: "shadow",
    DAY_COUNT_VS_DURATION: "shadow",
    SOURCE_DAY_CONTRADICTION: "shadow",   # never enforced: a source fault is not the writer's
    DAY_CHECK_SKIPPED: "shadow",          # never enforced: it is a degraded marker, not a finding
}
NEVER_ENFORCED = frozenset({SOURCE_DAY_CONTRADICTION, DAY_CHECK_SKIPPED})
CLASS_OF = {code: (CLASS_SOURCE if code == SOURCE_DAY_CONTRADICTION else CLASS_DAY) for code in DAY_ORACLE_MODES}


def enforced_codes() -> frozenset[str]:
    """Codes in enforce mode (A1 hard-block codes; graph._HARD_BLOCK_CODES includes them)."""
    return frozenset(c for c, m in DAY_ORACLE_MODES.items() if m == "enforce" and c not in NEVER_ENFORCED)


_DEPARTURE_RE = re.compile(r"\b(departure|depart(?:s|ing)? for home|fly(?:ing)? home|farewell|final day|"
                           r"return home|homeward)\b", re.IGNORECASE)


def _finding(code: str, detail: str) -> dict:
    return {"code": code, "class_id": CLASS_OF[code], "detector": DETECTOR, "mode": DAY_ORACLE_MODES[code],
            "detail": detail}


def source_days(source_itineraries: str, duration: Optional[str]) -> tuple[Optional[set[int]], list]:
    """The set of day numbers the source has (ranges expanded), or None when it has fewer than two markers
    (the splitter's fallback). Returns (days, markers)."""
    parsed = parse_source_day_word_counts(source_itineraries or "", duration or "")
    markers = parsed.get("markers") or []
    if parsed["used_fallback"]:
        return None, markers
    days: set[int] = set()
    for first, last, _idx in markers:
        days.update(range(first, last + 1))
    return days, markers


def check(source_itineraries: str, duration: Optional[str], output_itineraries: str) -> list[dict]:
    """Findings ({code, class_id, detector, mode, detail}) for one written itinerary against its source."""
    dur = parse_duration(duration)
    src, _markers = source_days(source_itineraries, duration)
    out = output_days(output_itineraries)
    if src is None and dur.day_count is None:
        return [_finding(DAY_CHECK_SKIPPED, "no source day markers and no usable duration")]
    findings: list[dict] = []
    nums = [n for n, _t, _f in out]
    if not nums:
        return findings  # ITINERARY_STRUCTURE_WEAK (validate) owns an itinerary with no day blocks
    distinct = sorted(set(nums))

    if dur.sub_day and len(distinct) > 1:
        findings.append(_finding(DAY_SUBDAY_SPLIT, f"duration {duration!r} is under a day but the output has "
                                                   f"{len(distinct)} days"))
    dups = sorted({n for n in nums if nums.count(n) > 1})
    if dups:
        findings.append(_finding(DAY_OUTPUT_DUPLICATE, "day " + "/".join(map(str, dups)) + " written twice"))
    backwards = [(a, b) for a, b in zip(nums, nums[1:]) if b < a]
    if backwards:
        findings.append(_finding(DAY_OUTPUT_ORDER, "; ".join(f"day {b} after day {a}" for a, b in backwards[:3])))
    internal = [n for n in range(distinct[0], distinct[-1] + 1) if n not in set(distinct)]
    gaps = [n for n in internal if src is None or n in src]
    if gaps:
        findings.append(_finding(DAY_OUTPUT_GAP, "day " + "/".join(map(str, gaps)) + " skipped"))

    exact = dur.exact_days
    contradiction = False
    if src is not None and exact is not None and len(src) != exact and max(src) != exact:
        contradiction = True
        findings.append(_finding(SOURCE_DAY_CONTRADICTION, f"duration {duration!r} says {exact} days, the "
                                                           f"source has {len(src)} day(s) (max day {max(src)})"))
    if src is not None:
        extra = [n for n in distinct if n not in src]
    elif exact is not None:
        extra = [n for n in distinct if n > exact]
    else:
        extra = []
    if extra and not dur.sub_day:
        findings.append(_finding(DAY_OUTPUT_EXTRA, "day " + "/".join(map(str, extra)) + " not in the source"))

    last_pos = len(out) - 1
    mid = [n for i, (n, title, _f) in enumerate(out) if i < last_pos and _DEPARTURE_RE.search(title or "")]
    if mid:
        findings.append(_finding(DAY_DEPARTURE_MIDTRIP, "departure title on day " + "/".join(map(str, mid))))

    if exact is not None and not contradiction and not dur.sub_day and len(distinct) != exact:
        findings.append(_finding(DAY_COUNT_VS_DURATION, f"{len(distinct)} output day(s) for a {exact}-day duration"))
    return findings
