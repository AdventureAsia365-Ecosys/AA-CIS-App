"""AA-795 (report 12 F-g) — brand-claim fidelity check of a T2 tenant version against its A1 source.

The T2 judge scores brand fit, and S234 found passing versions that earn it with brand claims the source never
makes: Terra "children aged seven to sixteen", WildKind "consent-based community visit". A claim like that promises
something about THIS tour (who it suits, how a visit is run, who owns the guesthouse), so it must come from the A1
text the T2 writer read, never from the brand's own story.

Deterministic, no LLM call. Three kinds of claim, each checked against the source text:
  * ``age``         an age range or limit next to a person noun ("children aged 7-16", "ages 12+", "under-12s");
                    supported when the source states an age with the same number(s);
  * ``group_size``  a group-size promise ("small groups of up to 12", "no more than 8 guests"); supported when the
                    source states a group size with the same number;
  * ``ethos``       a brand-ethos / policy / certification term from ``ETHOS_TERMS`` ("consent-based",
                    "carbon-neutral", "women-led", "certified"); supported when the source has the same term
                    (case, hyphen and spacing folded).

Report-only (backlog L1: no new gates for 4 weeks): ``check`` returns the claims the source does not support; the T2
job stores them in ``tenant_tour_versions.judge_breakdown.brand_claims`` for the admin, and nothing else changes.
``in_brand_pack`` says the term is in the tenant's own brand pack (the likely origin: the writer carried the brand's
story into the tour). The patterns prefer missing a claim to flagging background ("a 300-year-old temple" is not an
age claim: no person noun, and ages above 99 are never read).
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Optional

VERSION = "bc_v1"

# The fields of a T2 version a reader sees (rewritten_content keys, t2_rewrite_job). `trip_type` is a label.
_FIELDS = ("name", "subtitle", "summary", "highlights", "itineraries", "seo_title", "seo_meta")

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "twenty-one": 21, "twenty one": 21, "twenty-five": 25,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
}
# Whole tokens only: "300" is never read as "30", and "anyone" / "often" never yield "one" / "ten".
_NUM = (r"(?:(?<![\w])\d{1,2}(?!\d)|(?<![a-z])(?:"
        + "|".join(sorted((re.escape(w) for w in _NUM_WORDS), key=len, reverse=True)) + r")(?![a-z]))")
_RANGE = rf"{_NUM}(?:\s*(?:-|–|to|and|or)\s*{_NUM})?"
_PERSON = (r"(?:children|child|kids|kid|teens|teenagers|teenager|youngsters|young\s+people|young\s+travell?ers|"
           r"travell?ers|guests|participants|students|adults|families|family\s+members|anyone|those|people|"
           r"little\s+ones|minors)")
_PERSON_ONE = r"(?:child|kid|boy|girl|teen|teenager|traveller|traveler|guest|participant)"

# Age claims. Each pattern needs a person noun or the word age / aged / year-olds next to the number.
_AGE_PATTERNS = [
    re.compile(rf"\b{_PERSON}\s+(?:(?:who\s+are\s+)?aged|of\s+ages?|ages?|from\s+(?:the\s+)?ages?\s+of)\s+"
               rf"(?:between\s+)?{_RANGE}(?:\s*\+|\s+and\s+(?:up|over|above|older))?", re.I),
    re.compile(rf"\b(?:ages?|aged)\s+(?:between\s+)?{_RANGE}(?:\s*\+|\s+and\s+(?:up|over|above|older))?"
               rf"(?=[^.!?]{{0,40}}\b{_PERSON}\b|\s*(?:\+|and\s+(?:up|over|above|older))|[,.;:)]|\s+(?:only|welcome))",
               re.I),
    re.compile(rf"\b{_RANGE}[- ]year[- ]olds\b", re.I),
    re.compile(rf"\b{_RANGE}[- ]year[- ]old\s+{_PERSON_ONE}\b", re.I),
    re.compile(rf"\b{_PERSON}\s+(?:under|over|above|below)\s+{_NUM}\b(?!\s*(?:%|percent|days?|nights?|hours?|km|"
               rf"kilomet|miles?|m\b|metres?|meters?|feet|ft|kg))(?:\s+years?(?:\s+old)?)?", re.I),
    re.compile(rf"\b(?:under|over)[- ]{_NUM}s\b", re.I),
    re.compile(rf"\bminimum\s+age(?:\s+(?:of|is))?\s+{_NUM}\b", re.I),
    re.compile(rf"\b{_NUM}\s+years?\s+(?:old\s+)?and\s+(?:up|over|above|older)\b", re.I),
]

_GROUP_NOUN = r"(?:guests|travell?ers|people|participants|pax|walkers|trekkers|riders|cyclists|explorers|adventurers)"
_LIMIT = r"(?:up\s+to|no\s+more\s+than|at\s+most|max(?:imum)?(?:\s+of)?|never\s+more\s+than|capped\s+at|limited\s+to)"
_GROUP_PATTERNS = [
    re.compile(rf"\bsmall[- ]groups?\s+of\s+(?:{_LIMIT}\s+)?{_NUM}\b", re.I),
    re.compile(rf"\bgroups?\s+(?:of\s+)?{_LIMIT}\s+{_NUM}\b", re.I),
    re.compile(rf"\b{_LIMIT}\s+{_NUM}\s+{_GROUP_NOUN}\b", re.I),
    re.compile(rf"\bgroup\s+size(?:\s+(?:of|is))?\s+(?:up\s+to\s+|max(?:imum)?\s+)?{_NUM}\b", re.I),
]

# Brand-ethos, policy and certification terms. Each entry is a regex over the folded text (lower case, every
# hyphen / dash read as a space). Voice words every brand uses ("responsible", "sustainable", "authentic") are left
# out on purpose: they are tone, not a checkable promise.
ETHOS_TERMS: dict[str, str] = {
    "consent-based": r"consent (?:based|led|first)",
    "carbon-neutral": r"carbon (?:neutral|negative|positive|free)",
    "carbon offset": r"carbon offset(?:s|ting|ted)?|offsets? (?:the |its |all )?(?:carbon|emissions)",
    "net zero": r"net zero",
    "climate-positive": r"climate (?:positive|neutral)",
    "plastic-free": r"plastic free|single use plastic free|no single use plastic",
    "zero-waste": r"zero waste",
    "certified": r"certified|accredited|certification",
    "award-winning": r"award winning",
    "B Corp": r"\bb corp",
    "fair trade": r"fair trade|fairly traded",
    "ethically sourced": r"ethically sourced",
    "women-led": r"women (?:led|run|owned)|woman (?:led|run|owned)|female (?:led|run|owned)",
    "locally owned": r"locally (?:owned|run)",
    "family-run": r"family (?:run|owned)",
    "community-owned": r"community (?:owned|run|led|managed)",
    "Indigenous-led": r"indigenous (?:led|owned|run)",
    "profit share": (r"(?:\d+ ?%|percent|portion|share) of (?:the |our |every |each |all )?(?:profits?|proceeds|"
                     r"revenue)|profits? (?:go|goes|support|fund)|proceeds (?:go|support|fund)|donates?|donated"),
    "animal welfare": (r"cruelty free|no (?:elephant|animal) riding|elephant friendly|ethical (?:elephant|animal|"
                       r"wildlife)|rescued (?:elephants?|animals?)|sanctuary approved"),
    "child-safe": r"child safe|child protection|childsafe",
    "wheelchair-accessible": r"wheelchair (?:accessible|friendly)|step free",
    "eco-friendly": r"eco friendly|eco certified|environmentally friendly",
    "guaranteed": r"guaranteed (?:departures?|to depart)|price (?:match|guarantee)|best price|money back",
}
_ETHOS_COMPILED = {name: re.compile(rf"(?<![a-z0-9])(?:{rx})(?![a-z0-9])") for name, rx in ETHOS_TERMS.items()}

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Source side only: A1 text often states ages and group sizes as labels ("Minimum age: 7", "Maximum group size: 12",
# "Children 8+") that the claim patterns do not read. A number in a source clause that names an age or a group counts
# as stated, so a claim the source backs in another wording is not reported (report-only: missing a claim is
# preferred to a false flag).
_CLAUSE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
_AGE_CONTEXT = re.compile(r"\b(?:ages?|aged|years?[- ]olds?|child|children|kids?|infants?|teens?|teenagers?|minors?|"
                          r"under[- ]\d{1,2}s)\b", re.I)
_GROUP_CONTEXT = re.compile(r"\b(?:groups?|pax|guests|travell?ers|participants|people|persons)\b", re.I)


def _context_numbers(source: str, context: re.Pattern) -> frozenset[int]:
    out: set[int] = set()
    for clause in _CLAUSE_SPLIT.split(source or ""):
        if context.search(clause):
            out |= _numbers(clause)
    return frozenset(out)


def enabled() -> bool:
    """Kill switch (worker task def env, read per call): T2_BRAND_CLAIM_CHECK=off|0|false skips the check."""
    return (os.environ.get("T2_BRAND_CLAIM_CHECK") or "on").strip().lower() not in ("off", "0", "false", "no")


def fold(text: Optional[str]) -> str:
    """Lower case, every hyphen / dash / slash read as a space, whitespace collapsed (the ethos match key)."""
    t = (text or "").lower().replace(" ", " ")
    t = re.sub(r"[-‐‑‒–—/]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _to_int(token: str) -> Optional[int]:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUM_WORDS.get(token) or _NUM_WORDS.get(token.replace("-", " "))


def _numbers(phrase: str) -> frozenset[int]:
    out = set()
    for m in re.finditer(_NUM, phrase, re.I):
        n = _to_int(m.group(0))
        if n is not None and 0 < n < 100:
            out.add(n)
    return frozenset(out)


def _hits(patterns: Iterable[re.Pattern], text: str) -> list[tuple[str, frozenset[int]]]:
    out: list[tuple[str, frozenset[int]]] = []
    for rx in patterns:
        for m in rx.finditer(text or ""):
            nums = _numbers(m.group(0))
            if nums:
                out.append((m.group(0).strip(), nums))
    return out


def _claims(patterns: Iterable[re.Pattern], text: str) -> list[tuple[str, frozenset[int]]]:
    """(claim, its numbers) per match, text order; a span already matched by an earlier pattern (or overlapping
    one) is the same claim and is skipped."""
    found: list[tuple[int, int, str, frozenset[int]]] = []
    for rx in patterns:
        for m in rx.finditer(text or ""):
            nums = _numbers(m.group(0))
            if nums and not any(m.start() < e and s < m.end() for s, e, _, _ in found):
                found.append((m.start(), m.end(), m.group(0).strip(), nums))
    return [(c, n) for _, _, c, n in sorted(found)]


def _texts(rewritten: dict) -> list[tuple[str, str]]:
    """(field label, text) for every reader-facing string of a version (a highlight is `highlights[i]`, a day object
    of a list-shaped itinerary contributes its string values)."""
    out: list[tuple[str, str]] = []

    def walk(label: str, value: Any) -> None:
        if isinstance(value, str):
            if value.strip():
                out.append((label, value))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(f"{label}[{i}]", v)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(f"{label}.{k}", v)

    for field in _FIELDS:
        walk(field, (rewritten or {}).get(field))
    return out


def _sentence(text: str, phrase: str) -> str:
    for s in _SENT_SPLIT.split(text):
        if phrase.lower() in s.lower():
            return s.strip()[:300]
    return phrase[:300]


def source_text(tour: Optional[dict]) -> str:
    """The A1 text the T2 writer read (t2_rewrite_job.source_tour_dict values), as one string."""
    return "\n".join(str(v) for v in (tour or {}).values() if v)


def brand_text(brand_rules: Optional[dict]) -> str:
    """The tenant's brand pack as one string (only to say where an unsupported claim probably came from)."""
    keys = ("brand_name", "core_idea", "customer_segment", "customer_mindset", "style_guide", "system_prompt",
            "good_examples", "voice_examples", "signature_words")
    parts: list[str] = []
    for k in keys:
        v = (brand_rules or {}).get(k)
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v if x)
        elif v:
            parts.append(str(v))
    return "\n".join(parts)


def check(rewritten: dict, source: str, brand: str = "") -> dict:
    """The claims of `rewritten` (a T2 version's content) that `source` (the A1 text) does not support.

    Returns {"version", "checked": claims found, "supported": how many the source backs, "unsupported": [{"kind",
    "claim", "field", "sentence", "in_brand_pack"}]}. One entry per (kind, claim, field); text order."""
    src_fold, brand_fold = fold(source), fold(brand)
    src_age = [n for _, n in _hits(_AGE_PATTERNS, source)]
    src_group = [n for _, n in _hits(_GROUP_PATTERNS, source)]
    src_age_nums = frozenset().union(*src_age) if src_age else frozenset()
    src_group_nums = frozenset().union(*src_group) if src_group else frozenset()
    src_age_nums |= _context_numbers(source, _AGE_CONTEXT)
    src_group_nums |= _context_numbers(source, _GROUP_CONTEXT)

    checked = supported = 0
    unsupported: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def record(kind: str, claim: str, field: str, sentence: str, ok: bool, in_brand: bool) -> None:
        nonlocal checked, supported
        key = (kind, claim.lower(), field)
        if key in seen:
            return
        seen.add(key)
        checked += 1
        if ok:
            supported += 1
            return
        unsupported.append({"kind": kind, "claim": claim[:120], "field": field, "sentence": sentence[:300],
                            "in_brand_pack": in_brand})

    for field, text in _texts(rewritten):
        for kind, patterns, src_nums in (("age", _AGE_PATTERNS, src_age_nums),
                                         ("group_size", _GROUP_PATTERNS, src_group_nums)):
            for claim, nums in _claims(patterns, text):
                record(kind, claim, field, _sentence(text, claim), nums <= src_nums, False)
        text_fold = fold(text)
        for name, rx in _ETHOS_COMPILED.items():
            m = rx.search(text_fold)
            if not m:
                continue
            ok = bool(rx.search(src_fold))
            record("ethos", name, field, _orig_sentence(text, m.group(0)) or text, ok,
                   bool(brand_fold and rx.search(brand_fold)))
    return {"version": VERSION, "checked": checked, "supported": supported, "unsupported": unsupported}


def _orig_sentence(text: str, folded_match: str) -> Optional[str]:
    """The original sentence holding a match found in the folded text."""
    for s in _SENT_SPLIT.split(text):
        if folded_match in fold(s):
            return s.strip()
    return None
