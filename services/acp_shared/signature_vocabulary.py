"""AA-800 — the tenant signature vocabulary: three layers of words, one per reason to refuse a word.

`content_generation.forbidden_words.VALIDATE_FORBIDDEN` (plus "luxury" in the writer's prompt rule 1)
used to apply one list to every tenant. It mixes three kinds of words:

1. **Platform truth** (`PLATFORM_TRUTH`): price and booking promises the platform cannot back. Blocked
   for every tenant, in A1, T2 and T9.
2. **AA style**: Adventure Asia's own taste.
   - `STOCK_SUPERLATIVES`: A1 blocks them (unchanged). For a tenant (T2 / T9) one per piece is a
     warning; a second one makes them blocking (`SUPERLATIVE_CAP`).
   - `POSITIONING`: A1 blocks them (unchanged). A tenant may use the ones it declares as its own
     signature vocabulary (`signature_words`): a luxury brand writes "luxury".
3. **The tenant's own forbidden list**: always blocked, and it always wins over a declaration.

Evidence rule: a positioning word that states a service level ("a luxury lodge", "a tailored
itinerary") must be supported by the source (`service_level_claims`), for every tenant, declared or
not. It is a fidelity finding (T3 grounding for T2, F17 for T9), not a word-list hit.

The A1 paths keep `VALIDATE_FORBIDDEN` byte for byte; only tenant paths read this module.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Iterable, Optional

PLATFORM_TRUTH: tuple[str, ...] = ("cheap", "deal", "book now", "instant booking", "discount")
STOCK_SUPERLATIVES: tuple[str, ...] = ("pristine", "stunning", "breathtaking", "magical", "paradise")
POSITIONING: tuple[str, ...] = ("curated", "refined", "tailored", "bespoke", "luxury")
# In A1's prompt rule 1 but never in the validate list: a tenant prompt still asks the writer not to use
# it (unless declared), but no validate code fires on the word itself; an unsupported luxury claim is the
# evidence rule's job.
PROMPT_ONLY: frozenset[str] = frozenset({"luxury"})
SUPERLATIVE_CAP = 1

# Forms of a positioning word that declare it in the tenant's own brand text.
_DECLARE_FORMS: dict[str, tuple[str, ...]] = {
    "curated": ("curated", "curate", "curates", "curation"),
    "refined": ("refined", "refinement"),
    "tailored": ("tailored", "tailor-made", "tailor made", "tailor", "tailors"),
    "bespoke": ("bespoke",),
    "luxury": ("luxury", "luxurious", "luxe"),
}
# A cue anywhere before the word in its clause means the brand text rejects the word, not uses it.
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|nor|without|avoid\w*|don'?t|doesn'?t|do\s+not|does\s+not|ban\w*|forbid\w*|"
    r"clich\w*|anti)\b", re.IGNORECASE)
# A cue right before the word (at most one word between): "depth over luxury", "beyond bespoke".
_PREFER_RE = re.compile(r"\b(?:over|beyond|instead\s+of|rather\s+than|not\s+just)\s+(?:\w+\s+)?$", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r"[.;!?\n]+")

# Evidence rule. A service-level claim = a positioning word, then at most 2 words, then a service noun.
_SERVICE_NOUNS = (
    "lodge", "lodges", "hotel", "hotels", "stay", "stays", "accommodation", "accommodations", "room", "rooms",
    "suite", "suites", "villa", "villas", "camp", "camps", "tent", "tents", "resort", "resorts", "cabin",
    "cabins", "guesthouse", "guesthouses", "homestay", "ryokan", "train", "cruise", "cruises", "boat", "yacht",
    "vehicle", "vehicles", "car", "cars", "transfer", "transfers", "coach", "jeep", "service", "services",
    "dining", "dinner", "dinners", "meal", "meals", "menu", "spa", "itinerary", "itineraries", "programme",
    "program", "schedule",
)
_CLAIM_WORDS = {
    "luxury": ("luxury", "luxurious", "luxe"),
    "tailored": ("tailored", "tailor-made", "tailor made"),
    "bespoke": ("bespoke",),
    "curated": ("curated",),
    "refined": ("refined",),
}
_SUPPORT_ROOTS = {
    "luxury": ("luxur", "luxe", "deluxe", "five-star", "five star", "5-star", "5 star", "premium"),
    "tailored": ("tailor", "bespoke", "customi", "custom-made", "personali"),
    "bespoke": ("bespoke", "tailor", "customi", "custom-made", "personali"),
    "curated": ("curat",),
    "refined": ("refine",),
}
# The source wording the T2 writer is told to look for before it uses a signature word (rule 1). Each cue
# holds a root of _SUPPORT_ROOTS, so "the source says it" in the prompt is the same test the evidence rule
# (`service_level_claims`) applies afterwards (tests/unit/test_aa800_signature_nudge.py pins this).
_SUPPORT_CUES = {
    "luxury": ("luxury", "luxurious", "deluxe", "five-star", "premium"),
    "tailored": ("tailored", "tailor-made", "bespoke", "customised", "personalised"),
    "bespoke": ("bespoke", "tailor-made", "customised", "personalised"),
    "curated": ("curated",),
    "refined": ("refined",),
}


# ---------------------------------------------------------------- matching (same rule as validate_node)

@lru_cache(maxsize=256)
def _word_re(word: str) -> re.Pattern:
    """Whole word or phrase plus a plural "s", case-insensitive — forbidden_words._word_re's rule."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"s?(?![a-z0-9])")


def has_word(text: str, word: str) -> bool:
    return bool(word) and _word_re(word).search((text or "").lower()) is not None


def _dedupe(words: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(w for w in words if w))


def _lower_list(words) -> list[str]:
    return _dedupe((str(w).lower().strip() for w in (words or []) if w and str(w).strip()))


# ---------------------------------------------------------------- declaration

def explicit_signature_words(custom_validators) -> list[str]:
    """The `signature_words` list a tenant row carries in `tenant_brand_rules.custom_validators` (jsonb):
    `{"signature_words": [...]}`, or a list holding such an object. asyncpg hands jsonb back as text."""
    value = custom_validators
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else None
        except json.JSONDecodeError:
            return []
    entries = value if isinstance(value, list) else [value]
    words: list[str] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("signature_words"), list):
            words += [str(w) for w in entry["signature_words"]]
    return _lower_list(words)


def _declares(text: str, form: str) -> bool:
    """True when `form` appears in `text` in a clause that does not reject it."""
    pattern = _word_re(form)
    for clause in _CLAUSE_SPLIT_RE.split(text or ""):
        low = clause.lower()
        for m in pattern.finditer(low):
            before = low[:m.start()]
            if _NEGATION_RE.search(before) or _PREFER_RE.search(before):
                continue
            return True
    return False


def _brand_texts(pack: dict) -> list[str]:
    voice = pack.get("voice")
    if voice is None:
        voice = pack.get("voice_examples")
    return [t for t in (pack.get("brand_type"), pack.get("core_idea"),
                        ", ".join(str(v) for v in (voice or []) if v),
                        pack.get("style_guide"), pack.get("good_examples")) if isinstance(t, str) and t.strip()]


def signature_words(pack: Optional[dict]) -> list[str]:
    """The positioning words this tenant declared as its own (in POSITIONING order): named in its own T0
    brand text (brand type, core idea, voice, style guide, on-brand example) outside a negation, or listed
    in `pack["signature_words"]` (custom_validators). A word on the tenant's forbidden list never counts."""
    pack = pack or {}
    forbidden = set(_lower_list(pack.get("forbidden_words")))
    explicit = set(_lower_list(pack.get("signature_words")))
    texts = _brand_texts(pack)
    out = []
    for word in POSITIONING:
        if word in forbidden:
            continue
        if word in explicit or any(_declares(t, f) for t in texts for f in _DECLARE_FORMS[word]):
            out.append(word)
    return out


# ---------------------------------------------------------------- the layered list

def superlative_hits(text: str) -> list[str]:
    """Every stock superlative occurrence in `text`, in order of the list (one entry per occurrence)."""
    low = (text or "").lower()
    return [w for w in STOCK_SUPERLATIVES for _ in _word_re(w).finditer(low)]


def over_superlative_cap(text: str) -> bool:
    return len(superlative_hits(text)) > SUPERLATIVE_CAP


def undeclared_positioning(pack: Optional[dict], *, include_prompt_only: bool = False) -> list[str]:
    sig = set(signature_words(pack))
    return [w for w in POSITIONING if w not in sig and (include_prompt_only or w not in PROMPT_ONLY)]


def tenant_blocked_words(pack: Optional[dict], content_text: Optional[str] = None) -> list[str]:
    """The words a tenant piece (T2) is refused on — what validate_node fires FORBIDDEN_WORD on for a
    tenant rewrite: platform truth + the positioning words the tenant did not declare (validate words only;
    not `luxury`) + the stock superlatives present in `content_text` when it holds more than the cap + the
    tenant's own list. Lower case, deduplicated, order kept."""
    pack = pack or {}
    words = list(PLATFORM_TRUTH) + undeclared_positioning(pack)
    if content_text is not None and over_superlative_cap(content_text):
        words += [w for w in STOCK_SUPERLATIVES if has_word(content_text, w)]
    return _dedupe(words + _lower_list(pack.get("forbidden_words")))


# ---------------------------------------------------------------- evidence rule

@lru_cache(maxsize=1)
def _claim_re() -> re.Pattern:
    forms = sorted({f for fs in _CLAIM_WORDS.values() for f in fs}, key=len, reverse=True)
    nouns = "|".join(sorted(_SERVICE_NOUNS, key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9-])(" + "|".join(re.escape(f) for f in forms) + r")"
                      r"((?:[\s-]+[a-z'’&]+){0,2}?)[\s-]+(" + nouns + r")(?![a-z0-9])", re.IGNORECASE)


def _claim_word(form: str) -> str:
    form = form.lower()
    return next(w for w, fs in _CLAIM_WORDS.items() if form in fs)


def _supported(word: str, sources: list[str]) -> bool:
    low = " ".join(s.lower() for s in sources if s)
    return any(root in low for root in _SUPPORT_ROOTS[word])


def service_level_claims(text: str, *sources: Optional[str]) -> list[dict]:
    """The service-level positioning claims of `text` that no source supports: [{word, phrase}] in text
    order ('luxury lodge', 'tailored day-by-day itinerary'). Supported = a source has a root of the word
    ('luxury' ← 'luxurious', 'deluxe', 'five-star', 'premium'; 'tailored' ← 'tailor-made', 'customised',
    'personalised'). A brand-level use ('a luxury journey', 'refined taste') is not a service claim."""
    srcs = [s for s in sources if s]
    out = []
    for m in _claim_re().finditer(text or ""):
        word = _claim_word(m.group(1))
        if not _supported(word, srcs):
            out.append({"word": word, "phrase": " ".join(m.group(0).split())})
    return out


# ---------------------------------------------------------------- writer guidance

def _join(words: list[str]) -> str:
    return ", ".join(words)


def _or_join(words) -> str:
    words = list(words)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def t2_rule_1(pack: Optional[dict]) -> str:
    """Rule 1 of the T2 writer prompt, built from the layers for this tenant (A1 keeps
    prompts._RULE_1_CORE_FORBIDDEN byte for byte).

    S245 follow-up (owner decision 20/10/2026): with signature words, rule 1 nudges the writer to use one
    where the source states that level (the source wording to look for is named per word, the same roots
    the evidence rule accepts), and to leave it out everywhere else: source fidelity comes first. A tenant
    with no signature words gets the same rule as before (T2_SYSTEM_PROMPT is unchanged)."""
    sig = signature_words(pack)
    never = undeclared_positioning(pack, include_prompt_only=True) + list(PLATFORM_TRUTH)
    lines = [f"1. NEVER use these words: {_join(never)}",
             f"   Stock superlatives ({_join(list(STOCK_SUPERLATIVES))}): at most one in the whole tour; a"
             " concrete detail says more."]
    if sig:
        cues = "; ".join(f"{w}: the source says {_or_join(_SUPPORT_CUES[w])}" for w in sig)
        lines += [
            f"   This brand's own signature words: {_join(sig)}. Source fidelity (rule 6) comes first: use a"
            f" signature word only where the source itself states that level ({cues}).",
            "   When the source states it, use that signature word at least once, on the fact the source"
            " states, so the page reads as this brand. When the source does not state it, leave the word"
            " out: never add it as mood, and never as a service-level claim (a luxury lodge, a tailored"
            " itinerary) the source does not make.",
        ]
    return "\n".join(lines) + "\n"


def t9_words_line(pack: Optional[dict]) -> str:
    """The WORDS line of the T9 writer prompt, built from the same layers."""
    sig = signature_words(pack)
    parts = [f"WORDS: never write {_join(list(PLATFORM_TRUTH))}.",
             f"At most one stock superlative ({_join(list(STOCK_SUPERLATIVES))}) in the whole piece."]
    undeclared = undeclared_positioning(pack, include_prompt_only=True)
    if undeclared:
        parts.append(f"Avoid {_join(undeclared)}: they are not this brand's words.")
    if sig:
        parts.append(f"This brand's own signature words, yours to use where the content seed fits: {_join(sig)}.")
    parts.append("A service-level claim (a luxury lodge, a tailored itinerary) must be stated by the content seed.")
    return " ".join(parts)


__all__ = [
    "PLATFORM_TRUTH", "STOCK_SUPERLATIVES", "POSITIONING", "PROMPT_ONLY", "SUPERLATIVE_CAP",
    "explicit_signature_words", "signature_words", "superlative_hits", "over_superlative_cap",
    "undeclared_positioning", "tenant_blocked_words", "service_level_claims", "t2_rule_1", "t9_words_line",
    "has_word",
]
