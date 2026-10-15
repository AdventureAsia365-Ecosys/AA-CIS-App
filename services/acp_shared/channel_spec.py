"""AA-771 (S234) — channel spec by brand register, and the deterministic voice rules T8, T9 and T10
share.

S233 panel: brand voice 5.9 on LinkedIn (Trail Pulse 4.33). Three causes, all deterministic to fix:
- the shared LinkedIn row said "B2B and partner audiences" and "calm" for every brand, so a young,
  energetic consumer brand wrote calm essays to other travel operators;
- the T8 angle brief set a tone and pre-wrote the opening ("Calm, considered LinkedIn voice",
  "Open with …"), and the writer follows the brief;
- nothing bounded LinkedIn length, hashtags or the first line (S233: mean 369 words, 7/12 posts
  with more than 3 hashtags, first lines up to 391 characters).

This module owns:
- `brand_register(pack)` — one of 4 registers, from the voice adjectives first and the free-text
  brand_type second (the 4 pilot tenants: Trail Pulse consumer_social, Atlas & Hearth editorial,
  Terra warm_practical, WildKind observational);
- `linkedin_spec(pack)` / `channel_spec(channel, pack)` — use case, structure, avoid list, the
  gate word band, the (lower) prompt word target, the hashtag cap and the first-line limit. Only
  LinkedIn has a spec; every other channel keeps its `channel_style` row;
- `sanitize_best_final_style(text, pack)` — fixes, in code, the parts of an angle brief that
  set a tone on the opposite side of the brand voice, prescribe the opening ('Open with X' →
  'Lead with X'; a literal first line is cut), or (for a non-editorial brand) address or speak as
  the travel trade. Used before an angle is saved (T8) and on
  stored angles when a non-blog prompt is built (T9), so old requests get it at $0;
- the stock-phrase hard list, the trade-register pattern (TRADE_PATTERN / B2B_RE) and the
  generic-opener pattern read by the T9 prompt and the F15_voice / F15_voice_warn gates.

Pure: no DB, no LLM.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------- registers

CONSUMER_SOCIAL = "consumer_social"
EDITORIAL = "editorial"
WARM_PRACTICAL = "warm_practical"
OBSERVATIONAL = "observational"
REGISTERS = (CONSUMER_SOCIAL, EDITORIAL, WARM_PRACTICAL, OBSERVATIONAL)

# Voice axes. Each axis has two poles; a brand sits on the pole most of its voice adjectives name
# (no pole when it names neither or both equally). An angle brief may not prescribe the other pole.
_AXES: dict[str, tuple[frozenset, frozenset]] = {
    "energy": (
        frozenset({"energetic", "fresh", "playful", "lightly playful", "lively", "bold", "dynamic", "fun",
                   "upbeat", "punchy", "vibrant", "exciting", "spirited", "high-energy"}),
        frozenset({"calm", "considered", "quiet", "quietly", "measured", "unhurried", "contemplative",
                   "reflective", "restrained", "understated", "serene", "composed", "subdued", "sober",
                   "slow", "hushed", "meditative"}),
    ),
    "warmth": (
        frozenset({"warm", "reassuring", "friendly", "encouraging", "family-aware", "caring", "approachable",
                   "welcoming", "gentle", "supportive"}),
        frozenset({"authoritative", "quietly authoritative", "detached", "clinical", "corporate", "essayistic",
                   "academic", "analytical", "austere", "scholarly", "cerebral", "aloof", "dry"}),
    ),
    "formality": (
        frozenset({"casual", "conversational", "playful", "lightly playful", "chatty", "relaxed", "informal",
                   "breezy", "cheeky"}),
        frozenset({"formal", "professional", "corporate", "polished", "businesslike", "institutional"}),
    ),
}
TONE_WORDS = frozenset(w for pos, neg in _AXES.values() for w in pos | neg)


def fold(text: Optional[str]) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


def _voice(pack: Optional[dict]) -> set[str]:
    return {fold(v).strip() for v in (pack or {}).get("voice") or [] if str(v).strip()}


def brand_poles(pack: Optional[dict]) -> dict[str, int]:
    """{axis: 0 (first pole) | 1 (second pole)} for the axes the brand voice decides."""
    voice = _voice(pack)
    out: dict[str, int] = {}
    for axis, (first, second) in _AXES.items():
        a, b = len(voice & first), len(voice & second)
        if a != b:
            out[axis] = 0 if a > b else 1
    return out


def off_voice_words(pack: Optional[dict]) -> frozenset:
    """Tone words on the opposite pole of the brand voice ('calm' for Trail Pulse, 'authoritative'
    for Terra). A word the brand's own voice uses is never off-voice."""
    voice = _voice(pack)
    bad: set[str] = set()
    for axis, pole in brand_poles(pack).items():
        bad |= _AXES[axis][1 - pole]
    return frozenset(bad - voice)


_ENERGY_HIGH = _AXES["energy"][0]


def brand_register(pack: Optional[dict]) -> str:
    """consumer_social when the voice is energetic/playful or the brand_type is young/active/social;
    editorial for luxury/premium/cultural brands that are not family brands; warm_practical for
    family brands; observational otherwise. Deterministic — the voice adjectives decide first."""
    voice = _voice(pack)
    bt = fold((pack or {}).get("brand_type"))
    if voice & _ENERGY_HIGH or re.search(r"\b(young|youth|active|social)\b", bt):
        return CONSUMER_SOCIAL
    if re.search(r"\b(luxury|premium|cultural|culture)\b", bt) and "family" not in bt:
        return EDITORIAL
    if "family" in bt or "families" in bt:
        return WARM_PRACTICAL
    return OBSERVATIONAL


# ---------------------------------------------------------------- the LinkedIn spec

@dataclass(frozen=True)
class ChannelSpec:
    channel: str
    register: str
    use_when: str
    structure: str
    avoid: str
    words: tuple[int, int]          # the F14 gate band
    target_words: tuple[int, int]   # what the prompt asks for (below the band: LLMs overshoot counts)
    hashtags_max: int
    first_line_max: int

    @property
    def block_words(self) -> int:
        """F14 blocks (full rewrite) only above this — 1.3 × the band's top."""
        return int(self.words[1] * 1.3)


# Shown to the writer as things to avoid; F15_voice checks the same list (STOCK_PATTERNS). No commas
# inside an item: ranking.split_avoid_phrases splits the avoid list on commas.
STOCK_PHRASES = (
    "worth sitting with", "worth paying attention to", "difficult to overstate", "not incidental",
    "not by accident", "tells you something", "says something about", "speaks volumes", "here's the thing",
    "in a world where", "Not as X. Not as Y.",
)
STOCK_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\bworth sitting with\b",
    r"\bworth paying attention to\b",
    r"\b(?:difficult|hard) to overstate\b",
    r"\bit is argument\b",
    r"\bnot incidental\b",
    r"\b(?:not|never|wasn'?t|isn'?t|did not|didn'?t) (?:\w+ ){0,4}by accident\b"
    r"|\b(?:it'?s|it is|it was|is|was|this is|that is) no accident\b",
    r"\btells you something (?:about|important)\b",
    r"\bsays something about\b",
    r"\bspeaks volumes\b",
    r"\bhere'?s the thing\b",
    r"\bNot as [^.!?\n]{1,40}\.\s+Not as\b",
    r"\bin a world (?:where|of)\b",
))
# A first sentence that starts "There is a …" / "There's a …" (S232: 20/29 openers; S233: 6/12).
GENERIC_OPENER_RE = re.compile(r"^\s*There(?:\s+is|'s|’s|\s+was)\s+(?:a|an|one)\b", re.I)
# Writing to the trade instead of travellers (S233: Trail Pulse 3/3 'active travel brands', Terra 'a
# thoughtful operator reflecting on trip design', Trail Pulse 'how they plan or recommend active travel',
# '…before you put travellers in it'). Shared by the F15 trade-register warn (post text) and the
# brief sanitiser (T8/T9). Only phrases that ADDRESS or SPEAK AS the trade — never a bare 'operators',
# 'planners' or 'advisors', which are trip content ('local boat operators run the crossing') or
# traveller-facing ('parents and family trip planners').
TRADE_PATTERN = (
    r"\b(?:active |adventure |other )?(?:travel|tourism) (?:brands?|companies|professionals?|practitioners?"
    r"|planners?|advisors?|agents?|operators?|industry|trade|peers)\b"
    r"|\b(?:fellow|other) (?:tour |travel |trip )?(?:operators?|brands?|planners?|practitioners?)\b"
    r"|\b(?:as|like) an? (?:\w+ )?(?:operator|practitioner)\b"
    r"|\b(?:thoughtful|experienced|seasoned|savvy|smart|good) (?:tour |travel |trip )?(?:operators?|practitioners?)\b"
    r"|\btour operators? (?:like you|who (?:plan|sell|design|recommend))\b"
    r"|\bprofessionals? (?:planning|designing|selling|recommending|who plan|in (?:the )?(?:travel|tourism))\b"
    r"|\b(?:recommend|sell|package)(?:s|ing)? (?:active |adventure |family |group )?"
    r"(?:travel|trips|itineraries|tours)\b"
    r"|\bplan(?:s|ning)? or (?:recommend|sell|design)\w*\b"
    r"|\bput(?:ting)? (?:travell?ers|guests|clients|groups) (?:in|on|into|through)\b"
    r"|\byour (?:clients|customers)\b"
    r"|\bif you (?:work|operate|design|recommend|sell|run)\b"
    r"|\b(?:industry|trade) (?:peers|audience|readers|professionals?)\b|\bpeers in\b|\bB2B\b"
)
B2B_RE = re.compile(TRADE_PATTERN, re.I)

_LINKEDIN_AVOID = (
    "many emojis, generic travel copy, hard sell, long itinerary lists, hidden gem, bucket list, paradise awaits, "
    "markdown formatting, contrast frames (isn't X but Y), " + ", ".join(STOCK_PHRASES)
)
_TRAVELLER_FACING = ("traveller-facing: write to people who might take this trip (the brand's Audience line), "
                     "never to travel professionals, operators or other brands")

_REGISTER_ROWS: dict[str, dict] = {
    CONSUMER_SOCIAL: {
        "use_when": f"Social proof of a trip worth doing, in the brand's own energetic voice; {_TRAVELLER_FACING}",
        "structure": ("One-sentence hook → 2-4 short paragraphs built on concrete details from the source → what "
                      "it means for the people on the trip → light, direct CTA → hashtags"),
        "words": (120, 250),
    },
    EDITORIAL: {
        "use_when": ("Considered, insight-led positioning for the brand's own audience (its Audience line): "
                     "one clear idea about the place"),
        "structure": ("One-sentence hook → short paragraphs → one clear insight from the source → at most one "
                      "sentence on how the brand approaches it (only what its brand identity states) → light CTA"),
        "words": (120, 300),
    },
    WARM_PRACTICAL: {
        "use_when": f"A practical, reassuring look at one moment of the trip; {_TRAVELLER_FACING}",
        "structure": ("One-sentence hook → short paragraphs on what the moment is and what it asks of the "
                      "people on it, from the source → one reassuring practical point → light CTA"),
        "words": (120, 250),
    },
    OBSERVATIONAL: {
        "use_when": f"A place-led observation about one moment of the trip; {_TRAVELLER_FACING}",
        "structure": ("One-sentence hook → short paragraphs built on what the source says about the place → "
                      "why it matters to the brand's audience → light CTA"),
        "words": (120, 250),
    },
}
LINKEDIN_TARGET_WORDS = (150, 220)
HASHTAGS_MAX = 3
FIRST_LINE_MAX = 140


def linkedin_spec(pack: Optional[dict]) -> ChannelSpec:
    register = brand_register(pack)
    row = _REGISTER_ROWS[register]
    return ChannelSpec(channel="linkedin", register=register, use_when=row["use_when"], structure=row["structure"],
                       avoid=_LINKEDIN_AVOID, words=row["words"], target_words=LINKEDIN_TARGET_WORDS,
                       hashtags_max=HASHTAGS_MAX, first_line_max=FIRST_LINE_MAX)


def channel_spec(channel: Optional[str], pack: Optional[dict]) -> Optional[ChannelSpec]:
    """The numeric spec for `channel`, or None (every channel but LinkedIn today)."""
    return linkedin_spec(pack) if channel == "linkedin" else None


def apply_spec(channel_style: dict, spec: Optional[ChannelSpec]) -> dict:
    """The channel_style row with the spec's register-specific use case, structure and avoid list."""
    if spec is None:
        return channel_style
    return {**channel_style, "use_when": spec.use_when, "structure": spec.structure, "avoid": spec.avoid}


# ---------------------------------------------------------------- shape helpers

_WORD_RE = re.compile(r"[A-Za-z0-9’'][A-Za-z0-9’'-]*")
HASHTAG_RE = re.compile(r"(?<![\w#])#[A-Za-z][A-Za-z0-9_]*")


def post_words(text: Optional[str]) -> int:
    """Words of a social post, hashtags left out."""
    return len(_WORD_RE.findall(HASHTAG_RE.sub(" ", text or "")))


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'‘’“”])")


def first_sentence(text: Optional[str]) -> str:
    head = (text or "").strip().split("\n", 1)[0].strip()
    return _SENT_SPLIT_RE.split(head, 1)[0].strip() if head else ""


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def opener_overlap(a: str, b: str) -> float:
    """Token Jaccard of two first sentences."""
    ta, tb = set(_TOKEN_RE.findall(fold(a))), set(_TOKEN_RE.findall(fold(b)))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


# ---------------------------------------------------------------- angle brief sanitiser

_TONE_CUE_RE = re.compile(r"\b(?:tone|voice|register|mood)\b", re.I)
_NEGATED_RE = re.compile(r"\b(?:no|not|never|without|avoid|nor|rather than|instead of)\s+(?:too\s+|overly\s+|a\s+)?"
                         r"[\w-]+(?:\s+[\w-]+)?", re.I)
# 'Open with X' / 'Start on X' / 'Do not begin with X' — the verb and its preposition (S234 review:
# the sentence names WHICH seed detail to lead with, so it is rewritten to 'Lead with X', not cut).
_PRE = (r"(?P<pre>\s*(?:(?:please|then|so|and|first|always|simply|immediately|instead|do|does|not|don'?t|don’t"
        r"|never)\s+){0,3})")
_OPEN_RE = re.compile(_PRE + r"(?P<verb>open|opens|start|starts|begin|begins)(?P<mid>\s+(?:[\w'’]+\s+){0,2}?)"
                      r"(?P<prep>with|on|by)\b", re.I)
# 'Open: X' / 'Start — X'.
_OPEN_COLON_RE = re.compile(_PRE + r"(?:open|opening|start|begin)\s*[:—–-]\s*", re.I)
# Text that dictates the actual first line — a literal line, quoted or after 'Opening line:' — is cut.
_FIRST_LINE_RE = re.compile(r"^\s*(?:[\w'’]+\s+){0,2}?(?:opening|first)\s+(?:line|sentence|words?)\b\s*[:—–-]", re.I)
_LITERAL_RE = re.compile(r"^\s*(?:(?:a|the)\s+(?:line|sentence|words?)\b(?:\s+(?:like|such as))?\s*[:,]?\s*)?[\"“‘']",
                         re.I)
_NEG_PRE_RE = re.compile(r"\b(?:not|never|n't|don'?t)\b", re.I)
_AUDIENCE_RE = B2B_RE
_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_RE = re.compile(r"\s+[—–]\s+|;\s+|\s+-\s+")
_THEN_RE = re.compile(r",?\s+(?:and\s+)?then\s+", re.I)
_FALLBACK_BRIEF = "Lead with the most concrete details from the content seed."


def _words_in(clause: str) -> set[str]:
    text = _NEGATED_RE.sub(" ", fold(clause))
    found = set(re.findall(r"[a-z]+(?:-[a-z]+)?", text))
    pairs = re.findall(r"(?=\b([a-z]+ [a-z]+)\b)", text)
    return found | set(pairs)


def _is_tone_clause(clause: str) -> bool:
    """'Tone: calm, …', 'Calm, considered LinkedIn voice', 'Warm and direct.' — a tone cue word, or a
    clause that starts with a tone adjective. 'Lead with the calm lake' is content, not tone."""
    if _TONE_CUE_RE.search(clause):
        return True
    first = re.match(r"\s*(?:(?:keep it|be|write in an?|stay|sound)\s+)?([A-Za-z-]+)", clause, re.I)
    return bool(first and fold(first.group(1)) in TONE_WORDS)


def _cap(s: str) -> str:
    s = s.strip()
    return s[0].upper() + s[1:] if s else s


def _finish(s: str) -> str:
    s = s.strip().rstrip(",;:—–- ")
    return s if not s or s[-1] in ".!?\"'”’" else s + "."


def _opening(sentence: str) -> Optional[tuple[str, str]]:
    """None when `sentence` does not prescribe the opening; otherwise (what is kept, what was cut).
    - a literal first line ('Opening line: …', 'Open with "…"') is cut — a following ', then …'
      part is kept;
    - 'Open/Start/Begin with|on X' keeps X as 'Lead with X' (the brief names WHICH detail leads);
      'Do not open with X' becomes 'Do not lead with X', so the framing guard survives."""
    m = _OPEN_RE.match(sentence) or _OPEN_COLON_RE.match(sentence)
    literal = bool(_FIRST_LINE_RE.match(sentence)) or bool(m and _LITERAL_RE.match(sentence[m.end():]))
    if literal:
        head, *rest = _THEN_RE.split(sentence, 1)
        if rest and rest[0].strip():
            return _finish(_cap(rest[0])), head.strip()
        return "", sentence.strip()
    if m is None:
        return None
    pre = m.group("pre")
    if _NEG_PRE_RE.search(pre):
        lead = ("Never lead " if re.search(r"\bnever\b", pre, re.I) else "Do not lead ")
    else:
        lead = "Lead "
    prep = "by" if (m.groupdict().get("prep") or "").lower() == "by" else "with"
    rest = sentence[m.end():].lstrip() if m.re is _OPEN_COLON_RE else sentence[m.end():]
    return lead + prep + ("" if rest.startswith(" ") else " ") + rest, sentence[:m.end()].strip()


def sanitize_best_final_style(text: Optional[str], pack: Optional[dict]) -> tuple[str, list[str]]:
    """Strip from an angle brief:
    (a) a tone clause with a word on the opposite pole of the brand voice — the whole sentence when
        it leads the sentence ('Tone: calm, considered, quietly authoritative — the voice of …' for
        Terra); negated words ('no hype') are ignored, and 'Tone: calm, intelligent' stays for a
        calm brand;
    (b) the opening instruction: 'Open/Start/Begin with X' becomes 'Lead with X' (X is the seed
        detail the brief means to lead with), 'Do not open with X' becomes 'Do not lead with X', and a
        literal first line ('Opening line: …', 'Open with "…"') is cut, keeping a following ', then …';
    (c) for a non-editorial brand, a sentence addressing or speaking as the travel trade
        (TRADE_PATTERN — never a bare 'operators' / 'planners', which are trip content).
    Returns (clean brief, stripped parts). Never returns an empty brief."""
    if not text or not text.strip():
        return text or "", []
    bad = off_voice_words(pack)
    non_editorial = brand_register(pack) != EDITORIAL
    kept_sentences: list[str] = []
    stripped: list[str] = []
    for sentence in [s for s in _SENT_RE.split(text.strip()) if s.strip()]:
        opening = _opening(sentence)
        if opening is not None:
            sentence, cut = opening
            stripped.append(cut)
            if not sentence:
                continue
        if non_editorial and _AUDIENCE_RE.search(sentence):
            stripped.append(sentence.strip())
            continue
        clauses = _CLAUSE_RE.split(sentence)
        off = [i for i, c in enumerate(clauses) if _is_tone_clause(c) and (_words_in(c) & bad)]
        if 0 in off:  # the sentence is a tone line; what follows the dash elaborates it
            stripped.append(sentence.strip())
            continue
        stripped += [clauses[i].strip() for i in off]
        kept = [c for i, c in enumerate(clauses) if i not in off]
        if kept:
            kept_sentences.append(_finish(_cap("; ".join(c.strip() for c in kept))) if off else sentence.strip())
    if not stripped:
        return text, []
    clean = " ".join(s for s in kept_sentences if s.strip(" .")) or _FALLBACK_BRIEF
    return clean, stripped


__all__ = [
    "B2B_RE", "CONSUMER_SOCIAL", "ChannelSpec", "EDITORIAL", "FIRST_LINE_MAX", "GENERIC_OPENER_RE", "HASHTAGS_MAX",
    "HASHTAG_RE", "LINKEDIN_TARGET_WORDS", "OBSERVATIONAL", "REGISTERS", "STOCK_PATTERNS", "STOCK_PHRASES",
    "TRADE_PATTERN",
    "TONE_WORDS", "WARM_PRACTICAL", "apply_spec", "brand_poles", "brand_register", "channel_spec", "first_sentence",
    "linkedin_spec", "off_voice_words", "opener_overlap", "post_words", "sanitize_best_final_style",
]
