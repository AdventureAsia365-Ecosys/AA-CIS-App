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

_PRICE_RE = re.compile(r"(?:[$€£¥₫]|\b(?:USD|EUR|VND|THB|US\$)\s?)\s?\d[\d,.]*(?:\s?(?:k|K|m|M))?|\b\d[\d,.]*\s?"
                       r"(?:USD|EUR|VND|THB|dollars?|euros?)\b", re.I)
_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)|\b\d{1,2}:\d{2}\b|\b\d{1,2}h\d{2}\b", re.I)
_NUM_RE = re.compile(r"\b\d[\d,.]*(?:st|nd|rd|th)?\b")
_WORD_RE = re.compile(r"[A-Za-z][\w'’-]*")
_TOKEN_RE = re.compile(r"\[(?:N|PRICE|TIME|URL|NAME)\]")
_RUNS_RE = re.compile(r"\[NAME\](?:[\s-]+(?:of\s+|the\s+|de\s+)?\[NAME\])+")
# Capitalised words that are ordinary English, not names, when they appear mid-sentence.
_COMMON = frozenset("""I I'm I've I'll I'd Day Days Mr Mrs Ms Dr AM PM OK TV""".split())


def _is_sentence_start(text: str, idx: int) -> bool:
    """True when ``text[:idx]`` is empty or ends with an end mark / colon / semicolon + whitespace, a newline +
    optional whitespace, or an opening quote / parenthesis + optional whitespace.

    Same result as re.search(r'(?:^|[.!?:;]\\s+|\\n\\s*|["“(]\\s*)$', text[:idx]), as a backward scan over the
    trailing whitespace only: the regex search restarted at every offset and ran O(n^2) on a run of newlines
    (CodeQL py/polynomial-redos, S251). str.isspace is exactly the regex \\s."""
    idx = min(idx, len(text)) if idx >= 0 else max(0, len(text) + idx)   # text[:idx] slice semantics
    if idx == 0:
        return True
    w = idx
    while w > 0 and text[w - 1].isspace():
        w -= 1
    if "\n" in text[w:idx]:
        return True
    if w == 0:
        return False
    return (text[w - 1] in ".!?:;" and w < idx) or text[w - 1] in "\"“("


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"          # exactly the regex \w on str


def _url_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every match of re.finditer(r"https?://\\S+|www\\.\\S+|\\S+@\\S+\\.\\w+", text), in one
    linear pass per whitespace-free run (CodeQL py/polynomial-redos, S251: the e-mail branch backtracked O(n^2)
    on '!' * n and worse on '!@' * n).

    Every match lies inside one run. At a position p of a run [p0, r): a 'http://' / 'https://' / 'www.' prefix
    with at least one char after it in the run matches to the run's end (the first two branches). Else the e-mail
    branch matches iff an '@' sits in [p + 1, D - 2], where D is the run's rightmost '.' followed by a word char
    (greedy backtracking picks that D and the rightmost such '@'), and ends after the word chars that follow D.
    After an e-mail match the scan goes on at its end, past D, where only a prefix can match again."""
    spans: list[tuple[int, int]] = []
    n, i = len(text), 0
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        r = i
        while r < n and not text[r].isspace():
            r += 1
        dot = r - 2
        while dot >= i and not (text[dot] == "." and _is_word_char(text[dot + 1])):
            dot -= 1
        at = text.rfind("@", i, dot - 1) if dot - 1 > i else -1   # an '@' in [i, dot - 2]
        p = i
        while p < r:
            if text[p] in "hw" and any(text.startswith(pre, p) and p + len(pre) < r
                                       for pre in ("https://", "http://", "www.")):
                spans.append((p, r))
                break
            if at > p:
                e = dot + 1
                while e < r and _is_word_char(text[e]):
                    e += 1
                spans.append((p, e))
                p = e
                continue
            p += 1
        i = r
    return spans


def _mask_urls(text: str) -> str:
    out, last = [], 0
    for start, end in _url_spans(text):
        out.append(text[last:start])
        out.append("[URL]")
        last = end
    out.append(text[last:])
    return "".join(out)


def mask(text: Optional[str]) -> str:
    """The text with its specifics replaced by tokens (see the comment below for names). Idempotent on its own
    output."""
    if not text:
        return ""
    t = _mask_urls(text)
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
