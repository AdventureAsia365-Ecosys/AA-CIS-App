"""AA-790 (slice B) — the deterministic tenant-voice rubric, an OFFLINE scorer.

The S234 panel found 6 of its 15 false passes were Trail Pulse posts that read calm and formal although the
brand's declared voice is Energetic / Fresh / Direct / Lightly playful (r4 #6, #11: "That's not filler. That's
structure.", "That kind of sequencing is deliberate."); WildKind posts led by architecture with no habitat; Terra
posts in a school register or without the child. F9_brand_style is an uncalibrated LLM signal that only warns.

Nghiệp decided in S246: a deterministic rubric is the gate, the LLM panel only measures and calibrates it. The
S250 go/no-go (root docs/audits/2026-10-24-S250-s251-go-no-go, item 6b) keeps it OUT of the pipeline for now: no
ledger entry, no registry gate, no admin flag during the 4-week gate freeze (until ~19/11), and a blocking gate
only once κ ≥ 0.6 on human labels. So nothing in the T9 path imports this module; ``scripts/aa790/
voice_rubric_replay.py`` scores stored pieces with it and reports against the labels.

Pure: no LLM, no DB. Features are read from sentence STRUCTURE (lesson AA-985: word lists drift), except the
topic checks of the family and wildlife profiles, which are about what a section is about.

    rubric = compile_profile(brand_pack, override=custom_validators.get("voice_rubric"))
    result = score(text, rubric, channel="linkedin")
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from services.acp_content_writing.hygiene import _HASHTAG_LINE_RE, cta_norm, sentence_spans

RUBRIC_VERSION = "voice_rubric.v1"
PROFILES = ("energetic", "calm", "family", "wildlife", "neutral")

# Profile targets. Every number here was set on the S234 label pack + AA-729 fixture replay (see the AA-790
# implementation notes); a tenant override (shared.tenant_brand_rules.custom_validators.voice_rubric) wins.
DEFAULT_TARGETS: dict[str, dict] = {
    "energetic": {"contraction_rate_min": 0.15, "mean_words_max": 17.0, "second_person_min": 0.25,
                  "invitation_min": 1, "staccato_max": 0, "maxim_max": 0},
    "calm": {"exclamations_max": 0, "contraction_rate_max": 0.5, "staccato_max": 1},
    "family": {"family_section_min": 0.5, "school_max": 0},
    "wildlife": {"nature_section_min": 0.5},
    "neutral": {},
}

# ---------------------------------------------------------------- profile

_ENERGETIC = re.compile(r"\b(?:energetic|fresh|playful|dynamic|lively|upbeat|bold|fun)\b", re.I)
_YOUNG = re.compile(r"\b(?:young|active)\b", re.I)
_CALM = re.compile(r"\b(?:elegant|discreet|calm|refined|understated|quiet|sophisticated)\b", re.I)
_LUXURY = re.compile(r"\bluxury\b", re.I)
_FAMILY = re.compile(r"\bfamil(?:y|ies)\b|\bfamily-aware\b", re.I)
_CHILDREN = re.compile(r"\b(?:child|children|kids?|teens?)\b", re.I)
_NATURE = re.compile(r"\b(?:nature|wildlife|conservation|eco|nature-led)\b", re.I)


@dataclass
class Rubric:
    profile: str
    targets: dict = field(default_factory=dict)
    source: str = "compiled"            # compiled | override

    def as_dict(self) -> dict:
        return {"profile": self.profile, "targets": dict(self.targets), "source": self.source}


def _profile_of(pack: dict) -> str:
    voice = " ".join(pack.get("voice") or [])
    kind = pack.get("brand_type") or ""
    segment = pack.get("customer_segment") or ""
    if _FAMILY.search(kind) or _CHILDREN.search(segment) or re.search(r"family-aware", voice, re.I):
        return "family"
    if _NATURE.search(kind) or re.search(r"nature-led", voice, re.I):
        return "wildlife"
    if _ENERGETIC.search(voice) or _YOUNG.search(kind):
        return "energetic"
    if _CALM.search(voice) or _LUXURY.search(kind):
        return "calm"
    return "neutral"


def compile_profile(pack: Optional[dict], override: Optional[dict] = None) -> Rubric:
    """The register profile of a T0 brand pack (voice adjectives, brand_type, customer_segment) with its
    feature targets. ``override`` (``custom_validators.voice_rubric``: ``{"profile": …, "targets": {…}}``) wins:
    its profile replaces the compiled one, its targets update the profile's defaults."""
    profile = _profile_of(pack or {})
    source = "compiled"
    if isinstance(override, dict) and override:
        if override.get("profile") in PROFILES:
            profile = override["profile"]
        source = "override"
    targets = dict(DEFAULT_TARGETS[profile])
    if isinstance(override, dict) and isinstance(override.get("targets"), dict):
        targets.update({k: v for k, v in override["targets"].items() if isinstance(v, (int, float))})
    return Rubric(profile=profile, targets=targets, source=source)


def override_from_validators(custom_validators) -> Optional[dict]:
    """``shared.tenant_brand_rules.custom_validators.voice_rubric`` (jsonb; a dict or None)."""
    if isinstance(custom_validators, dict) and isinstance(custom_validators.get("voice_rubric"), dict):
        return custom_validators["voice_rubric"]
    return None


# ---------------------------------------------------------------- features

_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][\w'’-]*")
# A pronoun / auxiliary contraction ("it's", "you'll", "don't"); a possessive ("Trail Pulse's") is not one.
_CONTRACTION_RE = re.compile(
    r"\b(?:(?:it|that|there|here|what|you|we|they|i|he|she|let|who|where)['’](?:s|re|ll|ve|d|m)|\w+n['’]t)\b", re.I)
_SECOND_PERSON_RE = re.compile(r"\b(?:you|your|yours|you['’](?:ll|re|ve|d))\b", re.I)
_COPULA_RE = re.compile(r"\b(?:is|are|was|were)\b|['’]s\b", re.I)
_NEGATED_COPULA_RE = re.compile(r"^(?:that|this|it|these|those)(?:\s+(?:is|are|was|were)\s+not|['’]s\s+not|\s+"
                                r"(?:isn['’]t|aren['’]t|wasn['’]t))\b", re.I)
_DEMONSTRATIVE_RE = re.compile(r"^(?:that|this|such|it)\b", re.I)
_PRONOUN_RE = re.compile(r"^(?:i|you|we|they|it|that|this|there|here|these|those)\b", re.I)
_SUBORDINATOR_RE = re.compile(r"^(?:if|when|while|after|before|once|as|because|since|with)\b", re.I)
_NAME_AFTER_FIRST_RE = re.compile(r"(?<!^)(?<![.!?]\s)\b[A-Z][a-zà-öø-ÿ]+")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
_FAMILY_WORDS_RE = re.compile(r"\b(?:famil(?:y|ies)|child(?:ren)?|kids?|teens?|teenagers?|parents?|sons?|"
                              r"daughters?|young travell?ers|ages? \d+)\b", re.I)
# school framing (Terra r2 #17, r4 #4 / #16): the trip told as a lesson. A topic check, not a style one.
_SCHOOL_RE = re.compile(r"\b(?:classroom|curriculum|homework|lesson plan|field trip|learning outcomes?|"
                        r"school (?:project|trip)|textbook|syllabus)\b", re.I)
_NATURE_WORDS_RE = re.compile(r"\b(?:habitat|wildlife|species|birds?|forests?|rivers?|wetlands?|ecosystems?|"
                              r"animals?|reindeer|bears?|leopards?|deer|fish|trees?|valleys?|lakes?|landscapes?|"
                              r"meadows?|taiga|steppe|glaciers?|nature|natural|waterfalls?|cliffs?|mountains?|"
                              r"pools?|limestone|grasslands?|marsh(?:es)?)\b", re.I)


def _body(text: str, cta: Optional[str]) -> tuple[list[dict], list[list[dict]]]:
    """(prose sentences, sections). Headings and hashtag lines out; the FAQ out; the configured CTA sentence
    out (it is the brand's own line). Sections: split at headings; with no heading (a social post), each
    paragraph is a section."""
    want = cta_norm(cta)
    spans = [s for s in sentence_spans(text or "")
             if not s["in_faq"] and s["kind"] in ("prose", "bullet", "heading")
             and not _HASHTAG_LINE_RE.fullmatch(s["clean"] or "-")]
    has_heading = any(s["kind"] == "heading" for s in spans)
    sections: list[list[dict]] = [[]]
    prev_end = None
    sentences = []
    for s in spans:
        if s["kind"] == "heading":
            sections.append([])
            prev_end = None
            continue
        if want and want in cta_norm(s["clean"]):
            continue
        if not has_heading and prev_end is not None and "\n\n" in (text or "")[prev_end:s["start"]]:
            sections.append([])
        prev_end = s["end"]
        sections[-1].append(s)
        sentences.append(s)
    return sentences, [sec for sec in sections if sec]


def _words(sentence: str) -> list[str]:
    return _WORD_RE.findall(sentence)


def staccato(sentence: str) -> bool:
    """A short (≤ 8 words) negation with a demonstrative subject: "That's not filler.", "That is not a
    backdrop.", "It isn't a detour." — the S233 staccato template."""
    return len(_words(sentence)) <= 8 and bool(_NEGATED_COPULA_RE.match(sentence.strip().lstrip("\"'“‘*")))


def maxim(sentence: str) -> bool:
    """A short (≤ 8 words) abstract verdict with a demonstrative subject and a copula, and nothing concrete — no
    number, no name, no reader: "That kind of sequencing is deliberate.", "This is how pacing works." (S234 r4
    #11 / r3 #5). "This is Punakha Dzong." (a name) and "It is 5 a.m. in Paro." (a number) are not."""
    s = sentence.strip().lstrip("\"'“‘*")
    words = _words(s)
    if not (3 <= len(words) <= 8) or not _DEMONSTRATIVE_RE.match(s) or not _COPULA_RE.search(s):
        return False
    if re.search(r"\d", s) or _SECOND_PERSON_RE.search(s) or _NAME_AFTER_FIRST_RE.search(s):
        return False
    return not staccato(s)


def invitation(sentence: str) -> bool:
    """The sentence speaks to the reader directly: an imperative opener (a verb first, no subject — "Start the
    morning on a forest trail", "Grab a towel") or a you-will / you-can / let's line. Read by structure: the
    first word is lower-cased-verb-shaped, i.e. not a pronoun, an article, a subordinator, a name or a number."""
    s = sentence.strip().lstrip("\"'“‘*-• ")
    if re.search(r"\b(?:you['’]ll|you can|you will|let['’]s)\b", s, re.I):
        return True
    words = _words(s)
    if len(words) < 3:
        return False
    first = words[0]
    if _PRONOUN_RE.match(first) or _SUBORDINATOR_RE.match(first) or first.lower() in (
            "the", "a", "an", "on", "in", "at", "from", "for", "by", "of", "to", "day", "days", "every", "each",
            "most", "some", "all", "no", "not", "and", "but", "or", "so", "then", "just", "only", "even"):
        return False
    second = words[1].lower()
    # "Start the morning" / "Grab a towel": the next word is an article, a determiner or a pronoun object
    return second in ("the", "a", "an", "your", "this", "that", "these", "those", "it", "them", "us", "some",
                      "every", "each", "two", "three", "time", "in", "out", "up", "into", "through", "down")


def measure(text: str, *, cta: Optional[str] = None) -> dict:
    """The feature values of one piece (prose body, CTA line and FAQ out)."""
    sentences, sections = _body(text, cta)
    n = len(sentences) or 1
    words = [len(_words(s["clean"])) for s in sentences]
    flat = [s["clean"] for s in sentences]
    return {
        "sentences": len(sentences),
        "mean_words": round(sum(words) / n, 2),
        "contraction_rate": round(sum(bool(_CONTRACTION_RE.search(s)) for s in flat) / n, 3),
        "second_person": round(sum(bool(_SECOND_PERSON_RE.search(s)) for s in flat) / n, 3),
        "invitations": sum(invitation(s) for s in flat),
        "staccato": [s for s in flat if staccato(s)],
        "maxims": [s for s in flat if maxim(s)],
        "exclamations": sum(s.count("!") for s in flat) + sum(len(_EMOJI_RE.findall(s)) for s in flat),
        "family_sections": round(sum(any(_FAMILY_WORDS_RE.search(s["clean"]) for s in sec) for sec in sections)
                                 / (len(sections) or 1), 3),
        "school": [s for s in flat if _SCHOOL_RE.search(s)],
        "nature_sections": round(sum(any(_NATURE_WORDS_RE.search(s["clean"]) for s in sec) for sec in sections)
                                 / (len(sections) or 1), 3),
    }


# ---------------------------------------------------------------- the score

# A piece "would block" at this weight: a miss weighs 1, a SEVERE miss 2. The staccato template is severe: it is
# the S233 tell the S234 panel names in the deciding defects of 5 of the 6 Trail Pulse pieces of the label pack
# (r4 #6 is approved and heavy with it as its only voice miss). One ordinary miss is a style note.
WOULD_BLOCK_WEIGHT = 2
SEVERE = "severe: "


def _weight(misses: list[str]) -> int:
    return sum(2 if m.startswith(SEVERE) else 1 for m in misses)


def _misses(rubric: Rubric, f: dict) -> list[str]:
    t, out = rubric.targets, []
    if "contraction_rate_min" in t and f["contraction_rate"] < t["contraction_rate_min"]:
        out.append(f"formal register: contractions in {f['contraction_rate']:.0%} of sentences "
                   f"(target ≥ {t['contraction_rate_min']:.0%})")
    if "contraction_rate_max" in t and f["contraction_rate"] > t["contraction_rate_max"]:
        out.append(f"casual register: contractions in {f['contraction_rate']:.0%} of sentences "
                   f"(target ≤ {t['contraction_rate_max']:.0%})")
    if "mean_words_max" in t and f["mean_words"] > t["mean_words_max"]:
        out.append(f"long, flat sentences: {f['mean_words']} words on average (target ≤ {t['mean_words_max']})")
    if "second_person_min" in t and f["second_person"] < t["second_person_min"]:
        out.append(f"the reader is rarely addressed: 'you' in {f['second_person']:.0%} of sentences "
                   f"(target ≥ {t['second_person_min']:.0%})")
    if "invitation_min" in t and f["invitations"] < t["invitation_min"]:
        out.append("no direct invitation to the reader (an imperative or a 'you'll' line)")
    if "staccato_max" in t and len(f["staccato"]) > t["staccato_max"]:
        out += [f"{SEVERE}staccato negation template: '{s[:120]}'" for s in f["staccato"]]
    if "maxim_max" in t and len(f["maxims"]) > t["maxim_max"]:
        out += [f"consultancy maxim: '{s[:120]}'" for s in f["maxims"]]
    if "exclamations_max" in t and f["exclamations"] > t["exclamations_max"]:
        out.append(f"hype register: {f['exclamations']} exclamation mark(s) or emoji")
    if "family_section_min" in t and f["family_sections"] < t["family_section_min"]:
        out.append(f"the family or the child is in {f['family_sections']:.0%} of the body sections "
                   f"(target ≥ {t['family_section_min']:.0%})")
    if "school_max" in t and len(f["school"]) > t["school_max"]:
        out += [f"school framing: '{s[:120]}'" for s in f["school"]]
    if "nature_section_min" in t and f["nature_sections"] < t["nature_section_min"]:
        out.append(f"habitat or wildlife in {f['nature_sections']:.0%} of the body sections "
                   f"(target ≥ {t['nature_section_min']:.0%}): led by something else")
    return out



def score(text: str, rubric: Rubric, *, channel: Optional[str] = None, cta: Optional[str] = None) -> dict:
    """The offline result for one piece: ``{"rubric", "profile", "channel", "passed", "blocking": False,
    "repairable": False, "would_block", "violations", "features"}``. ``blocking`` is always False: this is a
    measurement (go/no-go 6b), never a gate result."""
    f = measure(text, cta=cta)
    misses = _misses(rubric, f)
    return {"rubric": RUBRIC_VERSION, "profile": rubric.profile, "channel": channel, "passed": not misses,
            "blocking": False, "repairable": False, "would_block": _weight(misses) >= WOULD_BLOCK_WEIGHT,
            "violations": misses, "features": f}


__all__ = ["DEFAULT_TARGETS", "PROFILES", "RUBRIC_VERSION", "Rubric", "SEVERE", "WOULD_BLOCK_WEIGHT",
           "compile_profile", "invitation", "maxim", "measure", "override_from_validators", "score", "staccato"]
