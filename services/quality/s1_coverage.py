"""AA-645 — figure coverage: the share of a source's operational figures a written page keeps (the "fact coverage"
side metric of the A/B, code-only). Moved from scripts/aa645/s1_screen.py (AA-1024 G17: the wave quality report in
services/waves/quality.py computes it too); s1_screen re-exports the names.
"""
from __future__ import annotations

import re
from typing import Optional

COVERAGE_SKIP_UNITS = frozenset({"day", "night", "week"})   # the trip length: structure, written as words
# Heading-style day references only: capitalised "Day N" / "Days N–M" at the start of a line (after an optional
# bullet) or followed by a dash/colon that is not a range ("Day 3 — Chilaw", "Days 2–3: rest"). Lowercase prose
# ("over 4 days 60 km") and figures glued to a range end ("Day 2 to 3,000 m") keep their numbers.
_DAY_NUM = r"\d+(?!\d|[.,]\d)"
_DAY_HEAD = rf"Days?[ \t]+{_DAY_NUM}(?:[ \t]*(?:[-–—]|to|and)[ \t]*{_DAY_NUM})?"
_DAY_REF_RE = re.compile(rf"^[ \t]*(?:[-•*][ \t]*)?{_DAY_HEAD}|\b{_DAY_HEAD}(?=[ \t]*[-–—:][ \t]*(?!\d))", re.M)


def figure_coverage(facts: dict, page: dict) -> Optional[float]:
    """Share of the source's operational figures (km, hours, metres, … — not the trip length, not the name) whose
    number appears in the page: the "fact coverage" side metric of the A/B (q4-q8 §2.2), code-only. None when the
    source has none."""
    from services.quality.s1_facts import field_text

    nums = {n for e in facts.get("numbers") or [] if e.get("role", "figure") == "figure" and e.get("unit")
            and e.get("unit") not in COVERAGE_SKIP_UNITS and e.get("field") not in ("name", "duration")
            for n in e["numbers"]}
    if not nums:
        return None
    # per field, so each field's first line counts as a line start; "Day 3 — …": structure, not a figure
    text = " ".join(_DAY_REF_RE.sub(" ", field_text(page.get(f)))
                    for f in ("subtitle", "summary", "description", "highlights", "itineraries", "seo_meta"))
    have = set(re.findall(r"\d+(?:[.,]\d+)?", text))
    return round(len({n for n in nums if n in have}) / len(nums), 3)
