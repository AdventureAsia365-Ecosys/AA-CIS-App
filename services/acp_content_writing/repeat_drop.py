"""AA-1077 (S279) — the blog repeat drop: a deterministic post-repair step (no model call) that deletes the later copy
of an exact or near-exact repeated sentence before the F13 check, when the blog stays at or above the F4 floor.

S278 paid trial 2 (t9_write v7, 3 audit-tenant tours): one piece was held `F13_repeat` at 5,724 chars (979 words) for
ONE repeated sentence left after three repair rounds. The final text-integrity pass (`hygiene.dedupe`) deletes a
repeat only when the delete takes no word out of the piece (`added_words`, S252 fourth review); a near-exact copy
that changed a word or two keeps that word, so dedupe leaves it and the F13 gate holds the whole article for one
sentence a reader would skip. The sentence repair is an LLM call that already failed on it three times.

The rule (a proposer; the existing invariants stay the arbiters):

- **candidate** — a sentence the F13 gate would quote (`hygiene.find_issues(...)["repeat"]`) that is an exact or
  near-exact repeat of an earlier prose sentence: its copy words (`hygiene.copy_words`) match the earlier one's IN
  ORDER with a Dice share >= the setting `cis.t9.repeat_drop_near_exact` (0.9; 1.0 = exact). A paraphrase, a
  figure told twice in other words, an echo or a misplaced moment is not a near-exact copy and stays for F13.
- **floor** — the piece after the delete still measures >= the F4 blog floor (`length_contract.floor_chars()`,
  tag-stripped). Otherwise the piece is held as today: a drop never trades F13 for a `length_short` hold.
- **invariants** — the delete orphans no pronoun (`hygiene._dup_safe_delete`), keeps every fact token of the removed
  sentence somewhere in the piece (`hygiene._keeps_facts`, the fact-preservation invariant), and breaks nothing
  (`hygiene.integrity_problems`); the T9 caller also runs it under the step contract (`_StepGuard`, invariant 7).

Both F13 gates feed it (S279 lane f13-drop): every `F13_repeat` finding and every `F13_hygiene` repeat warning
("repeats an earlier sentence: '…'") is a candidate under the same rule; a candidate the rule refuses stays for its
gate, which remains the backstop. On today's detectors a near-exact repeat is always an `F13_repeat` copy (a Dice share
>= 0.8 implies `hygiene.copies`), so the hygiene feed is a backstop for a detector change, not a second source of drops.

Blog only (the floor is the blog band). Switch: env `T9_REPEAT_DROP` (`off` / `0` / `false` = off; anything else, or
unset, = on). Every drop is logged for the admin F13_autofix flag; a candidate refused by the floor is logged too.
`trace` (the $0 replay, scripts/aa1077/repeat_feed_replay.py) gets each candidate's outcome code, never its text.
"""
from __future__ import annotations

import difflib
import os
from typing import Optional

from services.acp_content_writing import hygiene
from services.acp_content_writing import length_contract as lc
from services.quality import registry as qr
from shared import settings

MODE_ENV = "T9_REPEAT_DROP"
_OFF = frozenset({"off", "0", "false", "no"})
NEAR_EXACT_SETTING = "cis.t9.repeat_drop_near_exact"   # S279 thresholds L1: Platform › Settings (evidence required)
NEAR_EXACT_SHARE = float(settings.definition(NEAR_EXACT_SETTING).default_value)   # the compiled default (0.9)

# The gates whose repeat findings are candidates, in priority order (a sentence both quote counts as F13_repeat).
SOURCES = (qr.F13_REPEAT, qr.F13_HYGIENE)
# F13_hygiene's repeat warning (hygiene.find_issues: the near-verbatim arm that is not a strict repeat). Its other
# warnings (social proof, hashtags, an unbalanced quote, a dangling opener) are never repeats.
HYGIENE_REPEAT_PREFIX = "repeats an earlier sentence: '"

# Outcome codes of a candidate (`trace`): what the rule decided, never the text.
DROPPED, NOT_NEAR_EXACT, FLOOR, PRONOUN, FACTS, INTEGRITY = (
    "dropped", "not_near_exact", "floor", "pronoun", "facts", "integrity")
OUTCOMES = (DROPPED, NOT_NEAR_EXACT, FLOOR, PRONOUN, FACTS, INTEGRITY)


def near_exact_share() -> float:
    """The Dice share in force (setting `cis.t9.repeat_drop_near_exact`)."""
    return float(settings.get(NEAR_EXACT_SETTING))


_MIN_COPY_WORDS = 4      # hygiene._COPY_MIN_WORDS: a short line is never judged a copy
_MAX_DROPS = 5           # one drop per pass (offsets move); a piece with more repeats is not this step's case


def enabled() -> bool:
    return (os.environ.get(MODE_ENV) or "").strip().lower() not in _OFF


def dice(later: str, earlier: str, ignore: frozenset = frozenset()) -> Optional[float]:
    """The ordered Dice share of the two sentences' copy words (2 x matched / (len a + len b), difflib matching
    blocks); None when either has fewer than 4 copy words (never judged a copy). Pure."""
    ca, cb = hygiene.copy_words(later, ignore), hygiene.copy_words(earlier, ignore)
    if len(ca) < _MIN_COPY_WORDS or len(cb) < _MIN_COPY_WORDS:
        return None
    matched = sum(m.size for m in difflib.SequenceMatcher(None, ca, cb, autojunk=False).get_matching_blocks())
    return 2.0 * matched / (len(ca) + len(cb))


def near_exact(later: str, earlier: str, ignore: frozenset = frozenset(), share: Optional[float] = None) -> bool:
    """`later` repeats `earlier` exactly or nearly: >= 4 copy words each, and their ordered Dice share >= `share`
    (default: the setting in force). Pure but for the setting read."""
    d = dice(later, earlier, ignore)
    return d is not None and d >= (near_exact_share() if share is None else share)


def _quoted(finding: str) -> str:
    """The sentence an F13 repeat finding quotes ("<problem>: '<sentence[:200]>'")."""
    return finding.rsplit(": '", 1)[-1][:-1] if finding.endswith("'") else finding.rsplit(": '", 1)[-1]


def candidates(text: str, hyg: hygiene.HygieneContext, sources: tuple = SOURCES) -> dict[str, str]:
    """{quoted sentence (its first 200 chars): the F13 gate that quotes it} for the repeat findings of `sources`.
    One `find_issues` call; a sentence quoted by both gates is F13_repeat's (the blocking one)."""
    found = hygiene.find_issues(text, hyg)
    out: dict[str, str] = {}
    if qr.F13_REPEAT in sources:
        for f in found["repeat"]:
            out.setdefault(_quoted(f), qr.F13_REPEAT)
    if qr.F13_HYGIENE in sources:
        for f in found["hygiene"]:
            if f.startswith(HYGIENE_REPEAT_PREFIX):
                out.setdefault(_quoted(f), qr.F13_HYGIENE)
    return out


def _cut(text: str, span: dict) -> str:
    end = span["end"] + 1 if text[span["end"]:span["end"] + 1] == " " else span["end"]
    return hygiene.tidy_after_cut(text[:span["start"]] + text[end:])


class _Run:
    """One drop_repeats call: the text so far, its log, the refused sentences and each candidate's outcome."""

    def __init__(self, text: str, hyg: hygiene.HygieneContext, floor: int, share: float, sources: tuple):
        self.text, self.hyg, self.floor, self.share, self.sources = text, hyg, floor, share, sources
        self.ignore = hygiene.brand_words(hyg.brand_name)
        self.seed = hygiene.source_forms(hyg.source_text)
        self.log: list[str] = []
        self.refused: set[str] = set()
        self.outcomes: dict[str, dict] = {}

    def step(self) -> bool:
        """One pass: drop the first candidate the rule accepts. False when there is none (the loop stops)."""
        quoted = candidates(self.text, self.hyg, self.sources)
        if not quoted:
            return False
        spans = hygiene._dup_spans(self.text, self.hyg.cta)
        for j, span in enumerate(spans):
            source = quoted.get(span["clean"][:200])
            if source is None or span["clean"] in self.refused:
                continue
            if self._try(span, spans[:j], source):
                return True
        return False

    def _best(self, span: dict, earlier: list[dict]) -> float:
        shares = (dice(span["clean"], t["clean"], self.ignore) for t in earlier if t["end"] <= span["start"])
        return max((d for d in shares if d is not None), default=0.0)

    def _unsafe(self, span: dict, after: str) -> Optional[str]:
        """The invariant the delete would break (PRONOUN / FACTS / INTEGRITY), or None."""
        if not hygiene._dup_safe_delete(self.text, span):
            return PRONOUN
        if not hygiene._keeps_facts(after, span["clean"], self.seed):
            return FACTS
        if hygiene.integrity_problems(self.text, after, self.ignore, self.hyg.cta):
            return INTEGRITY
        return None

    def _note(self, clean: str, source: str, outcome: str, best: float, chars: Optional[int] = None) -> None:
        self.outcomes[clean] = {"source": source, "outcome": outcome, "dice": round(best, 3), "chars_after": chars}

    def _try(self, span: dict, earlier: list[dict], source: str) -> bool:
        clean = span["clean"]
        best = self._best(span, earlier)
        if best < self.share:
            self._note(clean, source, NOT_NEAR_EXACT, best)
            return False
        after = _cut(self.text, span)
        chars = lc.measure(after)
        why = FLOOR if chars < self.floor else self._unsafe(span, after)
        if why is not None:
            self.refused.add(clean)
            self._note(clean, source, why, best, chars)
            if why == FLOOR:
                self.log.append(f"kept a near-exact repeat: dropping it leaves {chars} chars, under the blog floor of "
                                f"{self.floor:,}: '{clean[:160]}'")
            return False
        self.text = after
        self._note(clean, source, DROPPED, best, chars)
        self.log.append(f"dropped a near-exact repeat of an earlier sentence ({source}; {chars:,} chars left): "
                        f"'{clean[:160]}'")
        return True


def drop_repeats(text: str, hyg: hygiene.HygieneContext, *, floor: Optional[int] = None,
                 share: Optional[float] = None, sources: tuple = SOURCES,
                 trace: Optional[list] = None) -> tuple[str, list[str]]:
    """Delete the later copy of each exact / near-exact repeat the F13 gates of `sources` quote (default: F13_repeat
    and F13_hygiene's repeat warnings), while the piece stays at or above `floor` (default: the blog floor in force)
    and every invariant holds. Returns (text, log). `trace`, when given, gets one entry per candidate:
    {source, outcome (OUTCOMES), dice, chars_after} — no text. Pure (no I/O) but for the setting reads."""
    if not text or hyg is None:
        return text, []
    run = _Run(text, hyg, lc.floor_chars() if floor is None else floor,
               near_exact_share() if share is None else share, tuple(sources))
    for _ in range(_MAX_DROPS):
        if not run.step():
            break
    if trace is not None:
        trace.extend(run.outcomes.values())
    return run.text, run.log


__all__ = ["DROPPED", "FACTS", "FLOOR", "HYGIENE_REPEAT_PREFIX", "INTEGRITY", "MODE_ENV", "NEAR_EXACT_SETTING",
           "NEAR_EXACT_SHARE", "NOT_NEAR_EXACT", "OUTCOMES", "PRONOUN", "SOURCES", "candidates", "dice",
           "drop_repeats", "enabled", "near_exact", "near_exact_share"]
