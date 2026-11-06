"""S271 W0 (root ADR 0020 D8, AA-900 l16) — the elephant-riding policy detector, one copy for S1 and T2.

The policy (business owner, 08/10/2026): a tour whose product offers elephant riding is kept AS IT IS and goes to a
human review; the system never strips the ride and never regenerates the text to hide it.

- **S1 (A1 Master):** a hit on the generated page OR on the raw source adds ``FACT_CHECK_MANUAL_CHECK`` in
  ``brand_audit_node.pre_audit_checks`` (manual_check, so the tour goes to the Review Queue and is never
  auto-published); ``flag_fix_node`` makes no LLM edit on such a tour (it could rewrite the ride away).
- **T2 (tenant rewrite):** a hit on the Master text or the raw source is ``policy_hold`` v0 (D8): the version is the
  Master text verbatim with the SEO title / meta fitted by code, 0 LLM calls, a review row; the tenant still receives
  it (``services/jobs/t2_rewrite_job.py``).

Before S271 the only detector was ``elephant[-\\s]?back|elephant\\s+ride|elephant\\s+trek`` over the generated
highlights: the policy's own wording ("elephant riding", "riding an elephant", "ride an elephant") did not match, and
the itinerary, summary, description and source were never read (audit S271, tour-pipeline-audit F-elephant).

What matches (case-insensitive, plural and inflected forms):

- ``elephant riding`` / ``elephant ride(s)`` / ``elephant trek(s|king)`` / ``elephant-back`` / ``elephant back`` /
  ``elephant safari(s)`` (an Indian park "elephant safari" is a ride on an elephant);
- ``ride / rides / riding / rode / ridden [on] [an|a|the|your|our|baby] elephant(s)``;
- ``on / by elephant-back``, ``mount an elephant``, ``back of an elephant``.

What does not: a negated mention in the same clause ("no elephant riding", "without riding the elephants",
"elephant riding is not offered", "a riding-free sanctuary"), and elephant mentions with no ride ("bathe the
elephants", "an elephant sanctuary", "watch wild elephants").

Pure: no DB, no LLM, no I/O.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

# The brand-audit code a hit adds (brand_audit_node._MANUAL_CHECK_CODES: manual_check, Review Queue).
POLICY_CODE = "FACT_CHECK_MANUAL_CHECK"
# The policy-hold reason a T2 version and its review row carry (D8).
POLICY_HOLD = "elephant_riding"

_ELE = r"elephants?"
_ART = r"(?:(?:an|a|the|your|our|their|baby|young|gentle|trained|domestic(?:ated)?)\s+){0,2}"
_RIDE_VERB = r"(?:ride|rides|riding|rode|ridden)"
ELEPHANT_RIDING_RE = re.compile(
    r"\b(?:"
    # "elephant riding / ride(s) / trek(s) / trekking / back / safari(s)"; "elephant-back", "elephant back"
    rf"{_ELE}[-\s]+(?:{_RIDE_VERB}|treks?|trekking|trekked|back(?:ed)?|safaris?)\b"
    # "ride on an elephant", "riding the elephants", "rode a baby elephant"
    rf"|{_RIDE_VERB}\s+(?:on\s+|atop\s+)?{_ART}{_ELE}\b"
    # "mount an elephant", "climb onto an elephant", "the back of an elephant"
    rf"|(?:mount|mounts|mounting|mounted)\s+{_ART}{_ELE}\b"
    rf"|back\s+of\s+{_ART}{_ELE}\b"
    r")",
    re.IGNORECASE,
)

# A negation in the same clause, just before the match ("no elephant riding", "without riding the elephants",
# "we never ride elephants", "instead of an elephant ride").
_NEG_BEFORE_RE = re.compile(
    r"\b(?:no|not|never|without|non|instead\s+of|rather\s+than|avoid(?:s|ing)?|ban(?:s|ned)?|free\s+of"
    r"|don['’]?t|doesn['’]?t|do\s+not|does\s+not|won['’]?t|will\s+not|cannot|can['’]?t)"
    r"\b[\w\s'’-]{0,25}$",
    re.IGNORECASE,
)
# A negation right after the match ("elephant riding is not offered", "elephant rides are not included",
# "riding-free").
_NEG_AFTER_RE = re.compile(
    r"^(?:\s*-?\s*free\b|\s+(?:is|are|was|were|will\s+be)\s+(?:not|never|no\s+longer)\b"
    r"|\s+(?:isn['’]?t|aren['’]?t)\b)",
    re.IGNORECASE,
)
_CLAUSE_BREAK_RE = re.compile(r"[.;:!?\n()\[\]]")


def _clause_before(text: str, start: int) -> str:
    """The part of the clause before ``start`` (back to the last sentence / clause break)."""
    head = text[:start]
    cut = max((m.end() for m in _CLAUSE_BREAK_RE.finditer(head)), default=0)
    return head[cut:]


def riding_mentions(text: str) -> list[str]:
    """Every elephant-riding mention in ``text`` that is not negated in its clause (the matched words)."""
    out: list[str] = []
    for m in ELEPHANT_RIDING_RE.finditer(text or ""):
        if _NEG_BEFORE_RE.search(_clause_before(text, m.start())):
            continue
        if _NEG_AFTER_RE.match(text[m.end():m.end() + 40]):
            continue
        out.append(m.group(0))
    return out


def mentions_riding(text: str) -> bool:
    return bool(riding_mentions(text))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(_as_text(v) for v in value.values())
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# The fields read on a generated page / a T2 version / the Master text (graph `generated`, rewritten_content,
# t2_rewrite_job.source_tour_dict): every customer-facing text field. `seo_keywords_used` is metadata (lesson
# S218 AA-738: never scan the keyword list as content).
CONTENT_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries", "seo_title",
                  "seo_meta", "trip_type")
# The raw source fields (grounding.SOURCE_FIELDS shape; raw_tours src_* columns). `exclusions` is not read: a ride
# listed there is a thing the tour does NOT include.
SOURCE_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries", "inclusions",
                 "activities", "price")


def field_hits(doc: Optional[dict], fields: Iterable[str] = CONTENT_FIELDS) -> list[dict]:
    """[{field, mention}] for every non-negated riding mention in ``doc``'s ``fields`` (first mention per field)."""
    hits: list[dict] = []
    for f in fields:
        found = riding_mentions(_as_text((doc or {}).get(f)))
        if found:
            hits.append({"field": f, "mention": found[0]})
    return hits


def scan(generated: Optional[dict] = None, source: Optional[dict] = None) -> list[dict]:
    """Every hit over the generated / rewritten fields and the source fields:
    [{"where": "generated" | "source", "field", "mention"}]. Empty = no elephant riding anywhere."""
    out = [{"where": "generated", **h} for h in field_hits(generated, CONTENT_FIELDS)]
    out += [{"where": "source", **h} for h in field_hits(source, SOURCE_FIELDS)]
    return out


def raw_source_from_row(row: Any) -> dict:
    """The raw-source part of a row that carries raw_tours' src_* columns under their own names (``src_name`` ...,
    ``inclusions``, ``activities``, ``price_raw``), keyed like SOURCE_FIELDS. Missing columns read as empty."""
    def get(key: str) -> Any:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None
    return {
        "name": get("src_name"), "subtitle": get("src_subtitle"), "summary": get("src_summary"),
        "description": get("src_description"), "highlights": get("src_highlights"),
        "itineraries": get("src_itineraries"), "inclusions": get("inclusions"), "activities": get("activities"),
        "price": get("price_raw"),
    }
