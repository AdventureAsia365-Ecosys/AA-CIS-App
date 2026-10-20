"""AA-912 — the one definition of a citation tag ([R:atom_id] / [F:fact_id]) the blog writer emits.

Every module that strips, skips or recognises a tag (quality_gates, hygiene, sentence_repair,
text_integrity, trip_facts, grounding) uses these patterns, so they cannot drift apart (they did:
four different tag bodies before AA-912).

Rules:
- A tag body never holds a bracket or a newline (real ids: `atom_83c773a60a`, `fact_9`, `f-12`).
  Excluding `[` keeps every scan linear: a failed attempt stops at the next `[` instead of running
  to the end of the text (CodeQL py/polynomial-redos).
- Stripping also removes a DANGLING opener the writer left unclosed (`[R:atom_1` with no `]` before
  the next bracket, newline or the end): the `[R:` / `[F:` and the id characters right after it, and
  nothing more. So a malformed tag never deletes real prose (the pre-AA-912 pattern deleted
  everything up to the next `]`) and never leaves a raw `[R:` fragment in tenant text.

Pure: no I/O.
"""
from __future__ import annotations

import re
from typing import Optional

# A well-formed tag, for composing bigger patterns (sentence ends, separators).
TAG = r"\[(?:R|F):[^\[\]\n]+\]"
# The same, compiled, with the id(s) as group 1.
TAG_RE = re.compile(r"\[(?:R|F):([^\[\]\n]+)\]")
# One whitespace char before it, then a whole tag (the empty '[R:]' too) or a dangling opener and its id.
_STRIP_RE = re.compile(r"\s?\[(?:R|F):(?:[^\[\]\n]*\]|[\w-]*)")
_TAG_FULL_RE = re.compile(TAG)


def strip_citation_tags(text: Optional[str], repl: str = "") -> str:
    """`text` with every citation tag and dangling tag opener (and one whitespace char before each)
    replaced by `repl`. Linear time."""
    return _STRIP_RE.sub(repl, text or "")


def trailing_tags(text: Optional[str]) -> str:
    """The run of citation tags (each with at most one whitespace char before it) plus trailing
    whitespace that ends `text` — the match of `(?:\\s?TAG)+\\s*$` — or '' when it does not end on a
    tag. One backward pass (the regex search was O(n^2) on '[R:a]' * k + 'x': 100k chars ~11 s)."""
    text = text or ""
    i = len(text)
    while i and text[i - 1].isspace():
        i -= 1
    start = None
    while i and text[i - 1] == "]":
        lb = text.rfind("[", 0, i - 1)
        if lb < 0 or not _TAG_FULL_RE.fullmatch(text, lb, i):
            break
        i = lb - 1 if lb and text[lb - 1].isspace() else lb
        start = i
    return "" if start is None else text[start:]


__all__ = ["TAG", "TAG_RE", "strip_citation_tags", "trailing_tags"]
