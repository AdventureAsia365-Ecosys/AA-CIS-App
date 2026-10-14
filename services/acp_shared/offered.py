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


def has_offered_phrase(text: Optional[str]) -> bool:
    """The origin's rule: one of `OFFERED_PHRASES` anywhere in `text` (lower case, substring)."""
    plain = (text or "").lower()
    return any(phrase in plain for phrase in OFFERED_PHRASES)


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


def moment_sentence(evidence: Optional[str], label: Optional[str]) -> Optional[str]:
    """The evidence sentence that names this moment: most words of the label's action part
    ('boat rides'), then of its place part; a one-sentence evidence is its own sentence. None when
    no sentence shares a label word, or when the best-scoring sentences disagree on offered-ness
    (unknown beats a wrong Status)."""
    sents = sentences(evidence)
    if not sents:
        return None
    if len(sents) == 1:
        return sents[0]
    parts = _LABEL_SPLIT_RE.split((label or "").strip(), 1)
    place, action = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
    act, plc = _stems(action), _stems(place)
    scored = []
    for s in sents:
        have = _stems(s)
        scored.append(((len(act & have), len(plc & have)), s))
    best = max(score for score, _ in scored)
    if best == (0, 0):
        return None
    top = [s for score, s in scored if score == best]
    if len({has_offered_phrase(s) for s in top}) > 1:
        return None
    return top[0]


def evidence_status(evidence: Optional[str], label: Optional[str], *,
                    prev_offered_quote: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """(OFFERED | INCLUDED | None, the quoted itinerary text for an OFFERED moment).

    Sentence level: only the sentence that names this moment decides ('Day starts at the lake.
    Optional boat rides are available.' makes the boat ride OFFERED and the lake INCLUDED). An
    evidence that starts in lower case right after an OFFERED moment of the same place and day
    (`prev_offered_quote`) continues that list: A3 split 'Optional activities include …, visits to
    local nomadic families, trekking along shorelines and forest paths' into one atom per item, and
    the last one's evidence is only 'trekking along shorelines and forest paths' (same rule as
    trip_facts.optional_activities)."""
    ev = " ".join((evidence or "").split())
    if not ev:
        return None, None
    if prev_offered_quote and ev[:1].islower():
        return OFFERED, f"{prev_offered_quote.rstrip(' .')} … {ev}"
    sent = moment_sentence(ev, label)
    if sent is None:
        return None, None
    return (OFFERED, sent) if has_offered_phrase(sent) else (INCLUDED, None)


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
    contrast = status == INCLUDED and any(has_offered_phrase(s) for s in sentences(evidence))
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
