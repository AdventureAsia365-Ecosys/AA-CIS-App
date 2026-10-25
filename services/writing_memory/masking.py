"""AA-917 WM-12 — entity masking and change excerpts for the rule drafter (app ADR 0010 decision 8, D9).

The drafter only ever sees masked text: numbers, prices, times, links and capitalised names (places, operators, brand
names) become `[N]`, `[PRICE]`, `[TIME]`, `[URL]`, `[NAME]`. So a platform rule cannot carry a tenant's facts or
names, and the rule text the model writes is in the same masked vocabulary the PREDICT check matches against.

`excerpt(before, after)` keeps only the changed words of an edit with a little context: a tenant's T9 edit stores the
whole piece, and the drafter needs the change, not 6 000 characters of blog.
"""
from __future__ import annotations

import difflib
import re
from typing import Optional

from services.writing_memory.config import EXCERPT_CHARS

_URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\w+")
_PRICE_RE = re.compile(r"(?:[$€£¥₫]|\b(?:USD|EUR|VND|THB|US\$)\s?)\s?\d[\d,.]*(?:\s?(?:k|K|m|M))?|\b\d[\d,.]*\s?"
                       r"(?:USD|EUR|VND|THB|dollars?|euros?)\b", re.I)
_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)|\b\d{1,2}:\d{2}\b|\b\d{1,2}h\d{2}\b", re.I)
_NUM_RE = re.compile(r"\b\d[\d,.]*(?:st|nd|rd|th)?\b")
_WORD_RE = re.compile(r"[A-Za-z][\w'’-]*")
_SENT_END_RE = re.compile(r"(?:^|[.!?:;]\s+|\n\s*|[\"“(]\s*)$")
_TOKEN_RE = re.compile(r"\[(?:N|PRICE|TIME|URL|NAME)\]")
_RUNS_RE = re.compile(r"\[NAME\](?:[\s-]+(?:of\s+|the\s+|de\s+)?\[NAME\])+")
# Capitalised words that are ordinary English, not names, when they appear mid-sentence.
_COMMON = frozenset("""I I'm I've I'll I'd Day Days Mr Mrs Ms Dr AM PM OK TV""".split())


def _is_sentence_start(text: str, idx: int) -> bool:
    return bool(_SENT_END_RE.search(text[:idx]))


def mask(text: Optional[str]) -> str:
    """The text with its specifics replaced by tokens (see the comment below for names). Idempotent on its own
    output."""
    if not text:
        return ""
    t = _URL_RE.sub("[URL]", text)
    t = _PRICE_RE.sub("[PRICE]", t)
    t = _TIME_RE.sub("[TIME]", t)
    t = _NUM_RE.sub("[N]", t)

    # A capitalised word mid-sentence is a name; a sentence-initial one only when the same word is also capitalised
    # mid-sentence somewhere in the text (a name that starts one sentence and recurs). A single sentence-initial name
    # stays (no dictionary to tell "Kyoto" from "Discover"): the 5-gram and brand-name lints are the backstop.
    names_mid = {m.group(0) for m in _WORD_RE.finditer(t)
                 if m.group(0)[0].isupper() and m.group(0) not in _COMMON and not _is_sentence_start(t, m.start())}
    out, last = [], 0
    for m in _WORD_RE.finditer(t):
        word = m.group(0)
        if not word[0].isupper() or word in _COMMON:
            continue
        if _is_sentence_start(t, m.start()) and word not in names_mid:
            continue
        if t[max(0, m.start() - 1):m.start()] == "[":       # a token we wrote ([NAME], [N] …)
            continue
        out.append(t[last:m.start()])
        out.append("[NAME]")
        last = m.end()
    out.append(t[last:])
    return _RUNS_RE.sub("[NAME]", "".join(out))


def tokens(text: str) -> list[str]:
    """Lower-cased words and mask tokens, in order (the unit of the change excerpt and of phrase matching)."""
    found = re.findall(r"\[(?:N|PRICE|TIME|URL|NAME)\]|[A-Za-z0-9][A-Za-z0-9'’-]*", text or "")
    return [w if w.startswith("[") else w.lower() for w in found]


def _join(words: list[str], limit: int) -> str:
    s = " ".join(words)
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + " …"


def excerpt(before: Optional[str], after: Optional[str], *, context: int = 8,
            limit: int = EXCERPT_CHARS) -> tuple[str, str]:
    """(before, after) cut down to the changed spans plus `context` words around each, masked, at most `limit`
    characters per side. A cut keeps after = ''. Unchanged text gives ('', '')."""
    b, a = mask(before).split(), mask(after).split()
    if not b and not a:
        return "", ""
    sm = difflib.SequenceMatcher(a=b, b=a, autojunk=False)
    keep_b, keep_a = set(), set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        keep_b.update(range(max(0, i1 - context), min(len(b), i2 + context)))
        keep_a.update(range(max(0, j1 - context), min(len(a), j2 + context)))
    if not keep_b and not keep_a:
        return "", ""

    def pick(words: list[str], keep: set[int]) -> str:
        parts, prev = [], None
        for i in sorted(keep):
            if prev is not None and i != prev + 1:
                parts.append("…")
            parts.append(words[i])
            prev = i
        return _join(parts, limit)

    return pick(b, keep_b), pick(a, keep_a)


def removed_phrases(before: Optional[str], after: Optional[str], *, max_n: int = 3) -> set[str]:
    """The 1-3 word phrases (masked, lower case) of `before` that the edit removed: in a deleted / replaced span and
    no longer anywhere in `after`. Single stopwords and bare mask tokens are not phrases."""
    from services.writing_memory.lint import STOPWORDS   # one stopword list for phrases and the lint
    b, a = tokens(mask(before)), tokens(mask(after))
    if not b:
        return set()
    sm = difflib.SequenceMatcher(a=b, b=a, autojunk=False)
    after_text = " " + " ".join(a) + " "
    out: set[str] = set()
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag not in ("delete", "replace"):
            continue
        span = b[i1:i2]
        for n in range(1, max_n + 1):
            for k in range(0, len(span) - n + 1):
                gram = span[k:k + n]
                if gram[0] in STOPWORDS or gram[-1] in STOPWORDS or all(_TOKEN_RE.fullmatch(g) for g in gram):
                    continue
                if n == 1 and len(gram[0]) < 4:
                    continue
                phrase = " ".join(gram)
                if f" {phrase} " not in after_text:
                    out.add(phrase)
    return out


def contains_phrase(text: Optional[str], phrase: str) -> bool:
    """Whether the masked, tokenised `text` holds `phrase` (masked the same way) as whole words."""
    want = tokens(mask(phrase))
    if not want:
        return False
    have = tokens(mask(text))
    n = len(want)
    return any(have[i:i + n] == want for i in range(len(have) - n + 1))


__all__ = ["contains_phrase", "excerpt", "mask", "removed_phrases", "tokens"]
