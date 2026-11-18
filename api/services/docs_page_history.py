"""AA-1132 — the page history and "needs review" fields of a docs manifest entry (api/services/docs_reader.py).

The root repo docs bundle (tools/docs/build_docs_reader.py rule 13) gives each entry ``history``: the newest commits
that touched the file, newest first, at most HISTORY_ITEMS, each {sha, date, subject, pr}. ``subject`` is already
cleaned by the builder (no "(#123)" suffix, no issue keys, IDs redacted); a line may also be a redacted one with no sha
or PR (a change to pages the reader may not open), so ``sha`` and ``pr`` are optional here. The root docs sync engine
may also mark a page ``needs_review`` (a mapped code dependency changed after the page's last change) with an optional
``needs_review_reason``.

Both are optional: an older bundle has neither, and the reader then gets ``history`` [] and ``needs_review`` null. A
value of the wrong shape is dropped (an item) or becomes null (a field), never an error. Like ``known_issue``, text a
reader sees has its 12-digit runs masked again here (defence in depth behind the builder's guard). The caps below are
shape checks that mirror the builder's output sizes, not runtime policy.
"""
from __future__ import annotations

import re
from typing import Any, Optional

HISTORY_ITEMS = 10          # technical-constant: the builder's HISTORY_MAX (root build_docs_reader.py rule 13)
SUBJECT_CHARS = 160         # technical-constant: the builder's SUBJECT_MAX
REASON_CHARS = 300          # technical-constant: a reason is one short sentence, like known_issue
PR_TOP = 999_999            # technical-constant: the builder's "(#123)" pattern takes 1-6 digits

_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
ACCOUNT_ID_MARKER = "[redacted-account-id]"
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T[0-9:.+-]{1,30}Z?)?$")


def _text(value: Any, limit: int) -> Optional[str]:
    """One line of reader text: whitespace collapsed, 12-digit runs masked, cut at `limit`; None when not a string."""
    if not isinstance(value, str):
        return None
    return _ACCOUNT_ID.sub(ACCOUNT_ID_MARKER, " ".join(value.split()))[:limit]


def _item(raw: Any) -> Optional[dict]:
    """One history item in its shape, or None (no usable date or subject)."""
    if not isinstance(raw, dict):
        return None
    date, subject = raw.get("date"), _text(raw.get("subject"), SUBJECT_CHARS)
    if not (isinstance(date, str) and _ISO_DATE.match(date)) or subject is None:
        return None
    sha, pr = raw.get("sha"), raw.get("pr")
    return {"sha": sha if isinstance(sha, str) and _GIT_SHA.match(sha) else None,
            "date": date,
            "subject": subject,
            "pr": pr if isinstance(pr, int) and not isinstance(pr, bool) and 0 < pr <= PR_TOP else None}


def clean_history(raw: Any) -> list[dict]:
    """The entry's ``history`` items that have the right shape, in the bundle's order, at most HISTORY_ITEMS."""
    items = (_item(r) for r in (raw if isinstance(raw, list) else []))
    return [i for i in items if i is not None][:HISTORY_ITEMS]


def _review_object(obj: dict) -> tuple[Optional[bool], Optional[str]]:
    """The root bundle's object form (rule 15, root tools/docs/docs_freshness.py): {since, count, changes: [{entry,
    repo, sha, date, files}]}: flagged when it names at least one change; the reason names how many changes since
    when and the doc-map entries (repos) they came through."""
    raw_changes = obj.get("changes")
    changes = [c for c in raw_changes if isinstance(c, dict)] if isinstance(raw_changes, list) else []
    count = obj.get("count")
    count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else len(changes)
    if count == 0:
        return False, None
    since = obj.get("since") if isinstance(obj.get("since"), str) and _ISO_DATE.match(obj["since"]) else None
    names = []
    for c in changes:
        name = _text(c.get("entry"), 60)
        repo = _text(c.get("repo"), 40)
        if name and f"{name} ({repo})" not in names:
            names.append(f"{name} ({repo})" if repo else name)
    head = f"{count} code change{'s' if count != 1 else ''}" + (f" since {since[:10]}" if since else "")
    return True, _text(head + (": " + ", ".join(names) if names else ""), REASON_CHARS)


def clean_review(raw: dict) -> tuple[Optional[bool], Optional[str]]:
    """(``needs_review``, ``needs_review_reason``): a boolean flag with its optional reason text, or the root bundle's
    object form (see :func:`_review_object`); any other shape is null. The reason only on a flagged page."""
    flag = raw.get("needs_review")
    if isinstance(flag, dict):
        return _review_object(flag)
    if not isinstance(flag, bool):
        return None, None
    reason = _text(raw.get("needs_review_reason"), REASON_CHARS) if flag else None
    return flag, reason or None
