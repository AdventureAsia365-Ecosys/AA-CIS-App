"""AA-925 (Q-2, design §4.3) — the repair contract: what any repair (an LLM fix of a field or a sentence, a
deterministic fit, a cut) may never do. Recurring failure #1 (S218–S245) was a repair that fixed one gate and
silently broke another: flag_fix re-adding "explore" to a meta (S218 Sri Lanka), the meta fitter cutting
"…a 20-kilometre walk" to "…a 20-kilometre." (S2), a sentence repair writing "the afternoon" the source never
gave (AA-777 r6).

Five invariants, checked per changed field (A1 flag_fix, A1 grounding repair, T3 sentence repair) or per
replaced sentence (T9 sentence repair):

1. ``gates_kept``       — a gate that passed before the repair does not fail after it. The gate runner is
                          injected per field (``gate_runner(field, text) -> set[str]``): this module imports no
                          writer module (graph / flag_fix_node / service import it).
2. ``no_new_specifics`` — no number, and no capitalised name, that neither the text before the repair nor the
                          source gives.
3. ``no_truncation``    — the field still ends as a complete sentence when it did before: terminal punctuation,
                          no "…", no trailing connector, and no **cut signature** (the repaired text minus its
                          final period is a strict word-prefix of the text before, cut mid-sentence —
                          "…a 20-kilometre." from "…a 20-kilometre walk.", which ``meta_complete_sentence``
                          accepts on purpose, AA-794).
4. ``scope_kept``       — only the flagged fields (A1 / T3) change.
5. ``tags_balanced``    — ``[R:…]``/``[F:…]`` tags, ``**`` and brackets stay balanced when they were.

The design's sixth idea, "no blanding" (a repair may not lower the source fact coverage), needs the fact
coverage measure of Q-LEARN and is left to that issue.

A violation is never a hold: the field (or sentence) is reverted to its pre-repair text and the output gets a
soft ``degraded: repair_contract`` entry (services/quality/degraded.py). The AA-528/AA-780 regression guard of
T9 full rewrites stays where it is (service.run_write_background) and already enforces invariant 1 there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from services.acp_shared.citation_tags import strip_citation_tags
from services.acp_shared.grounding import find_novel_numeric_claims

INVARIANTS = ("gates_kept", "no_new_specifics", "no_truncation", "scope_kept", "tags_balanced")

GateRunner = Callable[[str, str], set]

_TERMINAL_RE = re.compile(r"[.!?][\"'’”)\]]*\s*$")
_ELLIPSIS_RE = re.compile(r"(?:\.\.\.|…)[\"'’”)\]]*\s*$")
_CONNECTORS = frozenset({
    "and", "or", "but", "with", "of", "to", "the", "a", "an", "for", "in", "on", "at", "by", "from", "as", "via",
    "into", "through", "between", "while", "where", "which", "that", "including",
})
_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][\w'’-]*")
_CAP_RE = re.compile(r"(?<![\w'’-])([A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ][\w'’-]*)")
_SENT_START_RE = re.compile(r"(?:^|[.!?:;]\s+|\n\s*(?:[-*•]\s*)?|\*\*\s*|#+\s*|\"\s*|“\s*)$")
_OPEN_CLOSE = (("(", ")"), ("[", "]"), ("{", "}"))
_TAG_OPEN_RE = re.compile(r"\[(?:R|F):")
# Capitalised words that are not names: sentence words a repair may legitimately start a clause with.
_COMMON_CAPS = frozenset({
    "The", "A", "An", "This", "That", "These", "Those", "It", "Its", "We", "You", "Your", "Our", "They", "There",
    "Here", "Day", "Days", "Night", "Nights", "From", "With", "After", "Before", "Then", "On", "In", "At", "For",
    "And", "But", "Or", "If", "When", "While", "Each", "Every", "All", "Some", "Most", "No", "Not", "One", "Two",
    "I", "As", "By", "To", "Of", "Into", "Over", "Along", "Through", "Between", "Around", "Across", "Lunch",
    "Dinner", "Breakfast", "Morning", "Afternoon", "Evening", "Arrive", "Drive", "Visit", "Walk", "Hike",
    "Return", "Transfer", "Explore", "Enjoy", "Discover", "Take", "Spend", "Stop", "Continue", "Head",
})


@dataclass
class ContractResult:
    violations: list[dict] = field(default_factory=list)   # {"invariant", "field", "detail"}

    @property
    def ok(self) -> bool:
        return not self.violations

    def fields(self) -> list[str]:
        return sorted({v["field"] for v in self.violations})

    def invariants(self) -> list[str]:
        return sorted({v["invariant"] for v in self.violations})


# ── single checks (pure, public: the T9 span check and tests use them) ───────────────────────────


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(_as_text(v) for v in value.values())
    return str(value)


def _words(text: str) -> list[str]:
    return strip_citation_tags(text or "", " ").split()


def ends_complete(text: str) -> bool:
    """Terminal punctuation, no ellipsis, no trailing connector."""
    t = strip_citation_tags(text or "", " ").rstrip()
    if not t or _ELLIPSIS_RE.search(t) or not _TERMINAL_RE.search(t):
        return False
    words = _WORD_RE.findall(t)
    return not (words and words[-1].lower() in _CONNECTORS)


def cut_signature(before: str, after: str) -> Optional[str]:
    """Why ``after`` looks like ``before`` cut mid-sentence and closed with a period, or None. A cut at a
    sentence boundary (``_salvage_to_band``) is not a signature: the word before the cut ended a sentence."""
    b, a = _words(before), _words(after)
    if not a or not b or len(a) >= len(b):
        return None
    last = a[-1]
    if not last.endswith("."):
        return None
    a_core = a[:-1] + [last[:-1]]
    if a_core != b[: len(a_core)]:
        return None
    if re.search(r"[.!?][\"'’”)\]]*$", b[len(a_core) - 1]):
        return None     # the original sentence ended there: a sentence-boundary salvage
    return f"cut after '{a_core[-1]}' (dropped '{' '.join(b[len(a_core):len(a_core) + 3])}…')"


def balanced(text: str) -> bool:
    """Citation tags, ``**`` and brackets balance (the text_integrity._unbalanced idea, without its prose
    heuristics for quotes)."""
    t = text or ""
    if _TAG_OPEN_RE.search(t) and t.count("[") != t.count("]"):
        return False
    if t.count("**") % 2:
        return False
    plain = strip_citation_tags(t, " ")
    return all(plain.count(o) == plain.count(c) for o, c in _OPEN_CLOSE)


def _sentence_initial(text: str, idx: int) -> bool:
    return bool(_SENT_START_RE.search(text[:idx]))


def new_names(before: str, after: str, source: str = "") -> list[str]:
    """Capitalised words in ``after`` (not sentence-initial, not a common word) that neither ``before`` nor the
    source contains — a place, operator or name a repair invented."""
    known = f"{before or ''}\n{source or ''}".lower()
    out: list[str] = []
    a = strip_citation_tags(after or "", " ")
    for m in _CAP_RE.finditer(a):
        word = m.group(1).rstrip("'’-")
        if word in _COMMON_CAPS or _sentence_initial(a, m.start()):
            continue
        if not re.search(r"(?<![a-z])" + re.escape(word.lower()) + r"(?![a-z])", known):
            out.append(word)
    return list(dict.fromkeys(out))


def new_numbers(before: str, after: str, source: str = "") -> list[str]:
    return find_novel_numeric_claims(strip_citation_tags(after or "", " "),
                                     [source or "", strip_citation_tags(before or "", " ")])


def new_specifics(original: str, replacement: str, *, trip_ctx=None, source: Optional[str] = None) -> list[str]:
    """AA-777 (r6 fix 1), moved here from sentence_repair (which re-exports it) — the specifics a sentence
    repair ADDED that neither the sentence it replaces nor the source gives: a number (the F1 reading, against
    ``source`` + the original sentence) and, with ``trip_ctx``, a time of day / ordinal / position or (blog) a
    pace or rest word the F17 checks would flag (trip_facts.new_specific_claims). [] for a cut (empty
    replacement). r6: 4 of 26 retries added a blocking fail; R1 and R2 were exactly this."""
    rep = strip_citation_tags(replacement, " ").strip()
    if not rep:
        return []
    orig = strip_citation_tags(original, " ")
    out = [f"adds {n}, which neither the sentence nor the source gives"
           for n in find_novel_numeric_claims(rep, [source or "", orig], year_forms=True)]
    if trip_ctx is not None:
        from services.acp_content_writing.trip_facts import new_specific_claims  # lazy: a T9 module
        out += new_specific_claims(orig, rep, trip_ctx)
    return out


# ── the field contract (A1 flag_fix, A1 grounding repair, T3 sentence repair) ────────────────────


def check(before: dict, after: dict, *, allowed: Optional[Iterable[str]] = None, source: str = "",
          gate_runner: Optional[GateRunner] = None, names: bool = True) -> ContractResult:
    """Every invariant over the fields that changed. ``allowed`` = the flagged fields (None: any field may
    change). ``gate_runner(field, text)`` = the failing gate codes of one field's text."""
    res = ContractResult()
    allowed_set = set(allowed) if allowed is not None else None
    for key in sorted(set(before) | set(after)):
        b_raw, a_raw = before.get(key), after.get(key)
        if a_raw == b_raw:
            continue
        b, a = _as_text(b_raw), _as_text(a_raw)
        if allowed_set is not None and key not in allowed_set:
            res.violations.append({"invariant": "scope_kept", "field": key, "detail": "not a flagged field"})
            continue
        if gate_runner is not None:
            newly = sorted(set(gate_runner(key, a) or ()) - set(gate_runner(key, b) or ()))
            if newly:
                res.violations.append({"invariant": "gates_kept", "field": key, "detail": ", ".join(newly)})
        added = new_numbers(b, a, source) + (new_names(b, a, source) if names else [])
        if added:
            res.violations.append({"invariant": "no_new_specifics", "field": key,
                                   "detail": "adds " + ", ".join(added[:5])})
        if a.strip():
            if ends_complete(b) and not ends_complete(a):
                res.violations.append({"invariant": "no_truncation", "field": key,
                                       "detail": "no longer ends as a complete sentence"})
            else:
                sig = cut_signature(b, a)
                if sig:
                    res.violations.append({"invariant": "no_truncation", "field": key, "detail": sig})
        if balanced(b) and not balanced(a):
            res.violations.append({"invariant": "tags_balanced", "field": key,
                                   "detail": "unbalanced tag, bold or bracket"})
    return res


def enforce_fields(before: dict, after: dict, *, allowed: Optional[Iterable[str]] = None, source: str = "",
                   gate_runner: Optional[GateRunner] = None, names: bool = True) -> tuple[dict, ContractResult]:
    """``after`` with every violating field put back to its ``before`` value (a field absent before is
    dropped), plus the result. The caller adds ``degraded: repair_contract`` when ``not result.ok``."""
    res = check(before, after, allowed=allowed, source=source, gate_runner=gate_runner, names=names)
    if res.ok:
        return after, res
    out = dict(after)
    for key in res.fields():
        if key in before:
            out[key] = before[key]
        else:
            out.pop(key, None)
    return out, res


# ── the span contract (T9 sentence repair) ───────────────────────────────────────────────────────


def span_problems(original: str, replacement: str) -> list[str]:
    """Invariants 3 and 5 for one replaced sentence (a cut — empty replacement — is never a truncation;
    invariants 1 and 2 are the splice_checked integrity and new_specifics guards it already runs)."""
    rep = (replacement or "").strip()
    if not rep:
        return []
    out = []
    if ends_complete(original) and not ends_complete(rep):
        out.append("no_truncation: the repaired sentence does not end as a complete sentence")
    elif cut_signature(original, rep):
        out.append("no_truncation: " + cut_signature(original, rep))
    if balanced(original) and not balanced(rep):
        out.append("tags_balanced: unbalanced tag, bold or bracket")
    return out


def enforce_spans(items: list[dict], replacements: list[str]) -> tuple[list[dict], list[str], list[dict]]:
    """(kept items, kept replacements, reverted items). A reverted item keeps its sentence and gets
    ``"guard": "contract"`` plus its problems (the AA-777 guard convention)."""
    keep_items, keep_reps, reverted = [], [], []
    for item, rep in zip(items, replacements):
        found = span_problems(item.get("sentence") or "", rep)
        if found:
            item["guard"], item["contract"] = "contract", found
            reverted.append(item)
            continue
        keep_items.append(item)
        keep_reps.append(rep)
    return keep_items, keep_reps, reverted
