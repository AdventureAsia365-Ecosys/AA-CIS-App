"""AA-778 (S238) — no em-dash in a T2 tenant tour version.

chị Thư bans the em-dash in tenant copy. AA-778 #749 fixed the T9 pieces (hygiene.fix_dashes inside
hygiene.autofix), but a T2 rewrite never passes through hygiene: the S238 T2 versions of the Mongolia
15-day tour (Trail Pulse, Atlas & Hearth) still carried 43-45 em-dashes each, 33-34 of them in the
itinerary (15 of those the "Day N — Title" marker the graph writes itself).

`fix_t2_dashes` runs once in services/jobs/t2_rewrite_job.py, on the version's `rewritten_content`
right before the UPDATE, after the writer, the judge, flag-fix and the T3 QA gate are all done. Only
the T2 job calls it, so A1 Master content (published_tours) is untouched. It reuses
hygiene.fix_dashes with a context built from the SOURCE tour (the published_tours fields the writer
read), so:
- the Master name written verbatim ("Mongolia: Khövsgöl Lake and Gobi Desert — 15 Days") keeps its
  dash, in the name field and anywhere else it is quoted whole;
- a quoted span copied word for word from the source keeps its dash;
- a dash between two numbers becomes a range (en-dash), never a colon.
A name the tenant's writer made up is tenant copy and is converted like the rest
("…to the Gobi Desert — 15 Days" → "…to the Gobi Desert, 15 Days" when the name already has a colon).

The canonical itinerary marker "Day N — Title" (graph._process_itineraries) becomes "Day N: Title"
first, so a dash inside the title then reads as the second one on the line (", ") instead of
pairing with the marker. The separator is then picked once per tour, after fix_dashes: if any day
title still holds a colon of its own ("Day 4: Northward to Lake Khövsgöl: Taiga Shore", common since
the rule 5 title example has one), every day marker in that itinerary becomes "Day N · Title", so no
day line carries two colons and the tour reads one way throughout; otherwise "Day N: Title" stays.
Not "Day N. Title": hygiene.sentence_spans (the admin T2 sentence editor) and grounding's sentence
split would cut "Day N." off as a sentence of its own, while " · " keeps the line one span, as the
em-dash form did. Nothing reads a stored T2 itinerary back with
itinerary_utils.parse_canonical_itinerary_days: A3 atomize reads published_tours, the portal and the
.docx export print the text, and the admin T2 sentence editor (admin_content_quality) splits
sentences with hygiene.sentence_spans.

Pure: no DB, no LLM, no I/O.
"""
from __future__ import annotations

import json
import re
from typing import Any

from services.acp_content_writing.hygiene import HygieneContext, fix_dashes

_EM = "—"
# the customer-facing text fields of a T2 version (t2_rewrite_job's `rewritten` dict); `trip_type` is a
# label ('cultural') but is converted the same way in case a model writes prose there
TEXT_FIELDS = ("name", "subtitle", "summary", "highlights", "itineraries", "seo_title", "seo_meta", "trip_type")
_CANONICAL_MARKER_RE = re.compile(rf"^(Day[ \t]+\d+)[ \t]+{_EM}[ \t]*", re.MULTILINE)
# a day line after the marker became a colon: group 1 the "Day N" marker, group 2 the title
_COLON_DAY_LINE_RE = re.compile(r"^(Day[ \t]+\d+): (.*)$", re.MULTILINE)
# the marker used for the whole tour when a day title has its own colon (see the module docstring)
DAY_MARKER_ALT = " · "


def em_dash_count(value: Any) -> int:
    """Em-dashes in a field value or a whole rewritten_content dict (every string inside, recursively)."""
    if isinstance(value, str):
        return value.count(_EM)
    if isinstance(value, dict):
        return sum(em_dash_count(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(em_dash_count(v) for v in value)
    return 0


def _source_text(source: dict) -> str:
    parts = []
    for v in (source or {}).values():
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        if v:
            parts.append(str(v))
    return "\n".join(parts)


def t2_dash_context(source: dict) -> HygieneContext:
    """The fix_dashes context for one T2 version: the source tour's text as the seed (quoted spans
    copied from it are kept) and the Master name as the trip name (kept verbatim)."""
    return HygieneContext(seed=_source_text(source), trip_name=(source or {}).get("name") or None)


def _fix_text(text: str, ctx: HygieneContext, *, itinerary: bool = False) -> str:
    if itinerary:
        text = _CANONICAL_MARKER_RE.sub(r"\1: ", text)
    return fix_dashes(text, ctx)[0]


def unify_day_markers(itinerary: str) -> str:
    """One day separator per tour: "Day N: Title" unless some title has its own colon, then every
    "Day N: " marker becomes "Day N · " (no day line with two colons, no mixed forms)."""
    lines = _COLON_DAY_LINE_RE.findall(itinerary)
    if not any(":" in title for _, title in lines):
        return itinerary
    return _COLON_DAY_LINE_RE.sub(lambda m: f"{m.group(1)}{DAY_MARKER_ALT}{m.group(2)}", itinerary)


def _fix_value(value: Any, ctx: HygieneContext, *, itinerary: bool = False) -> Any:
    if isinstance(value, str):
        return _fix_text(value, ctx, itinerary=itinerary) if _EM in value or " – " in value else value
    if isinstance(value, list):  # highlights; an itinerary stored as day objects (older shape)
        return [_fix_value(v, ctx, itinerary=itinerary) for v in value]
    if isinstance(value, dict):  # {"day", "title", "body"}
        return {k: (_fix_value(v, ctx) if k in ("title", "body", "description") else v) for k, v in value.items()}
    return value


def fix_t2_dashes(rewritten: dict, source: dict) -> tuple[dict, list[str]]:
    """`rewritten` (a T2 version's rewritten_content) with every em-dash the writer used as a dash
    replaced (hygiene.fix_dashes rules), in every customer-facing text field. `source` is the dict the
    writer read (t2_rewrite_job.source_tour_dict). Returns (new dict, log). Other keys are unchanged."""
    ctx = t2_dash_context(source)
    out = dict(rewritten)
    log: list[str] = []
    for field in TEXT_FIELDS:
        if field not in out:
            continue
        before = em_dash_count(out[field])
        out[field] = _fix_value(out[field], ctx, itinerary=field == "itineraries")
        after = em_dash_count(out[field])
        if before != after:
            log.append(f"{field}: {before} -> {after} em-dash(es)")
        if field == "itineraries" and isinstance(out[field], str):
            unified = unify_day_markers(out[field])
            if unified != out[field]:
                out[field] = unified
                log.append(f"itineraries: day marker 'Day N{DAY_MARKER_ALT}' (a day title has its own colon)")
    return out, log
