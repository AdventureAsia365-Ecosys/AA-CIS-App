"""AA-1077 — the blog length contract: one owner for the numbers the writer prompt, the F4 gate, the extend repair
and the post-loop re-check use.

AA-741 phase 3 (S276, root docs/audits/2026-11-10-S276-aa741-phase3/ §4.5) found the two blog floors about 10 % apart,
in two units, in two files, linked only by a comment: F4 held a blog under 4,700 tag-stripped characters
(quality_gates._BLOG_MIN_CHARS) while the writer was asked for 900–1,300 words (prompts._BLOG_WORDS), and the comment
said F4 counts "tags included" (it strips them first). This module is now the one place:

- `BLOG_FLOOR_CHARS` / `BLOG_CEILING_CHARS` — the F4 blog band (tag-stripped characters, AA-531: the origin repo's
  800–2,200 words). quality_gates reads them; so do the length trim (AA-984), the blog truth-cut refusal (AA-777),
  the extend repair (length_extend) and the re-check after the normalizer (`recheck_after_normalizer`).
- `writer_words()` — the writer's own word target, read from the prompt's registered symbols (`prompts._BLOG_WORDS`,
  `_BLOG_WORDS_MAX`: hashed in the t9_write prompt manifest, so they stay the literal; reading them here instead of
  restating them keeps one number). The target sits ABOVE the floor on purpose (models undershoot: 25 of 28 phase 2
  pieces came in under 900 words); `tests/unit/test_aa1077_length_contract.py` pins target-floor ≥ the margin and
  max ≤ the ceiling, in characters at the measured `CHARS_PER_WORD`.
- `side()` / `violation_side()` — which side of the band a text or an F4 violation is on, so the hold reason code
  says `length_short` or `length_long` (gate_eval), never one `length` for both; old ledgers ("content is only N
  chars — effectively empty", "… far beyond any real channel example") read the same way.
"""
from __future__ import annotations

import re
from typing import Optional

from services.acp_shared.citation_tags import strip_citation_tags

BLOG_FLOOR_CHARS = 4700     # ~810 words at CHARS_PER_WORD; the F4 blog minimum (tag-stripped)
BLOG_CEILING_CHARS = 13000  # ~2,240 words; the F4 blog maximum
# Median characters per word of the 28 phase 2 blog pieces (5.54–6.15; audit §4.5.2, `o1 SUM_LEN cpw`). Used only to
# turn a character deficit into a word count for the extend repair and the messages; never to measure.
CHARS_PER_WORD = 5.8
# What the extend repair aims for: the writer's own floor in characters (900 words ≈ 5,220), so the post-loop
# normalizer (FAQ answer fit, final integrity: 192–557 chars cut on the phase 2 holds) still leaves it over the floor.
EXTEND_MARGIN_CHARS = 520
# The minimum distance between the writer's word floor (in characters) and the F4 floor; pinned by the test.
TARGET_MARGIN_MIN = 0.08

SHORT, LONG = "short", "long"
CODE_SHORT, CODE_LONG = "length_short", "length_long"
_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ0-9]+(?:['’-][A-Za-zÀ-ɏ0-9]+)*")


def measure(text: Optional[str]) -> int:
    """The length F4 reads: the tag-stripped text (quality_gates.run_quality_gates `length_check_text`)."""
    return len(strip_citation_tags(text or ""))


def words(text: Optional[str]) -> int:
    """Words of the tag-stripped text (letters / digits with inner apostrophes or hyphens)."""
    return len(_WORD_RE.findall(strip_citation_tags(text or "")))


def writer_words() -> tuple[int, int, int]:
    """(floor, target top, never more) words the writer prompt states, read from its registered symbols."""
    from services.acp_content_writing import prompts  # lazy: prompts imports half the package
    lo, hi = prompts._BLOG_WORDS
    return int(lo), int(hi), int(prompts._BLOG_WORDS_MAX)


def floor_words() -> int:
    return round(BLOG_FLOOR_CHARS / CHARS_PER_WORD)


def extend_target_chars() -> int:
    """The size the extend repair adds up to: the F4 floor plus the normalizer margin, never past the ceiling."""
    return min(BLOG_FLOOR_CHARS + EXTEND_MARGIN_CHARS, BLOG_CEILING_CHARS)


def side(length: int, lo: int = BLOG_FLOOR_CHARS, hi: int = BLOG_CEILING_CHARS) -> Optional[str]:
    """'short' / 'long' when `length` is outside [lo, hi], else None."""
    if length < lo:
        return SHORT
    if length > hi:
        return LONG
    return None


def short_message(length: int, n_words: int) -> str:
    """The blog F4 violation for a text under the floor. 'only N chars' (digits, no separator) stays readable by the
    audit scripts' regex; the words and the writer's target say how far off it is (a 650-word article is no longer
    called 'effectively empty')."""
    lo, hi, _ = writer_words()
    return (f"{CODE_SHORT}: content is only {length} chars (about {n_words:,} words), under the blog floor of "
            f"{BLOG_FLOOR_CHARS:,} chars (about {floor_words():,} words); the writer aims for {lo:,}-{hi:,} words")


def long_message(length: int, n_words: int) -> str:
    """The blog F4 violation for a text over the ceiling; keeps 'far beyond' (service._F4_OVER_RE reads it)."""
    return (f"{CODE_LONG}: content is {length} chars (about {n_words:,} words) — far beyond the blog ceiling of "
            f"{BLOG_CEILING_CHARS:,} chars")


def violation_side(violation: str) -> Optional[str]:
    """The side an F4_extreme_length violation reports, for new and old wording alike: 'length_short: …' / '… only
    N chars — effectively empty' → 'short'; 'length_long: …' / '… far beyond …' → 'long'; anything else None."""
    v = str(violation or "")
    if v.startswith(CODE_SHORT) or (" only " in v and " chars" in v):
        return SHORT
    if v.startswith(CODE_LONG) or "far beyond" in v:
        return LONG
    return None


def entry_side(entry: Optional[dict]) -> Optional[str]:
    """The side of a FAILED F4_extreme_length ledger entry (its first readable violation), else None."""
    if not isinstance(entry, dict) or entry.get("passed"):
        return None
    for v in entry.get("violations") or []:
        s = violation_side(v)
        if s:
            return s
    return None


def code_for(entry: Optional[dict]) -> Optional[str]:
    """'length_short' / 'length_long' for a failed F4 entry whose side reads, else None (the caller keeps the legacy
    'length' code)."""
    s = entry_side(entry)
    return CODE_SHORT if s == SHORT else CODE_LONG if s == LONG else None


__all__ = ["BLOG_FLOOR_CHARS", "BLOG_CEILING_CHARS", "CHARS_PER_WORD", "EXTEND_MARGIN_CHARS", "TARGET_MARGIN_MIN",
           "SHORT", "LONG", "CODE_SHORT", "CODE_LONG", "measure", "words", "writer_words", "floor_words",
           "extend_target_chars", "side", "short_message", "long_message", "violation_side", "entry_side",
           "code_for"]
