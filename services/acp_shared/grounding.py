"""
services.acp_shared.grounding — DET entailment check shared by S1-from-atom
(check_grounding, AA-306/AA-325) and N7 Produce (gate_grounding, AA-298 P0-1).

ADR-2026-033 (amends ADR-2026-029): the originally specified whole-sentence
token-overlap ratio (~0.3 threshold) was tested against 107 real (sentence,
atom) pairs pulled from a live AA-325 production audit and could not separate
real violations from real good content at any threshold or formula (raw
ratio, Jaccard, atom-coverage, symmetric-min all tested) — a ceiling of the
metric on real content, not a tuning problem. Root cause: sentences that
synthesize multiple cited atoms score low against any single atom (false
positives), while real fabrications are usually one embellished phrase inside
an otherwise well-grounded sentence, so whole-sentence overlap stays
misleadingly high (false negatives).

This module instead flags a narrower, high-confidence violation class: a
number/measurement asserted in a sentence that does not appear anywhere in
the atom(s) it cites. Verified on the same real dataset: 0 false positives
across 79 sentence-units, 1-for-1 true positive on the one confirmed
production fabrication ("22-meter-long reclining Buddha" — no atom mentions
a length).

Known limitation, by design, not an oversight: this does NOT catch
qualitative/superlative claims that carry no number (e.g. "the high density
of leopards", "one of the world's oldest living religious monuments") — a
lexical check has no reliable signal for that class. Tracked separately as
AA-326.
"""
from __future__ import annotations

import re

# AA-639: a number is a digit run (optionally with thousands separators and a decimal part) that
# does not start inside a word/number. It may be followed directly by a unit ("4,460m", "710m",
# "12km") — the old `\b\d+\b` could not match "460m" at all (no word boundary between "0" and
# "m"), so a source written "4,460m" contributed only "4", and a rewrite written "4,460 m"
# was flagged for a "novel" 460. Thousands separators are normalised away ("4,460" == "4460").
_NUM_RE = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_DAY_LABEL_RE = re.compile(r"\bDay\s+\d+\b\s*:?", re.IGNORECASE)
_CITE_RE = re.compile(r"\[(?:R|F):[^\]]*\]")


def _numbers(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUM_RE.findall(text or "")}


def find_novel_numeric_claims(sentence: str, cited_atom_texts: list[str]) -> list[str]:
    """Numbers/measurements present in `sentence` but absent from the union of
    `cited_atom_texts`. Non-empty means the sentence asserts a specific figure
    none of its citations support. Strips citation tags and "Day N" itinerary
    labels before scanning — neither is a factual claim."""
    clean = _DAY_LABEL_RE.sub("", sentence)
    clean = _CITE_RE.sub("", clean)
    sentence_nums = _numbers(clean)
    if not sentence_nums:
        return []
    atom_nums: set[str] = set()
    for text in cited_atom_texts:
        atom_nums.update(_numbers(text))
    return sorted(sentence_nums - atom_nums)


def check_entailment(sentence: str, cited_atom_texts: list[str]) -> bool:
    """True if `sentence` introduces no unsupported number/measurement beyond
    what its cited atoms state. See module docstring for scope/limitations —
    this is a narrow, high-precision check, not full semantic entailment."""
    return not find_novel_numeric_claims(sentence, cited_atom_texts)
