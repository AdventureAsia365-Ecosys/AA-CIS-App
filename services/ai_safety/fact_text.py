"""services.ai_safety.fact_text — the text layer of the Facts Entry instruction guard
(services/ai_safety/fact_guard.py): which characters are invisible (the Unicode Default_Ignorable_Code_Point set,
format characters, variation selectors, blank look-alikes), when one hides text (``hidden_text``), which ones are
removed from a kept value, and the matching copies the rules read (NFKC, look-alikes mapped, accents folded or kept,
digits read as letters). Split out of fact_guard.py to keep each file within the code-health size limit; the rules and
the decisions stay there. The character sets are security invariants, not policy values (no setting edits them)."""
from __future__ import annotations

import re
import unicodedata

# ── normalisation ────────────────────────────────────────────────────────────────────────────────────────────────

# security-invariant: look-alike letters (Cyrillic, Greek) mapped to the Latin letter they imitate, for matching only.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
    "ӏ": "l", "һ": "h", "ԛ": "q", "ԝ": "w", "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z",
    "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X", "ο": "o",
    "ι": "i", "ν": "v", "ѡ": "w", "đ": "d", "Đ": "D",
})
# The same map for the Vietnamese copy, which keeps đ.
_CONFUSABLES_KEEP_VI = {k: v for k, v in _CONFUSABLES.items() if k not in (ord("đ"), ord("Đ"))}

# security-invariant: an emoji subdivision flag (black flag, tag letters, cancel tag) is the one real use of tag
# characters; any other tag character is hidden text.
_FLAG_TAGS = re.compile("\U0001F3F4[\U000E0020-\U000E007E]{1,12}\U000E007F")
_TAG_CHAR = re.compile("[\U000E0000-\U000E007F]")
# security-invariant: the Unicode Default_Ignorable_Code_Point set (review round 4): characters a renderer draws as
# nothing. Category Cf covers only part of it; the Hangul fillers (Lo), the combining grapheme joiner, the Khmer
# inherent vowels and the Mongolian free variation selectors (Mn) are just as invisible and split words for a model
# while the matcher reads one word. Hardcoded so a Python / Unicode upgrade never narrows the set.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)
# security-invariant: blank look-alikes outside that set, read like an invisible character (U+2800 BRAILLE PATTERN
# BLANK draws as a space but is a symbol, so neither the sentence split nor the word rules see a space).
_BLANK_LOOKALIKES = frozenset({"\u2800"})
# The Khmer inherent vowels: deprecated, never real text (Unicode: "not recommended"); any one is hidden text.
_NEVER_REAL = frozenset({"\u17B4", "\u17B5"})
# Removed from every kept value, unless real in context (see _scrub): the default-ignorable set, the interlinear
# annotation controls (Cf) and the blank look-alikes.
_SCRUB_EXTRA = ((0xFFF9, 0xFFFB),)
_ZW_JOINERS = frozenset({"\u200C", "\u200D"})     # zero-width (non-)joiner: real in emoji and Indic / Arabic text
_SHY = "\u00AD"
_MONGOLIAN_FVS = frozenset({"\u180B", "\u180C", "\u180D", "\u180F"})


def _in_ranges(o: int, ranges) -> bool:
    return any(a <= o <= b for a, b in ranges)


def _default_ignorable(ch: str) -> bool:
    """A Default_Ignorable_Code_Point (the hardcoded ranges above)."""
    return _in_ranges(ord(ch), _DEFAULT_IGNORABLE)


# security-invariant: the letters and digits of the scripts the rules read (Latin with Vietnamese, Greek, Cyrillic). An
# invisible character between two of them splits or joins words for the matcher, never for a model (review round 3).
_WORDISH = re.compile("[0-9A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF\u1E00-\u1EFF]")
_KEYCAP = "\u20E3"


def _is_vs(ch: str) -> bool:
    """A variation selector (U+FE00-FE0F, or the ideographic ones U+E0100-E01EF)."""
    o = ord(ch)
    return 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF


def _invisible(ch: str) -> bool:
    """An invisible character: a Default_Ignorable_Code_Point (review round 4), any other format character (Unicode
    category Cf), a variation selector, or a blank look-alike (U+2800)."""
    return _is_vs(ch) or ch in _BLANK_LOOKALIKES or _default_ignorable(ch) or unicodedata.category(ch) == "Cf"


def _hidden_format(text: str) -> bool:
    """Whether ``text`` hides something with invisible characters (``hidden_text``, review rounds 3 and 4): an
    invisible character (the whole Default_Ignorable_Code_Point set: zero-width space / joiner / non-joiner, word
    joiner, Hangul fillers, combining grapheme joiner, Mongolian selectors, ...; and U+2800 BRAILLE PATTERN BLANK)
    between two letters or digits of the scripts the rules read ("must<ZWSP>say", "Note<U+3164>to"), a Khmer inherent
    vowel anywhere, or a variation selector that is not the one selector of an emoji, a symbol or a CJK character (a
    run of them carries bytes, "variation-selector smuggling"; a keycap digit "1<VS16><U+20E3>" is real). An emoji ZWJ
    sequence, a zero-width joiner next to a space and a zero-width space between Thai / Khmer / Lao / Burmese letters
    stay real text. One soft hyphen alone inside a word ("Accommo<SHY>dation") is not hidden text here: it is scrubbed,
    and :func:`_rules` drops the sentence only when reading it as a space reveals a rule ("must<SHY>say")."""
    t = unicodedata.normalize("NFKC", _FLAG_TAGS.sub("", text))
    i, n = 0, len(t)
    while i < n:
        if not _invisible(t[i]):
            i += 1
            continue
        j = i
        while j < n and _invisible(t[j]):
            j += 1
        run = t[i:j]
        if _NEVER_REAL.intersection(run):
            return True
        k = i - 1
        while k >= 0 and unicodedata.category(t[k]) == "Mn":          # combining marks belong to their letter
            k -= 1
        prev, nxt = (t[k] if k >= 0 else ""), (t[j] if j < n else "")
        selectors = sum(1 for ch in run if _is_vs(ch))
        if selectors:
            keycap = prev in "0123456789#*" and prev != "" and nxt == _KEYCAP
            if (selectors > 1 or not _is_vs(run[0]) or not prev or prev.isspace()
                    or (_WORDISH.match(prev) and not keycap)):
                return True
        if (selectors < len(run) and run != _SHY and prev and nxt and _WORDISH.match(prev)
                and _WORDISH.match(nxt)):
            return True
        i = j
    return False


def _scrubbed(text: str, i: int) -> bool:
    """Whether ``text[i]`` is removed from a kept value: a default-ignorable character, an interlinear annotation
    control or a blank look-alike, except where it is real text: a zero-width (non-)joiner (emoji, Indic and Arabic
    shaping), a variation selector (emoji, CJK; a smuggling run drops the fact as ``hidden_text`` instead), a tag
    character (an emoji flag; any other one drops the fact), a Mongolian free variation selector after a Mongolian
    letter."""
    ch = text[i]
    o = ord(ch)
    if not (ch in _BLANK_LOOKALIKES or _default_ignorable(ch) or _in_ranges(o, _SCRUB_EXTRA)):
        return False
    if ch in _ZW_JOINERS or _is_vs(ch) or 0xE0000 <= o <= 0xE007F:
        return False
    if ch in _MONGOLIAN_FVS and i > 0 and 0x1820 <= ord(text[i - 1]) <= 0x18AF:
        return False
    return True


def _scrub(text: str) -> str:
    """``text`` without the invisible characters that are not real text (the same object when it has none)."""
    gone = {i for i in range(len(text)) if _scrubbed(text, i)}
    if not gone:
        return text
    return "".join(ch for i, ch in enumerate(text) if i not in gone)


def _fold(text: str, *, accents: bool = False, spaced: bool = False) -> str:
    """The matching copy of ``text`` (case kept): NFKC, invisible characters (Cf, variation selectors) removed, or
    read as a space when ``spaced`` (the second copy: "Note<ZWSP>to<ZWSP>the<ZWSP>writer" reads as words),
    look-alikes mapped, whitespace collapsed, and accents folded (đ -> d) unless ``accents`` (the Vietnamese copy:
    tone marks kept, NFC)."""
    t = unicodedata.normalize("NFKC", text)
    t = "".join((" " if spaced else "") if _invisible(ch) else ch for ch in t)
    if accents:
        t = t.translate(_CONFUSABLES_KEEP_VI)
    else:
        t = t.translate(_CONFUSABLES)
        t = "".join(ch for ch in unicodedata.normalize("NFD", t) if unicodedata.category(ch) != "Mn")
    return " ".join(t.split())


# security-invariant: digits written for letters ("N0TE T0 THE WR1TER"), read back on a lower-case copy only when a
# digit touches a letter (prices, dates and times never change).
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"})
_LEET_DIGIT = re.compile(r"(?<=[a-z])[013457]|[013457](?=[a-z])")


def _unleet(low: str) -> str:
    """``low`` with each digit that touches a letter read as the letter it imitates."""
    return _LEET_DIGIT.sub(lambda m: m.group(0).translate(_LEET), low) if _LEET_DIGIT.search(low) else low


# The names fact_guard.py reads.
CONFUSABLES = _CONFUSABLES
FLAG_TAGS = _FLAG_TAGS
TAG_CHAR = _TAG_CHAR
invisible = _invisible
hidden_format = _hidden_format
scrub = _scrub
fold = _fold
unleet = _unleet

__all__ = ["CONFUSABLES", "FLAG_TAGS", "TAG_CHAR", "fold", "hidden_format", "invisible", "scrub", "unleet"]
