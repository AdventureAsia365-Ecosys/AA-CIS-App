"""AA-1049 (tech lead decision S277, root ADR 0020 D8) — the S1 Review Queue row of the elephant-riding policy review.

The policy (business owner, 08/10/2026): a tour whose product offers elephant riding is kept as it is and a person
reviews it. Tech lead decision S277: a Master that offers elephant riding is not on sale until a person reviews it;
it goes to the Review Queue and only an approval puts it back on sale.

A policy review row is an ordinary ``silver_aa_internal.review_queue`` row (no new column) marked with:

- ``escalate_detail``: one item per detector hit (``elephant_policy.scan``), ``check_id`` = ``policy:elephant_riding``
  (the reason id the T2 policy hold already writes), so SQL finds the row with ``@>`` (``row_is_policy_sql``). The
  evidence is codes only: where the ride was found (``version`` / ``master`` / ``source``) and which field; never
  the matched words or any page text (``source_span`` is NULL);
- ``failure_summary``: starts with ``SUMMARY_PREFIX``.

An S1 row is told apart from a T2 tenant row by ``generated_content_id IS NOT NULL`` (a T2 row carries
``tenant_tour_version_id`` instead).

Pure: no DB, no I/O. The DB side (scan reads, the hold, the publish guard) is ``services/tour_lifecycle/policy.py``.
"""
from __future__ import annotations

import json
from typing import Iterable

from services.quality import elephant_policy

CHECK_ID = f"policy:{elephant_policy.POLICY_HOLD}"
# The jsonb containment probe (one literal, so a test can pin every SQL that reads it).
MARKER_JSON = json.dumps([{"check_id": CHECK_ID}])
SUMMARY_PREFIX = "POLICY_REVIEW elephant_riding: a person reviews this tour before it is on sale"
# The Review Queue filter value (GET /admin/review-queue?policy=...).
FILTER_VALUE = elephant_policy.POLICY_HOLD
_MAX_ITEMS = 10


def row_is_policy_sql(alias: str = "rq") -> str:
    """SQL boolean: the review row ``alias`` is an S1 elephant-riding policy review (never NULL)."""
    return (f"({alias}.generated_content_id IS NOT NULL"
            f" AND COALESCE({alias}.escalate_detail @> '{MARKER_JSON}'::jsonb, false))")


def detail(hits: Iterable[dict]) -> list[dict]:
    """``escalate_detail`` of a policy review row: one item per (where, field) of the detector hits, codes only
    (``where`` = ``version`` / ``master`` / ``source``). At least one item, so the row always carries the marker."""
    out = []
    seen = set()
    for h in hits:
        w, field = str(h.get("where") or "version"), str(h.get("field") or "")
        if (w, field) in seen:
            continue
        seen.add((w, field))
        out.append({"check_id": CHECK_ID, "where": w, "field": field or None,
                    "description": f"Elephant riding in the {w} {field}".strip()
                                   + ": kept as it is for a person to review (policy, no rewrite)",
                    "source_span": None, "suggested_fix": None})
        if len(out) >= _MAX_ITEMS:
            break
    if not out:
        out.append({"check_id": CHECK_ID, "where": None, "field": None,
                    "description": "Held by the elephant-riding policy: a person reviews it",
                    "source_span": None, "suggested_fix": None})
    return out


def summary(base: str | None = None) -> str:
    """The row's ``failure_summary``: the policy prefix, then the usual reason text."""
    base = (base or "").strip()
    return f"{SUMMARY_PREFIX}; {base}" if base else SUMMARY_PREFIX


def _items(escalate_detail) -> list:
    value = escalate_detail
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    return value if isinstance(value, list) else []


def is_policy_detail(escalate_detail) -> bool:
    """True when a row's ``escalate_detail`` (a list, or its JSON text: this app's connections have no jsonb codec)
    carries the marker."""
    return any(isinstance(i, dict) and i.get("check_id") == CHECK_ID for i in _items(escalate_detail))


def evidence(escalate_detail) -> list[dict]:
    """[{where, field}] of a policy row (the admin badge's tooltip): codes only."""
    return [{"where": i.get("where"), "field": i.get("field")} for i in _items(escalate_detail)
            if isinstance(i, dict) and i.get("check_id") == CHECK_ID and i.get("field")]


__all__ = ["CHECK_ID", "FILTER_VALUE", "MARKER_JSON", "SUMMARY_PREFIX", "detail", "evidence", "is_policy_detail",
           "row_is_policy_sql", "summary"]
