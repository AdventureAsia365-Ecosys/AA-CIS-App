"""AA-1146 (S285) — a person's draft version of a tour's wording: the pure parts (field values, the diff against the
live version, the editors of a draft). The routes are api/routers/admin_pipeline/drafts.py.

A draft is a new silver_aa_internal.generated_content row (status 'draft', origin 'human', CIS 457) copied from the
live version (or a version the person names) with the person's fields applied; it never overwrites a version. It goes
live through the publish gate: the re-check of a human edit (revalidate_passed TRUE, AA-234), the two-person rule
(cis.content.publish_two_person, D1) and ``process_export`` (exit ``human_publish``), which locks the Master.

No DB, no I/O.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any, Iterable, Optional

# The wording fields of a version a person may edit (tour_detail._ALLOWED_GC_FIELDS), in reading order.
DRAFT_FIELDS: tuple[str, ...] = (
    "aa_name", "aa_subtitle", "aa_summary", "aa_description", "aa_highlights", "aa_itineraries", "mobile_card_text",
    "seo_title", "seo_meta", "seo_keywords_used", "og_tags",
)
JSONB_FIELDS = frozenset({"aa_highlights", "seo_keywords_used", "og_tags"})
REASON_MIN, REASON_MAX = 3, 500   # technical-constant: the CHECK bounds of raw_tour_revision.reason (CIS 457)
_WORD = re.compile(r"\S+|\s+")


def _parsed(field: str, value: Any) -> Any:
    """A stored value as the API shows it: JSONB columns as lists / objects (asyncpg hands them back as text)."""
    if field in JSONB_FIELDS and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def fields_of(row: Any) -> dict:
    """{field: value} of the draft fields of a generated_content (or published_tours) row; missing fields are None."""
    out = {}
    for f in DRAFT_FIELDS:
        try:
            out[f] = _parsed(f, row[f])
        except (KeyError, IndexError):
            out[f] = None
    return out


def db_value(field: str, value: Any) -> Any:
    """The value to bind for ``field`` (JSONB columns as JSON text)."""
    if field in JSONB_FIELDS and value is not None and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def word_ops(before: str, after: str) -> list[dict]:
    """A word-level diff: [{op: equal | insert | delete, text}], whitespace kept, so the page can show what changed
    inside a long field (a day of the itinerary) instead of two whole blocks."""
    a, b = _WORD.findall(before), _WORD.findall(after)
    ops: list[dict] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            ops.append({"op": "equal", "text": "".join(a[i1:i2])})
            continue
        if i2 > i1:
            ops.append({"op": "delete", "text": "".join(a[i1:i2])})
        if j2 > j1:
            ops.append({"op": "insert", "text": "".join(b[j1:j2])})
    return ops


def diff_fields(live: Optional[dict], draft: dict) -> list[dict]:
    """The fields of ``draft`` that differ from ``live`` (None = no live version yet: every non-empty field is new),
    each with its before / after and its word-level ops. Fields in DRAFT_FIELDS order."""
    out = []
    for f in DRAFT_FIELDS:
        before = None if live is None else live.get(f)
        after = draft.get(f)
        if _text(before) == _text(after):
            continue
        out.append({"field": f, "before": before, "after": after, "ops": word_ops(_text(before), _text(after))})
    return out


def clean_reason(reason: Any) -> Optional[str]:
    """The reason trimmed, or None when it is missing / outside 3..500 characters."""
    if not isinstance(reason, str):
        return None
    r = reason.strip()
    return r if REASON_MIN <= len(r) <= REASON_MAX else None


def editors_of(edit_diff: Any, author_id: Optional[str]) -> list[str]:
    """Everyone who wrote this draft (its author and each later editor, from edit_diff.editors)."""
    d = edit_diff
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = {}
    names = list((d or {}).get("editors") or []) if isinstance(d, dict) else []
    if author_id:
        names.insert(0, author_id)
    return list(dict.fromkeys(n for n in names if n))


def with_editor(edit_diff: Any, actor: str, fields: Iterable[str], reason: Optional[str]) -> dict:
    """edit_diff of a draft after ``actor`` saved ``fields``: the editors (in order, once each) and the last save."""
    d = edit_diff
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = {}
    d = dict(d) if isinstance(d, dict) else {}
    editors = list(dict.fromkeys([*(d.get("editors") or []), actor]))
    return {**d, "editors": editors, "fields": sorted(set(fields)), "reason": reason, "draft": True}


__all__ = ["DRAFT_FIELDS", "JSONB_FIELDS", "REASON_MAX", "REASON_MIN", "clean_reason", "db_value", "diff_fields",
           "editors_of", "fields_of", "with_editor", "word_ops"]
