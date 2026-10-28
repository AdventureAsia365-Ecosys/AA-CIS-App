"""AA-790 round 9 (S256) — ground the T8 angle brief in the content seed before the T9 writer reads it. $0, no LLM.

Rounds 7 and 8 both failed the blind panel, and in both the writer followed the angle brief over the seed: round 7
found the family-brand LinkedIn closer "… a structured, accessible stop" (20 of 42 posts) copied from a brief that asked
for a reassuring note the seed cannot give; in round 8 the source-fidelity losses the judges quoted are the brief's own
asks ("the family watches students at work … crafts being practised, not demonstrated for visitors" → "your family
can visit while that work is underway"; "a four-pair FAQ addressing chorten meaning" → a whole FAQ the seed does not
support). T8's prompt already forbids a brief that names a scene, object, number or promise the seed lacks (the
round-6 bullet); the model still writes them. Rounds 4-8 answered with more instructions; round 9 removes the cause:
before the brief reaches the writer, every clause of it that asks for content the seed (+ its FACTS section) does not
contain is dropped in code.

What a clause is: the brief split into sentences (and lines), each sentence split at ' — ', '; ' and ', then '. A
clause is kept when at most one of its content words is unsupported; it is dropped when two or more are. A word is
supported when one of its forms (singular candidates, or a shared stem of at least five letters) is a word of the
seed + FACTS, or when it is brief vocabulary (``BRIEF_WORDS``: the words a brief uses to talk about the piece, its
reader and its shape: section, FAQ, lead, reader, family, day …). A count in a shape clause ("900–1,100 words",
"3–4 pairs", "H2 sections") is shape, not a figure; any other number must be in the seed. So:

  kept     "Lead with the turquoise travertine pools"               (every word in the seed)
  kept     "Three H2 sections in day order, a CTA paragraph, then a four-pair FAQ."   (shape)
  dropped  "the guide unrolling a map"                              (guide, unrolling, map: not in the seed)
  dropped  "Tone is reassuring but not soft"                        (tone, reassuring, soft: the BRAND block sets tone)
  dropped  "crafts being practised, not demonstrated for visitors"  (a scene the seed does not give)

A dropped clause that carried a part label ("Lead with: …") hands the label to the next kept clause of its sentence.
When nothing is left, the brief is ``FALLBACK_BRIEF`` (the same line channel_spec uses).

Mode switch: api/worker env ``T9_BRIEF_GROUNDING_MODE`` = ``r9`` turns it on; unset (default) or any other value is
``off`` and the writer prompt is byte for byte the round-6 prompt (test_aa790_round9_brief_grounding pins a 192-prompt
fixture hash). The A/B harness passes the mode explicitly (``build_user_prompt(brief_grounding=...)``), so both arms
run on one deployed image with the env unset. Only the brief the writer READS changes: the stored angle, the angle
picker, the AI-tell context (``ai_tells.angle_text``) and the brief-copy check (``hygiene.brief_phrases``) still see
the full brief, so a writer that copies a dropped clause is still caught.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from services.acp_content_writing.hygiene import _STOP, _forms, fold, source_forms

MODE_ENV = "T9_BRIEF_GROUNDING_MODE"
MODE_OFF = "off"
MODE_R9 = "r9"
MODES = (MODE_OFF, MODE_R9)

FALLBACK_BRIEF = "Lead with the most concrete details from the content seed."
# A clause is dropped when at least DROP_MIN of its content words are unsupported and they are at least DROP_SHARE
# of them, or when DROP_ANY are unsupported whatever its length (a long list clause with one paraphrase stays).
DROP_MIN = 2
DROP_SHARE = 0.2
DROP_ANY = 5
STEM_MIN = 5          # 'restoring' ~ 'restoration', 'mastery' ~ 'mastering'

# The words a brief uses about the piece itself, its reader and its shape. Never facts, so never "unsupported".
# Closed list; tone, mood and thesis words are deliberately absent (the BRAND block sets the voice, R6/R8).
BRIEF_WORDS = frozenset("""
reader readers traveller travellers traveler travelers family families parent parents child children kid kids
guest guests audience brand voice angle lead leads leading led shape structure structured section sections heading
headings faq faqs pair pairs question questions answer answers answered cta call action paragraph paragraphs intro
introduction hook insight close closing closes end ends ending open opens opening order ordered day days word words
long short length piece post posts blog linkedin article guide pillar page body title meta line lines sentence
sentences first second third fourth last final then each every one two three four five six seven eight nine ten
per across follow follows followed move moves moving through toward towards visit visits visiting see sees seeing
trip route journey stop stops moment moments detail details fact facts concrete specific seed itinerary content
note notes mention mentions name names named use uses used carry carries carried give gives given build builds
built around focus focuses point points plus before after between within keep keeps include includes including
main central core part parts single brief briefly once own same other next where what how why which who later
earlier start starts begin begins middle wider broader full whole bullet bullets list hashtag hashtags format
clear major minor form draw draws drawn drawing cover covers covering pointing tied emphasise emphasize
arrive arrives arriving reach reaches reaching continue continues continuing travel travels travelling spend spends
spending stay stays staying sleep sleeps leave leaves return returns returning head heads onward onwards
""".split())
# Function words the stop list does not carry; they never make a clause unsupported.
FUNCTION_WORDS = frozenset("""without both any out another way things thing else whose rather either neither much many
less few rest instead via upon whether while also already still even""".split())

_SENT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE_RE = re.compile(r"(\s+[—–]\s+|;\s+|,?\s+(?:and\s+)?then\s+)")  # captured: a dash is an aside
_DASH_RE = re.compile(r"\s+[—–]\s+")
_LABEL_RE = re.compile(r"^\s*([A-Z][A-Za-z ]{1,20}?):\s+")
# A count that belongs to the shape ("900–1,100 words", "3-4 pairs", "a 3-question FAQ", "four H2 sections").
_SHAPE_COUNT_RE = re.compile(
    r"\b\d[\d,]*(?:\s*[–-]\s*\d[\d,]*)?\s*-?\s*(?:FAQ\s+)?"
    r"(?:words?|pairs?|questions?|sections?|paragraphs?|sentences?|lines?|headings?|bullets?|hashtags?|characters?"
    r"|items?|H[1-6]s?)\b", re.I)
_HEADING_TAG_RE = re.compile(r"\bH[1-6]s?\b")
_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")  # '1,283' is one number, '20,000' too


def brief_grounding_mode(value: Optional[str] = None) -> str:
    """The mode: `value` when given (the A/B harness), else the env switch. Only 'r9' turns round 9 on; every other
    value, unset included, is 'off'."""
    raw = os.environ.get(MODE_ENV, "") if value is None else value
    return MODE_R9 if (raw or "").strip().lower() == MODE_R9 else MODE_OFF


def _stems(forms: frozenset) -> frozenset:
    """The five-letter stems of the words of six letters or more ('restoring' and 'restoration' share 'resto')."""
    return frozenset(f[:STEM_MIN] for f in forms if len(f) > STEM_MIN and not f.isdigit())


class Source:
    """What a brief clause is checked against: the seed + FACTS words (``hygiene.source_forms``), their stems and
    every number written in them (source_forms keeps words of three letters or more only, so '29', '7' and '14' are
    read here)."""

    def __init__(self, text: Optional[str]):
        self.forms = source_forms(text or "")
        self.stems = _stems(self.forms)
        self.numbers = frozenset(re.findall(r"\d+", _THOUSANDS_RE.sub("", fold(text or ""))))
        self.long_forms = tuple(f for f in self.forms if len(f) > STEM_MIN + 2)

    def supports(self, token: str) -> bool:
        if token.isdigit():
            return token in self.numbers
        forms = _forms(token)
        if any(f in self.forms or f in BRIEF_WORDS or f in FUNCTION_WORDS for f in forms):
            return True
        if any(f[:STEM_MIN] in self.stems for f in forms if len(f) > STEM_MIN):
            return True
        # 'night' in 'overnight', 'boat' in 'boatman': a word of five letters or more inside one seed word
        return len(token) >= STEM_MIN and any(token in f for f in self.long_forms)


def _clause_words(clause: str) -> list[str]:
    """The content words of a brief clause, in order: shape counts and heading tags removed, stop words and words of
    two letters or fewer dropped (a number of any length stays)."""
    text = _HEADING_TAG_RE.sub(" ", _SHAPE_COUNT_RE.sub(" ", clause))
    tokens = _TOKEN_RE.findall(_THOUSANDS_RE.sub("", fold(text)))
    return [t for t in tokens if t not in _STOP and (len(t) > 2 or t.isdigit())]


def unsupported_words(clause: str, source: "Source | str | None") -> list[str]:
    """The content words of `clause` the seed + FACTS do not carry, in order (pure). A number counts unless the seed
    has it; a count in a shape ("3–4 FAQ pairs", "900 words") is shape and never counts."""
    src = source if isinstance(source, Source) else Source(source)
    return [t for t in _clause_words(clause) if not src.supports(t)]


def is_grounded(clause: str, source: "Source | str | None") -> bool:
    """Keep a clause unless it asks for content the seed lacks (see DROP_MIN / DROP_SHARE / DROP_ANY)."""
    src = source if isinstance(source, Source) else Source(source)
    words = _clause_words(clause)
    missing = [t for t in words if not src.supports(t)]
    if len(missing) >= DROP_ANY:
        return False
    return not (len(missing) >= DROP_MIN and len(missing) >= DROP_SHARE * len(words))


def _finish(s: str) -> str:
    s = s.strip().rstrip(",;:—–- ")
    if not s:
        return s
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?\"'”’" else s + "."


def ground_brief(brief: Optional[str], source: Optional[str]) -> tuple[str, list[str]]:
    """(the grounded brief, the dropped clauses) — pure. `source` is the seed text the writer gets (with its FACTS
    section). An empty brief comes back unchanged; a brief with nothing left becomes ``FALLBACK_BRIEF``."""
    if not brief or not brief.strip():
        return brief or "", []
    src = Source(source)
    kept_sentences: list[str] = []
    dropped: list[str] = []
    for sentence in [s.strip() for s in _SENT_RE.split(brief.strip()) if s.strip()]:
        kept: list[str] = []
        carry = ""
        prev_dropped = False
        parts = _CLAUSE_RE.split(sentence)  # [clause, separator, clause, separator, …]
        for k in range(0, len(parts), 2):
            clause = parts[k].strip()
            if not clause:
                continue
            after_dash = k > 0 and bool(_DASH_RE.fullmatch(parts[k - 1]))
            label = _LABEL_RE.match(clause)
            body = clause[label.end():] if label else clause
            if not body.strip(" .,:;"):
                carry = clause.strip()  # a bare label ('Structure:') waits for its first kept clause
                continue
            # an aside after a dash that goes on from a dropped clause ('… the idea of convergence — geographic,
            # ecological, and cultural — as …', '… the weight of the date — 1937 — …') goes with it: on its own
            # it is a fragment. A list item after ';' or ', then' is judged on its own.
            continues = prev_dropped and after_dash and not label and not body.lstrip()[:1].isupper()
            if continues or not is_grounded(body, src):
                dropped.append(clause.rstrip(" .;"))
                prev_dropped = True
                if label and not carry:
                    carry = label.group(0).strip()
                continue
            prev_dropped = False
            if carry and not label:
                clause = f"{carry} {clause}"
            carry = ""
            kept.append(clause.rstrip(" ;"))
        if kept:
            kept_sentences.append(_finish("; ".join(kept)))
    if not dropped:
        return brief, []
    return " ".join(kept_sentences) or FALLBACK_BRIEF, dropped


def ground_angle(angle: dict, source: Optional[str], mode: Optional[str] = None) -> dict:
    """`angle` with its best_final_style grounded when the mode is 'r9' (`mode` None reads the env switch); the same
    dict object otherwise, so the 'off' prompt is built from exactly what it was built from before."""
    if brief_grounding_mode(mode) != MODE_R9:
        return angle
    grounded, dropped = ground_brief(angle.get("best_final_style"), source)
    if not dropped:
        return angle
    return {**angle, "best_final_style": grounded}


__all__ = ["BRIEF_WORDS", "DROP_ANY", "DROP_MIN", "DROP_SHARE", "FALLBACK_BRIEF", "FUNCTION_WORDS", "MODES",
           "MODE_ENV", "MODE_OFF", "MODE_R9", "Source", "brief_grounding_mode", "ground_angle", "ground_brief",
           "is_grounded", "unsupported_words"]
