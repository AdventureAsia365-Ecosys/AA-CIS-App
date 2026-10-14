"""AA-773 (S235) — is a moment OFFERED (optional / at extra cost) or included in the trip?

S234 r4: the itinerary said "Optional activities include boat rides on the water, horseback riding…"
but the seed showed "Khövsgöl Lake — take boat rides on the water" like any other moment, so the
writer promised the boat ride as part of the plan (F17 optional-as-plan 15x in the pool) and, the
other way round, called the included Tsaatan day optional. The seed now states it per moment on its
own line:

    Status: OFFERED, NOT INCLUDED (the itinerary says: "<offered sentence>"). Describe it as available,
        never as something the reader will do.   (one line in the seed)
    Status: included in the trip. Never call it optional.

The included line is written only on a day (route) or in an evidence (single moment) that also has
an offered item: the contrast is what the writer gets wrong.

This module is the ONE place that decides it (lesson S218: a value computed in two code paths must
use one function). `atom_seed` writes the Status line, and `quality_gates._is_offered`
(promises_an_option) and `trip_facts.optional_activities` (F17) read it back. It is light on purpose:
`atom_seed` must not import `quality_gates` (judge_client / llm_client).

The Status text itself contains 'optional' and 'not included': every reader of a seed either reads it
through `status_of_line` / `is_status_line` or skips lines starting with `STATUS_PREFIX`
(claim_rules, quality_gates.unused_source_facts, trip_facts.parse_moments).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from services.acp_content_writing.hygiene import content_words

# AA-514 — ported verbatim from the origin's `reference/offered-phrases.toml` (moved here from
# quality_gates, which re-exports it as `_OFFERED_PHRASES`). Said of something the traveller has
# not bought (at_a_price) or something included but not compulsory (offered).
OFFERED_PHRASES = frozenset({
    "additional cost", "at own expense", "at your own expense", "extra charge",
    "not included", "own expense", "pre-book", "pre-bookable", "supplementary charge",
    "supplement",
    "at your leisure", "can be arranged", "choice is yours", "for those who",
    "if desired", "if time allows", "if you prefer", "if you wish", "may wish",
    "on request", "optional", "optionally", "perhaps", "should you wish",
    "the option of", "those who wish", "upon request", "you may choose",
    "you may visit", "your choice",
})

OFFERED = "offered"
INCLUDED = "included"

STATUS_PREFIX = "Status:"
_OFFERED_HEAD = "Status: OFFERED, NOT INCLUDED"
INCLUDED_LINE = "Status: included in the trip. Never call it optional."

_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")
_LABEL_SPLIT_RE = re.compile(r"\s+[—–]\s+")
# Words of a moment label that never pick its sentence on their own ('take boat rides on the water':
# 'boat' + 'ride' do, 'take' / 'water' do not).
_LABEL_STOP = frozenset(content_words(
    "take takes visit visits stop stops enjoy explore experience overnight see local surrounding across along "
    "water terrain trip day journey continue arrive"))

# Review S235 (B2/B3) — what makes a CLAUSE offered for the Status line. Stricter than the warn-only
# gate's `OFFERED_PHRASES`: 'perhaps' ('Perhaps the highlight…') and 'for those who' ('a perfect day
# for those who love history') describe included content, and 'pre-book' must not match 'pre-booked
# for the group'. Cost wording the origin list misses ('at extra cost', 'additional fee') is added.
_STATUS_PHRASES = sorted(OFFERED_PHRASES - {"perhaps", "for those who", "pre-book", "supplement"}, key=len,
                         reverse=True)
_STATUS_OFFERED_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _STATUS_PHRASES) + r")\b"
    r"|\bpre-book(?:able)?\b(?!ed)|\bsupplement\b"
    r"|\bat (?:an )?extra\b|\bextra (?:cost|fee|charge)s?\b|\badditional (?:fee|charge|cost)s?\b"
    r"|\bfor a fee\b|\bsurcharge\b", re.I)
# Said of something that is part of the trip: never a reason to mark the moment offered.
_NOT_OFFERED_RE = re.compile(
    r"\b(?:fee|fees|meal|meals|lunch|dinner|breakfast|tips?|drinks?|entrance\w*|admission)\b[^.]{0,40}?"
    r"\bnot included\b|\bsingle supplement\b|\bpre-booked\b", re.I)
# F17's own loose marks (trip_facts.optional_activities reads this same pattern): a clause with one
# of them and no offered phrase gets NO Status line — unknown beats a wrong "Never call it optional".
LOOSE_MARK_RE = re.compile(r"\b(?:optional(?:ly)?|if you wish|if desired|on request|available|can|may)\b", re.I)
# A clause that opens a list ('Optional activities include boat rides, horseback riding…'): the
# items after it in the same clause run share its offered-ness.
_LIST_INTRO_RE = re.compile(r"\b(?:include|includes|including|such as|activities|options)\b", re.I)
_STRONG_SPLIT_RE = re.compile(r"\s*[;()]\s*|\s+[—–]\s+")
_CLAUSE_SPLIT_RE = re.compile(r"(\s*[,:]\s*)")
_INCLUDED_RE = re.compile(r"\bincluded\b", re.I)


def has_offered_phrase(text: Optional[str]) -> bool:
    """The origin's rule: one of `OFFERED_PHRASES` anywhere in `text` (lower case, substring)."""
    plain = (text or "").lower()
    return any(phrase in plain for phrase in OFFERED_PHRASES)


def offered_clause(text: Optional[str]) -> bool:
    """The Status line's rule for one clause: an offered or cost phrase once the 'not offered' wording
    ('lunch not included', 'single supplement', 'pre-booked') is taken out."""
    return bool(_STATUS_OFFERED_RE.search(_NOT_OFFERED_RE.sub(" ", text or "")))


def activity_text(clause: Optional[str]) -> str:
    """The activity an offered clause names, without its offer wording (F17 quotes it). At the first
    offer mark, the activity is the text AFTER it when that has content words ('kayaking and an
    optional hot-air balloon flight' → 'hot-air balloon flight'; 'with an optional stop at the water
    puppet show' → 'stop at the water puppet show'; 'Optional boat rides' → 'boat rides'), cut at the
    next mark; otherwise the text BEFORE it ('Horse riding is available at extra cost' → 'Horse
    riding'). Review S235 N1: taking the text before the mark made 'kayaking' the optional one."""
    text = " ".join((clause or "").split()).strip(" .")
    marks = sorted((m for rx in (LOOSE_MARK_RE, _STATUS_OFFERED_RE) for m in rx.finditer(text)),
                   key=lambda m: m.start())
    if not marks:
        return text.strip(" ,.;")
    first = marks[0]
    later = [m.start() for m in marks if m.start() >= first.end()]
    after = text[first.end():min(later) if later else len(text)]
    after = re.sub(r"(?:\s+(?:is|are|at|for|an|a|be))+\s*$", "", " " + after.strip(), flags=re.I).strip()
    if [w for w in content_words(after) if w not in _OFFER_STOP]:
        return after.strip(" ,.;")
    before = re.sub(r"(?:\s+(?:is|are|at|for|an|a|and|or|with|then))+\s*$", "", " " + text[:first.start()].strip(),
                    flags=re.I).strip()
    return before.strip(" ,.;")


_OFFER_STOP = frozenset(content_words(
    "available extra cost fee charge additional arranged supplement surcharge activities activity include includes "
    "including optional request wish desired"))


def sentences(text: Optional[str]) -> list[str]:
    return [s.strip() for s in _SENT_RE.split(" ".join((text or "").split())) if s.strip()]


def _stem(word: str) -> str:
    w = word.lower()
    for suf in ("ing", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    if len(w) > 3 and w[-1] == w[-2]:
        w = w[:-1]
    return w[:-1] if w.endswith("e") and len(w) > 3 else w


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in content_words(text) if w not in _LABEL_STOP}


def _label_stems(label: Optional[str]) -> tuple[set[str], set[str]]:
    parts = _LABEL_SPLIT_RE.split((label or "").strip(), 1)
    place, action = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
    return _stems(action), _stems(place)


def _score(text: str, label: Optional[str]) -> tuple[int, int]:
    act, plc = _label_stems(label)
    have = _stems(text)
    return len(act & have), len(plc & have)


@dataclass(frozen=True)
class Decision:
    """`status` OFFERED / INCLUDED / None (unknown); `quote` the itinerary text an OFFERED Status line
    quotes; `span` the evidence clause that names the moment (a substring of the evidence, '' when
    unknown) — trip_facts reads the rest of the line with its own rule."""
    status: Optional[str]
    quote: Optional[str] = None
    span: str = ""


def _clauses(sentence: str) -> list[tuple[int, list[tuple[str, str]]]]:
    """[(segment index, [(clause, separator before it)])] — segments split at ; ( ) and dashes (a new
    statement), clauses at , and : (list items)."""
    out = []
    for si, seg in enumerate(x for x in _STRONG_SPLIT_RE.split(sentence) if x and x.strip()):
        bits = _CLAUSE_SPLIT_RE.split(seg)
        items, sep = [], ""
        for i, b in enumerate(bits):
            if i % 2:
                sep = b.strip()
            elif b.strip():
                items.append((b.strip(), sep))
        out.append((si, items))
    return out


def _decide_sentence(sent: str, label: Optional[str]) -> Decision:
    """Clause level (review S235 B3): the clause that names the moment decides. It is OFFERED when it
    has an offered phrase itself, or when an earlier clause of the same segment opens a list with one
    ('Optional activities include boat rides …, horseback riding …'). Not offered and a loose F17 mark
    in its segment ('Kayaks are available …') → unknown. A sentence whose clauses name the moment
    nowhere is decided whole only when it is one plain clause."""
    best: Optional[tuple] = None
    for si, items in _clauses(sent):
        for ci, (clause, _) in enumerate(items):
            score = _score(clause, label)
            if best is None or score > best[0]:
                best = (score, si, ci, items)
    if best is None:
        return Decision(None)
    score, _, ci, items = best
    if score == (0, 0):
        flat = [c for _, its in _clauses(sent) for c in its]
        if len(flat) != 1:
            return Decision(None)
    clause = items[ci][0]
    offered = offered_clause(clause)
    if not offered:
        for cj in range(ci - 1, -1, -1):
            prev, _ = items[cj]
            sep_after = items[cj + 1][1]
            if offered_clause(prev) and (_LIST_INTRO_RE.search(prev) or sep_after == ":"):
                offered = True
                break
    if offered:
        return Decision(OFFERED, sent, clause)
    segment = " ".join(c for c, _ in items)
    if LOOSE_MARK_RE.search(segment):
        return Decision(None, None, clause)
    return Decision(INCLUDED, None, clause)


def moment_sentence(evidence: Optional[str], label: Optional[str]) -> Optional[str]:
    """The evidence sentence that names this moment: most words of the label's action part
    ('boat rides'), then of its place part; a one-sentence evidence is its own sentence. None when
    no sentence shares a label word, or when the best-scoring sentences decide differently
    (unknown beats a wrong Status)."""
    sents = sentences(evidence)
    if not sents:
        return None
    if len(sents) == 1:
        return sents[0]
    scored = [(_score(s, label), s) for s in sents]
    best = max(score for score, _ in scored)
    if best == (0, 0):
        return None
    top = [s for score, s in scored if score == best]
    if len({_decide_sentence(s, label).status for s in top}) > 1:
        return None
    return top[0]


def decide(evidence: Optional[str], label: Optional[str], *,
           prev_offered_quote: Optional[str] = None) -> Decision:
    """The moment's Decision. Sentence, then clause level: only the clause that names this moment
    decides ('Day starts at the lake. Optional boat rides are available.' makes the boat ride OFFERED
    and the lake INCLUDED). An evidence that starts in lower case right after an OFFERED moment of the
    same place and day (`prev_offered_quote`) continues that list: A3 split 'Optional activities
    include …, visits to local nomadic families, trekking along shorelines and forest paths' into one
    atom per item, and the last one's evidence is only 'trekking along shorelines and forest paths'
    (same rule as trip_facts.optional_activities) — unless the fragment says it is included."""
    ev = " ".join((evidence or "").split())
    if not ev:
        return Decision(None)
    if (prev_offered_quote and ev[:1].islower()
            and not (_INCLUDED_RE.search(ev) and not re.search(r"\bnot included\b", ev, re.I))):
        return Decision(OFFERED, f"{prev_offered_quote.rstrip(' .')} … {ev}", ev)
    sent = moment_sentence(ev, label)
    if sent is None:
        return Decision(None)
    return _decide_sentence(sent, label)


def evidence_status(evidence: Optional[str], label: Optional[str], *,
                    prev_offered_quote: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """(OFFERED | INCLUDED | None, the quoted itinerary text for an OFFERED moment) — see `decide`."""
    d = decide(evidence, label, prev_offered_quote=prev_offered_quote)
    return d.status, d.quote


def offered_line(quote: str) -> str:
    q = " ".join((quote or "").split()).replace('"', "'")
    return (f'{_OFFERED_HEAD} (the itinerary says: "{q}"). '
            "Describe it as available, never as something the reader will do.")


def status_line(status: Optional[str], quote: Optional[str], *, contrast: bool) -> Optional[str]:
    """The Status line to render: always for OFFERED; for INCLUDED only when `contrast` (the same
    day / evidence has an offered item); never for an unknown status."""
    if status == OFFERED and quote:
        return offered_line(quote)
    if status == INCLUDED and contrast:
        return INCLUDED_LINE
    return None


def single_status_line(evidence: Optional[str], label: Optional[str]) -> Optional[str]:
    """A one-atom seed's Status line: OFFERED from its own sentence; included only when another
    sentence of the same evidence is offered."""
    status, quote = evidence_status(evidence, label)
    contrast = status == INCLUDED and any(offered_clause(s) for s in sentences(evidence))
    return status_line(status, quote, contrast=contrast)


def is_status_line(line: Optional[str]) -> bool:
    return (line or "").lstrip().startswith(STATUS_PREFIX)


def status_of_line(line: Optional[str]) -> Optional[str]:
    """OFFERED / INCLUDED for a Status line, None for any other line."""
    s = (line or "").strip()
    if s.startswith(_OFFERED_HEAD):
        return OFFERED
    if s.startswith("Status: included"):
        return INCLUDED
    return None


def strip_status_lines(text: Optional[str]) -> str:
    return "\n".join(ln for ln in (text or "").split("\n") if not is_status_line(ln))


def has_status_lines(text: Optional[str]) -> bool:
    return any(status_of_line(ln) for ln in (text or "").splitlines())


def is_offered_seed(text: Optional[str]) -> bool:
    """promises_an_option's test for one seed block. A block that carries Status lines is offered
    only if one of them says OFFERED: the included line's 'Never call it optional' and the offered
    line's own 'NOT INCLUDED' must not count as offered phrases (an included-only block would then
    warn on every sentence). A block without Status lines (pre-AA-773 seed, or a sentence the seed
    builder could not place) keeps the origin's phrase rule."""
    if has_status_lines(text):
        return any(status_of_line(ln) == OFFERED for ln in (text or "").splitlines())
    return has_offered_phrase(text)
