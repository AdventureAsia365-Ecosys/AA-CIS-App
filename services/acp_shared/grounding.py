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

AA-785 (S235) — `year_forms=True` (T9/T10 F1 only) also reads year ranges and decades the way a
reader does: "1637–38" claims the years 1637 and 1638 (not a number 38), "the late 1630s" claims a
year in 1630–1639 (not the year 1630). The default stays the plain digit-run reading, so T3, A1 and
source_facts behave exactly as before.
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
# AA-912: body excludes "[" too, so a run of unclosed "[F:" tags is scanned once, not once per tag (was O(n^2)).
_CITE_RE = re.compile(r"\[[RF]:[^\[\]]*\]")
# AA-785 — "1637–38" / "1637-38" / "1637/38": a 4-digit year, a dash or slash, a 2-digit tail that is
# not the start of a longer number. Same left boundary as _NUM_RE.
_YEAR_RANGE_RE = re.compile(r"(?<![\w.,])(1\d{3}|20\d{2})\s*[–—\-/]\s*(\d{2})(?!\d|[.,]\d)")
# "1630s" / "1630's": a decade ("1600s" — a century).
_DECADE_RE = re.compile(r"(?<![\w.,])(1\d{2}0|20\d0)['’]?s\b")


def _numbers(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUM_RE.findall(text or "")}


def _range_years(m: re.Match) -> tuple[int, int]:
    """(start, full end year) of a _YEAR_RANGE_RE match: '1637–38' → (1637, 1638), '1998–02' → (1998, 2002)."""
    start = int(m.group(1))
    end = start // 100 * 100 + int(m.group(2))
    return start, end + 100 if end <= start else end


def _decade_supported(decade: int, source_nums: set[str]) -> bool:
    """A source year inside the decade ('1637' for the 1630s), or inside the century for a
    '…00s' ('1637' for the 1600s — "the 1600s" is read as the century, the looser reading)."""
    span = 100 if decade % 100 == 0 else 10
    return any(len(n) == 4 and n.isdigit() and decade <= int(n) < decade + span for n in source_nums)


def _novel_year_forms(clean: str, cited_atom_texts: list[str]) -> list[str]:
    """find_novel_numeric_claims with year ranges and decades read as years (AA-785).

    A range's start year is checked like any number. Its end year is supported when the source has
    it (a source range '1637–52' counts as 1652) or when it is start + 1 and the source has the start
    (a founding that spans two calendar years); otherwise the FULL end year is reported ('1652', not
    '52'). A decade is supported by any source year inside it; otherwise it is reported as '1880s'.
    Every other number is checked exactly as in the default reading."""
    atom_nums: set[str] = set()
    for text in cited_atom_texts:
        atom_nums.update(_numbers(text))
        atom_nums.update(str(_range_years(m)[1]) for m in _YEAR_RANGE_RE.finditer(text or ""))
    novel: set[str] = set()
    for m in _YEAR_RANGE_RE.finditer(clean):
        start, end = _range_years(m)
        if str(start) not in atom_nums:
            novel.add(str(start))
        if str(end) not in atom_nums and not (end == start + 1 and str(start) in atom_nums):
            novel.add(str(end))
    rest = _YEAR_RANGE_RE.sub(" ", clean)
    for m in _DECADE_RE.finditer(rest):
        if not _decade_supported(int(m.group(1)), atom_nums):
            novel.add(f"{m.group(1)}s")
    novel.update(_numbers(_DECADE_RE.sub(" ", rest)) - atom_nums)
    return sorted(novel)


def find_novel_numeric_claims(sentence: str, cited_atom_texts: list[str], *, year_forms: bool = False) -> list[str]:
    """Numbers/measurements present in `sentence` but absent from the union of
    `cited_atom_texts`. Non-empty means the sentence asserts a specific figure
    none of its citations support. Strips citation tags and "Day N" itinerary
    labels before scanning — neither is a factual claim.

    `year_forms=True` (AA-785, T9/T10 F1 only) reads "1637–38" and "1630s" as years — see
    _novel_year_forms. Off by default: T3, A1 and source_facts keep the plain reading."""
    clean = _DAY_LABEL_RE.sub("", sentence)
    clean = _CITE_RE.sub("", clean)
    if year_forms:
        return _novel_year_forms(clean, cited_atom_texts)
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
