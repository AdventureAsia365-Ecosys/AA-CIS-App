"""AA-1146 T1 (tech lead S285) — an Excel re-import of a tour staff already edited, merged field by field.

The supplier sends a new sheet for a tour that exists. "Replace" made a new source version from the sheet and lost
every hand edit; the merge keeps the tour and compares each field:

- a field the sheet leaves empty is not touched (an empty cell never erases a value);
- a field equal to the current value is ``unchanged``;
- a field nobody edited by hand since the import is accepted from the sheet (``auto``; still recorded in the history);
- a field a person edited (a ``staff_edit`` row of silver_aa_internal.raw_tour_revision, or a field a person kept in an
  earlier merge) is a ``conflict``: the person resolves it, ``incoming`` (the sheet) or ``current`` (the hand edit).

The result is a DRAFT of the tour's facts (silver_aa_internal.raw_tour_fact_draft, CIS 457), never a write into the
tour: it goes live only through the publish gate (POST /admin/tours/{id}/fact-draft/publish,
api/routers/admin_pipeline/fact_drafts.py), which refuses a draft whose base values changed since the merge
(:func:`stale_fields`). D5 (tech lead S285): a person without reference_price.write never changes the price text through
a merge either (:func:`withhold`; choosing the sheet's price is refused by the route).

Pure: no DB. The route is POST /admin/upload-staging/{staging_id}/decide with decision ``merge``; the preview is GET
/admin/upload-staging/{staging_id}/merge-preview (api/routers/admin_pipeline/uploads.py).
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional

# Source tour fields a sheet carries and a merge may change, in reading order.
MERGE_FIELDS: tuple[str, ...] = (
    "src_name", "src_subtitle", "country", "duration", "group_size", "price_raw", "period", "provider", "src_summary",
    "src_highlights", "src_itineraries", "src_description", "inclusions", "exclusions", "activities",
    "best_time_to_go", "feature",
)
JSONB_FIELDS = frozenset({"src_highlights", "activities"})
# D5: the price text feeds the reference price Booking reads; only reference_price.write changes it (tour_detail.py).
PRICE_FIELDS = frozenset({"price_raw"})
INCOMING, CURRENT = "incoming", "current"


def _norm(value: Any) -> str:
    """A value as comparable text: JSON text parsed, lists and objects canonical, whitespace collapsed."""
    if isinstance(value, str):
        v = value.strip()
        if v[:1] in "[{":
            try:
                value = json.loads(v)
            except ValueError:
                return " ".join(v.split())
        else:
            return " ".join(v.split())
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return " ".join(str(value).split())


def _empty(value: Any) -> bool:
    return _norm(value) in ("", "[]", "{}")


def hand_edited_fields(revisions: Iterable[Mapping[str, Any]]) -> set[str]:
    """The fields a person owns now, from the tour's history in revision order: a staff edit owns its fields; a merge
    gives back to the sheet every field it took from it and keeps the ones a person decided to keep."""
    owned: set[str] = set()
    for r in sorted(revisions, key=lambda r: r["revision_num"]):
        fields = set(r["fields"] or [])
        if r["source"] == "staff_edit":
            owned |= fields
        elif r["source"] == "excel_merge":
            res = r.get("resolutions") or {}
            if isinstance(res, str):
                res = json.loads(res)
            owned -= {f for f in fields if res.get(f) != CURRENT}
            owned |= {f for f, choice in res.items() if choice == CURRENT}
    return owned


def plan(current: Mapping[str, Any], incoming: Mapping[str, Any], hand_edited: Iterable[str]) -> dict:
    """{unchanged: [field], auto: [{field, current, incoming}], conflicts: [{field, current, incoming}]}."""
    owned = set(hand_edited)
    out: dict = {"unchanged": [], "auto": [], "conflicts": []}
    for f in MERGE_FIELDS:
        if f not in incoming or _empty(incoming.get(f)):
            continue
        cur, inc = current.get(f), incoming.get(f)
        if _norm(cur) == _norm(inc):
            out["unchanged"].append(f)
            continue
        item = {"field": f, "current": cur, "incoming": inc, "hand_edited": f in owned}
        (out["conflicts"] if f in owned else out["auto"]).append(item)
    return out


def resolve(merge_plan: Mapping[str, Any], resolutions: Optional[Mapping[str, str]]) -> tuple[dict, dict]:
    """(changes {field: new value}, resolutions of the conflicts) for a plan and the person's choices. ValueError when a
    conflict has no valid choice or a choice names a field that is not a conflict."""
    resolutions = dict(resolutions or {})
    conflicts = {c["field"]: c for c in merge_plan.get("conflicts", [])}
    unknown = sorted(set(resolutions) - set(conflicts))
    if unknown:
        raise ValueError(f"not conflicts of this merge: {unknown}")
    missing = sorted(f for f in conflicts if resolutions.get(f) not in (INCOMING, CURRENT))
    if missing:
        raise ValueError(f"choose incoming or current for: {missing}")
    changes = {a["field"]: a["incoming"] for a in merge_plan.get("auto", [])}
    for f, c in conflicts.items():
        if resolutions[f] == INCOMING:
            changes[f] = c["incoming"]
    return changes, {f: resolutions[f] for f in conflicts}


def withhold(changes: Mapping[str, Any], blocked: Iterable[str]) -> tuple[dict, dict]:
    """(the changes without the ``blocked`` fields, {field: the sheet's value} of the fields held back). A field held
    back keeps the tour's value; the result names it so the person can hand it to whoever may enter it."""
    blocked = set(blocked)
    return ({f: v for f, v in changes.items() if f not in blocked},
            {f: v for f, v in changes.items() if f in blocked})


def stale_fields(current: Mapping[str, Any], before: Mapping[str, Any]) -> list[str]:
    """The fields of a draft whose value on the tour is no longer the one the draft was merged from (someone edited the
    tour since): publishing it would silently undo that edit."""
    return sorted(f for f, v in before.items() if _norm(current.get(f)) != _norm(v))


def db_value(field: str, value: Any) -> Any:
    """The value to bind for ``field`` (JSONB columns as JSON text)."""
    if field in JSONB_FIELDS and value is not None and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return value


__all__ = ["CURRENT", "INCOMING", "JSONB_FIELDS", "MERGE_FIELDS", "PRICE_FIELDS", "db_value", "hand_edited_fields",
           "plan", "resolve", "stale_fields", "withhold"]
