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
- **floor** — the piece after the delete still measures >= the F4 blog floor (`length_contract.BLOG_FLOOR_CHARS`,
  tag-stripped). Otherwise the piece is held as today: a drop never trades F13 for a `length_short` hold.
- **invariants** — the delete orphans no pronoun (`hygiene._dup_safe_delete`), keeps every fact token of the removed
  sentence somewhere in the piece (`hygiene._keeps_facts`, the fact-preservation invariant), and breaks nothing
  (`hygiene.integrity_problems`); the T9 caller also runs it under the step contract (`_StepGuard`, invariant 7).

Blog only (the floor is the blog band). Switch: env `T9_REPEAT_DROP` (`off` / `0` / `false` = off; anything else, or
unset, = on). Every drop is logged for the admin F13_autofix flag; a candidate refused by the floor is logged too.
"""
from __future__ import annotations

import difflib
import os
from typing import Optional

from services.acp_content_writing import hygiene
from services.acp_content_writing import length_contract as lc
from shared import settings

MODE_ENV = "T9_REPEAT_DROP"
_OFF = frozenset({"off", "0", "false", "no"})
NEAR_EXACT_SETTING = "cis.t9.repeat_drop_near_exact"   # S279 thresholds L1: Platform › Settings (evidence required)
NEAR_EXACT_SHARE = float(settings.definition(NEAR_EXACT_SETTING).default_value)   # the compiled default (0.9)


def near_exact_share() -> float:
    """The Dice share in force (setting `cis.t9.repeat_drop_near_exact`)."""
    return float(settings.get(NEAR_EXACT_SETTING))
_MIN_COPY_WORDS = 4      # hygiene._COPY_MIN_WORDS: a short line is never judged a copy
_MAX_DROPS = 5           # one drop per pass (offsets move); a piece with more repeats is not this step's case


def enabled() -> bool:
    return (os.environ.get(MODE_ENV) or "").strip().lower() not in _OFF


def near_exact(later: str, earlier: str, ignore: frozenset = frozenset(), share: Optional[float] = None) -> bool:
    """`later` repeats `earlier` exactly or nearly: >= 4 copy words each, and the ordered Dice share of their copy
    words (2 x matched / (len a + len b), difflib matching blocks) >= `share`. Pure."""
    ca, cb = hygiene.copy_words(later, ignore), hygiene.copy_words(earlier, ignore)
    if len(ca) < _MIN_COPY_WORDS or len(cb) < _MIN_COPY_WORDS:
        return False
    matched = sum(m.size for m in difflib.SequenceMatcher(None, ca, cb, autojunk=False).get_matching_blocks())
    return 2.0 * matched / (len(ca) + len(cb)) >= (near_exact_share() if share is None else share)


def _quoted(finding: str) -> str:
    """The sentence an F13 repeat finding quotes ("<problem>: '<sentence[:200]>'")."""
    return finding.rsplit(": '", 1)[-1][:-1] if finding.endswith("'") else finding.rsplit(": '", 1)[-1]


def _cut(text: str, span: dict) -> str:
    end = span["end"] + 1 if text[span["end"]:span["end"] + 1] == " " else span["end"]
    return hygiene.tidy_after_cut(text[:span["start"]] + text[end:])


def drop_repeats(text: str, hyg: hygiene.HygieneContext, *, floor: int = lc.BLOG_FLOOR_CHARS,
                 share: Optional[float] = None) -> tuple[str, list[str]]:
    """Delete the later copy of each exact / near-exact repeat the F13 gate would quote, while the piece stays at or
    above `floor` and every invariant holds. Returns (text, log). Pure (no I/O)."""
    if not text or hyg is None:
        return text, []
    share = near_exact_share() if share is None else share
    cta = hyg.cta
    ignore = hygiene.brand_words(hyg.brand_name)
    seed = hygiene.source_forms(hyg.source_text)
    log: list[str] = []
    refused: set[str] = set()
    for _ in range(_MAX_DROPS):
        quoted = {_quoted(f) for f in hygiene.find_issues(text, hyg)["repeat"]}
        if not quoted:
            break
        spans = hygiene._dup_spans(text, cta)
        dropped = False
        for j, span in enumerate(spans):
            clean = span["clean"]
            if clean[:200] not in quoted or clean in refused:
                continue
            if not any(near_exact(clean, t["clean"], ignore, share) for t in spans[:j] if t["end"] <= span["start"]):
                continue
            after = _cut(text, span)
            chars = lc.measure(after)
            if chars < floor:
                refused.add(clean)
                log.append(f"kept a near-exact repeat: dropping it leaves {chars} chars, under the blog floor of "
                           f"{floor:,}: '{clean[:160]}'")
                continue
            if (not hygiene._dup_safe_delete(text, span) or not hygiene._keeps_facts(after, clean, seed)
                    or hygiene.integrity_problems(text, after, ignore, cta)):
                refused.add(clean)
                continue
            text = after
            log.append(f"dropped a near-exact repeat of an earlier sentence ({chars} chars left): '{clean[:160]}'")
            dropped = True
            break
        if not dropped:
            break
    return text, log


__all__ = ["MODE_ENV", "NEAR_EXACT_SETTING", "NEAR_EXACT_SHARE", "drop_repeats", "enabled", "near_exact",
           "near_exact_share"]
