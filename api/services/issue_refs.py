"""User-visible text hygiene for free text that comes from outside sources (CloudWatch alarm descriptions, ops check
messages, ECS rollout reasons, job errors): internal tracker references are removed and long text is cut at a word
boundary, never mid-word.

User-visible admin text never shows tracker IDs (AA-994): "AA-942 (CRM-14, V1): ..." reads "...". The Terraform
descriptions keep them for engineers. Only the tracker prefixes below are IDs; runbook and event codes ("CIS-08",
"EVENT-0087") and product names ("AA-Booking", "AA-CIS") stay. A version tag ("V1") goes only inside a parenthesised
group that also names a tracker ID. Pure, no I/O.
"""
from __future__ import annotations

import re

TRACKERS = ("AA", "CRM", "KAN", "PR", "CON")
_ID = r"(?:" + "|".join(TRACKERS) + r")-\d+"
_TAG = r"V\d+(?:\.\d+)*"
_ID_RE = re.compile(rf"^{_ID}$")
_PART = rf"(?:{_ID}|{_TAG})"
_IDS = rf"(?<![A-Za-z0-9-]){_ID}(?:\s*[,/]\s*{_ID})*"

# "AA-942: ...", "AA-942 (CRM-14, V1): ...", "AA-942 — ...", "AA-1, AA-2: ..."
_PREFIX = re.compile(rf"^\s*{_IDS}(?:\s*\([^)]*\))?(?:\s*:|\s+[—–-])\s*")
# "(CRM-14, V1)", "(AA-969)": a group made only of IDs and version tags
_GROUP = re.compile(rf"\s*\(\s*({_PART}(?:\s*[,;/]\s*{_PART})*)\s*\)")
# a lone reference: "... AA-703." / "(latency SLO, AA-969)"
_REF = re.compile(rf"\s*(?<![A-Za-z0-9-]){_ID}(?![\w-])")
_TIDY = (
    (re.compile(r"\(\s*[,;/]\s*"), "("),
    (re.compile(r"\s*[,;/]\s*\)"), ")"),
    (re.compile(r"\(\s*\)"), ""),
    (re.compile(r",\s*,"), ","),
    (re.compile(r",\s*\."), "."),
    (re.compile(r"\.\s*,\s*"), ". "),
    (re.compile(r"\s+([.,;:)])"), r"\1"),
    (re.compile(r"\.{2,}"), "."),
    (re.compile(r"\s{2,}"), " "),
)
ELLIPSIS = "…"


def _drop_group(m: re.Match) -> str:
    parts = re.split(r"\s*[,;/]\s*", m.group(1))
    return "" if any(_ID_RE.match(p) for p in parts) else m.group(0)


def strip_issue_refs(text: str | None) -> str:
    """The text without internal tracker references; punctuation tidied, a sentence that started after a removed
    "AA-939: " prefix gets its capital back (not a resource name such as "aa-cis-dev ...")."""
    raw = text or ""
    head = _PREFIX.sub("", raw)
    out = _REF.sub("", _GROUP.sub(_drop_group, head))
    for pattern, repl in _TIDY:
        out = pattern.sub(repl, out)
    out = out.strip()
    first = out.split(" ", 1)[0]
    if head != raw and out and "-" not in first:
        out = out[:1].upper() + out[1:]
    return out


def clip_words(text: str | None, limit: int) -> str:
    """At most ``limit`` characters, cut at the last word boundary with an ellipsis (never mid-word); the page clamps
    long text to a few lines with a "Show more" toggle, so ``limit`` is only a storage bound."""
    s = (text or "").strip()
    if len(s) <= limit:
        return s
    cut = s[: max(limit - 1, 1)]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-—–") + ELLIPSIS


def clean(text: str | None, limit: int = 1000) -> str | None:
    """Outside free text as the page shows it: no tracker IDs, cut at a word boundary; None when nothing is left."""
    out = clip_words(strip_issue_refs(text), limit)
    return out or None
