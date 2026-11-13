"""AA-1077 — the blog length contract: one owner for the numbers the writer prompt, the F4 gate, the extend repair
and the post-loop re-check use.

AA-741 phase 3 (S276, root docs/audits/2026-11-10-S276-aa741-phase3/ §4.5) found the two blog floors about 10 % apart,
in two units, in two files, linked only by a comment: F4 held a blog under 4,700 tag-stripped characters
(quality_gates._BLOG_MIN_CHARS) while the writer was asked for 900–1,300 words (prompts._BLOG_WORDS), and the comment
said F4 counts "tags included" (it strips them first). This module is now the one place:

- `floor_chars()` / `ceiling_chars()` — the F4 blog band (tag-stripped characters, AA-531: the origin repo's
  800–2,200 words; S279 lane L2: the settings cis.t9.blog_chars_min / _max, Platform › Settings, evidence-gated, read at
  call time). quality_gates reads them; so do the length trim (AA-984), the blog truth-cut refusal (AA-777), the
  extend repair (length_extend) and the re-check after the normalizer (`recheck_after_normalizer`).
- `writer_words()` — the writer's own word target, read from the prompt (`prompts._blog_words()`, `_BLOG_WORDS_MAX`;
  S279 L2: the floor is `target_words()`, rendered into the t9_write prompt at call time). The target sits ABOVE
  the floor on purpose (models undershoot: 25 of 28 phase 2
  pieces came in under 900 words); `tests/unit/test_aa1077_length_contract.py` pins target-floor ≥ the margin and
  max ≤ the ceiling, in characters at the measured `CHARS_PER_WORD`.
- `side()` / `violation_side()` — which side of the band a text or an F4 violation is on, so the hold reason code
  says `length_short` or `length_long` (gate_eval), never one `length` for both; old ledgers ("content is only N
  chars — effectively empty", "… far beyond any real channel example") read the same way.

S278 (12/11, 9 new blog pieces on 3 audit tenants after the T7 600-word seed floor): 5 of 9 were `length_short`, 3 of
them at the floor's edge (4,699 / 4,696 / 4,696 chars, 799–809 words) under a writer floor of 900 words (only 11 %
above F4), and the normalizer then cut up to 241 chars more (4,696 → 4,455). A systematic edge miss, not a seed one.
So the aim is ONE number above the floor, never the floor itself:

- `target_margin()` — the registry threshold `t9.blog_length_target_margin` (S279 L2: the setting
  cis.quality.t9_blog_length_target_margin, read at call time).
- `target_chars()` = floor × (1 + margin) (5,499 at 0.17) and `target_words()` (950, rounded up to ten) — the
  writer prompt's word floor (`prompts._blog_words()[0]`, rendered at call time; a value other than the seeded one
  puts the settings fingerprint on the ledger's prompt version, prompt_registry.version_for), the extend repair's
  target (`extend_target_chars()`) and the size the F4 short rewrite is told to reach (`short_rewrite_target()`).
- `floor_guard()` — the normalizer's rule for an OPTIONAL edit (the FAQ answer fit): an edit that leaves a blog under
  the floor and shorter than before is not made. Mandatory edits (the usage-policy cut, the final integrity pass)
  are made and the re-check holds the piece (truth and integrity before length). `NORMALIZER_STEPS` says which is
  which.
"""
from __future__ import annotations

import math
import re
from typing import Optional

from services.acp_shared.citation_tags import strip_citation_tags
from services.quality import registry as _qr



def floor_chars() -> int:
    """The F4 blog minimum, tag-stripped (4,700, ~810 words at CHARS_PER_WORD): the setting cis.t9.blog_chars_min."""
    from services.quality import settings_keys
    from shared import settings

    return int(settings.get(settings_keys.T9_BLOG_CHARS[0]))


def ceiling_chars() -> int:
    """The F4 blog maximum (13,000, ~2,240 words): the setting cis.t9.blog_chars_max."""
    from services.quality import settings_keys
    from shared import settings

    return int(settings.get(settings_keys.T9_BLOG_CHARS[1]))


# Median characters per word of the 28 phase 2 blog pieces (5.54–6.15; audit §4.5.2, `o1 SUM_LEN cpw`). Used only to
# turn a character deficit into a word count for the extend repair and the messages; never to measure.
CHARS_PER_WORD = 5.8


def target_margin() -> float:
    """S278 — how far above the F4 floor every length aim sits (writer floor, extend repair, F4 short rewrite). One
    registry threshold, read at call time (S279 L2: a setting)."""
    return float(_qr.threshold("t9.blog_length_target_margin"))


# The normalizer steps (service._normalize_final_text, in order) and whether the floor may stop their edit:
# "floor_guarded" = optional, not made when it leaves the blog under the floor and shorter (`floor_guard`);
# "mandatory" = made anyway (usage policy, duplicates / second CTA / fragments), the re-check then holds the piece.
# The stock-line cut is guarded inside itself (it re-runs F4 and refuses) and runs only for a channel with a spec.
NORMALIZER_STEPS = {"stock_cut": "floor_guarded", "authenticity_cut": "mandatory",
                    "faq_answer_fit": "floor_guarded", "final_integrity": "mandatory"}

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
    lo, hi = prompts._blog_words()
    return int(lo), int(hi), int(prompts._BLOG_WORDS_MAX)


def floor_words() -> int:
    return round(floor_chars() / CHARS_PER_WORD)


def target_chars() -> int:
    """The blog length aim in tag-stripped characters: the F4 floor plus the registry margin (5,499 at 0.17), never
    past the ceiling. Never the floor itself: a writer aiming at the floor lands on both sides of it (S278)."""
    return min(round(floor_chars() * (1 + target_margin())), ceiling_chars())


def target_words() -> int:
    """`target_chars()` in words at CHARS_PER_WORD, rounded UP to ten (950): the writer prompt's word floor."""
    return int(math.ceil(target_chars() / CHARS_PER_WORD / 10.0) * 10)


def extend_target_chars() -> int:
    """The size the extend repair adds up to: the length target (S278: was floor + 520 = 5,220, under the target)."""
    return target_chars()


# AA-1077 round 2 (S278 paid trial, 6610ac8d) — the short rewrite was told to lengthen "from the CONTENT SEED, its
# Place background and the FACTS lines" and wrote "The source notes name deer, birds, leopards…" (held F13_leak). No
# instruction text the model may echo names where facts come from without this rule next to it.
NO_SOURCE_MENTION = ('never mention where a fact comes from (no "the source", "the notes", "the seed", "the facts" or '
                     '"the itinerary says"): state the fact itself')


def short_rewrite_target() -> str:
    """S278 — what a blog's F4-short rewrite is told to reach (service._repair_feedback appends it to the violation, as
    the over-max side gets its cut target): the target, never the floor the violation names. Round 2: the length is
    measured AFTER the other fixes (a rewrite told to cut 6 sentences and lengthen came back shorter, S278 73e6a233),
    and no source label (NO_SOURCE_MENTION)."""
    most = writer_words()[2]
    return (f" — after every fix above, the piece must still be at least {target_chars():,} characters (about "
            f"{target_words():,} words, never more than {most:,}), not just at the floor: give the richest sections "
            "more depth with the facts you were given that the piece does not use yet, their place background and the "
            f"FAQ; never invented facts or filler; {NO_SOURCE_MENTION}")


# AA-1077 round 2 — a violation whose fix may delete its sentence ("say it is optional or cut it", "cut it or say
# something new", "rewrite or cut it", F1 "remove that number"). On a blog under the floor a cut leaves a hole the
# length check then holds on (S278 73e6a233: 6 F17 cuts + 1 F13_repeat cut, 3,957 → 3,546 chars).
_CUT_RE = re.compile(r"\bcut it\b|\bor cut\b|\bremove\b|\bdelete\b", re.I)
REPLACE_NOT_DELETE = ("; the piece is short: if you cut this sentence, fill its place in the same section with another "
                      "fact you were given, never leave the section shorter")


# The sentence-repair side of the same rule: the repair prompt lets a PROBLEM be fixed by returning "" (a delete).
KEEP_NOT_CUT = ("; the post is short: prefer the fix that keeps the sentence (say it is optional, or say only what "
                "is true) over deleting it")


def is_cut_violation(violation: str) -> bool:
    """True when the violation's fix may delete its sentence (a cut-type fix)."""
    return bool(_CUT_RE.search(str(violation or "")))


# F13_repeat's own fix wording (hygiene: "cut it or say something new").
_REPEAT_FIX = "say something new"


def keeps_not_cut(item: dict) -> bool:
    """True when a sentence-repair item gets KEEP_NOT_CUT: a cut-type problem that is not a repeat. Review (S278): a
    near-duplicate (F13_repeat) kept by a repair is filler, a paraphrase near_duplicates misses or a 'new' detail the
    repair prompt forbids; it is cut, and the expand step fills the section it shortened (length_extend._holes)."""
    problem = str(item.get("problem") or "")
    gates = item.get("gates") or [item.get("gate")]
    return is_cut_violation(problem) and _qr.F13_REPEAT not in gates and _REPEAT_FIX not in problem


def cut_projection(text: Optional[str], items: list[dict]) -> int:
    """The F4 length left if every cut-type item's sentence is deleted (repeats included: they are cut). A sentence
    repair is told to keep its sentences (KEEP_NOT_CUT) when this is under the floor, on any blog repair attempt, not
    only when the text is already short (S278 review: attempt 3 could cut a just-expanded blog back under it)."""
    gone = sum(len(it.get("sentence") or "") + 1 for it in items or [] if is_cut_violation(it.get("problem")))
    return measure(text) - gone


def floor_guard(before: Optional[str], after: Optional[str]) -> bool:
    """True when an OPTIONAL normalizer edit must not be made: it leaves the blog under the floor AND shorter than
    before (a blog in band is never taken under the floor; one already under it is never made shorter)."""
    a = measure(after)
    return a < floor_chars() and a < measure(before)


def side(length: int, lo: Optional[int] = None, hi: Optional[int] = None) -> Optional[str]:
    """'short' / 'long' when `length` is outside [lo, hi] (default: the blog band in force), else None."""
    lo = floor_chars() if lo is None else lo
    hi = ceiling_chars() if hi is None else hi
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
            f"{floor_chars():,} chars (about {floor_words():,} words); the writer aims for {lo:,}-{hi:,} words")


def long_message(length: int, n_words: int) -> str:
    """The blog F4 violation for a text over the ceiling; keeps 'far beyond' (service._F4_OVER_RE reads it)."""
    return (f"{CODE_LONG}: content is {length} chars (about {n_words:,} words) — far beyond the blog ceiling of "
            f"{ceiling_chars():,} chars")


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


__all__ = ["floor_chars", "ceiling_chars", "CHARS_PER_WORD", "target_margin", "NORMALIZER_STEPS",
           "SHORT", "LONG", "CODE_SHORT", "CODE_LONG", "measure", "words", "writer_words", "floor_words",
           "target_chars", "target_words", "extend_target_chars", "short_rewrite_target", "floor_guard", "side",
           "NO_SOURCE_MENTION", "REPLACE_NOT_DELETE", "KEEP_NOT_CUT", "is_cut_violation", "keeps_not_cut",
           "cut_projection",
           "short_message", "long_message", "violation_side", "entry_side", "code_for"]
