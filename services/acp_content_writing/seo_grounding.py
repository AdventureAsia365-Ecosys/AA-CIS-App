"""AA-772 (S234 round 2) — an SEO keyword or meta may only name a product the seed supports.

S234 panel class 2: every Punakha Dzong LinkedIn post (6/6) got the keyword "Bhutan meditation
retreat", and the meta then sold "a Bhutan meditation retreat" — a product the trip does not have,
in the link preview. Root cause: `choose_keyword` matched the keyword on "bhutan", because the S234
"Location:" line ends with "(do not place it in any other town, region or country)" and the
country was read from after the LAST comma of the line — inside that parenthesis — so the country
was never removed from the place words. Second gap: nothing checked the keyword's non-place words
("meditation", "retreat") against the seed.

A product word is an activity, stay type or service a reader could book ('retreat', 'cruise',
'trek', 'safari', 'spa', 'meditation', 'cooking class', 'homestay' …). It is allowed in a keyword
or meta only when the seed (moment label, action, itinerary evidence, place-card facts) or the
piece's own body uses it (same stem: 'trek' ~ 'trekking', 'hike' ~ 'hiking'). Place words are not
checked here — scope (`hygiene.build_scope_lexicon`) owns them.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Stems of bookable products / services. A word matches a stem when it starts with it
# ('trekking' ~ 'trek'); stems of 3 letters ('spa', 'ski') match the whole word only.
PRODUCT_STEMS: tuple[str, ...] = (
    "retreat", "meditat", "yoga", "spa", "wellness", "massage", "healing",
    "cruise", "boat", "kayak", "canoe", "raft", "sail",
    "trek", "hike", "hiking", "rock climb", "mountaineer", "ski",
    "safari", "game drive", "wildlife", "birdwatch", "birding",
    "cooking", "culinary", "food tour", "tasting",
    "homestay", "glamping", "camping",
    "diving", "snorkel", "surf", "swim",
    "cycling", "bike", "biking",
    "horse", "riding", "camel", "elephant",
    "balloon", "zipline", "fishing",
    "festival", "honeymoon", "luxury", "pilgrimage", "workshop", "volunteer",
)
# Stems that name the same product: a 'hiking day' on a 'short trek' promises nothing new.
_SAME_PRODUCT = {"hike": "trek", "hiking": "trek", "bike": "cycling", "biking": "cycling",
                 "birding": "birdwatch"}
# Other roots in a source that support a product word (review r2b: 'camping' on a 'ger camp' seed,
# 'horse riding' on a 'horseback excursion', 'tasting' on 'taste the …', 'swim' on 'bathe').
_SUPPORT_ROOTS = {"camping": ("camp",), "riding": ("ride", "rode", "horseback"),
                  "tasting": ("tast",), "swim": ("bathe", "bathing")}
_GUIDE_PROMISE_RE = re.compile(r"\b(guides you|guided tour|private guide)\b", re.I)


def _fold(text: Optional[str]) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def _words(text: Optional[str]) -> list[str]:
    return re.findall(r"[a-z]+", _fold(text))


def _stem_of(word: str) -> Optional[str]:
    for stem in PRODUCT_STEMS:
        if " " in stem:
            continue
        if word == stem or (len(stem) >= 4 and word.startswith(stem)):
            return _SAME_PRODUCT.get(stem, stem)
    return None


def product_terms(text: Optional[str]) -> list[str]:
    """The product stems `text` uses, in order of first use ('meditation retreat' →
    ['meditat', 'retreat']); two-word products ('game drive', 'food tour') by phrase."""
    folded = " ".join(_words(text))
    out: list[str] = []
    for stem in PRODUCT_STEMS:
        if " " in stem and re.search(rf"\b{stem}", folded) and stem not in out:
            out.append(stem)
    for w in _words(text):
        stem = _stem_of(w)
        if stem and stem not in out:
            out.append(stem)
    return out


def _supported(stem: str, sources: list[str]) -> bool:
    if " " in stem:
        return any(re.search(rf"\b{stem}", " ".join(_words(s))) for s in sources)
    roots = _SUPPORT_ROOTS.get(stem, ())
    return any(_stem_of(w) == stem or (roots and w.startswith(roots)) for s in sources for w in _words(s))


def unsupported_products(text: Optional[str], *sources: Optional[str]) -> list[str]:
    """Product stems in `text` that no source uses — e.g. ['meditat', 'retreat'] for
    'Bhutan meditation retreat' against a Punakha Dzong seed."""
    srcs = [s for s in sources if s]
    return [stem for stem in product_terms(text) if not _supported(stem, srcs)]


def meta_unsupported_products(meta: Optional[str], seed: Optional[str], body: Optional[str]) -> list[str]:
    """S234 panel fix 3 — the product words of a meta description ('retreat', 'guides you') that
    appear in neither the seed nor the body. Readable words, not stems, for the violation text."""
    if not meta:
        return []
    found = unsupported_products(meta, seed, body)
    words = []
    for stem in found:
        word = next((w for w in _words(meta) if _stem_of(w) == stem), stem)
        words.append(word)
    guide = _GUIDE_PROMISE_RE.search(meta or "")
    if guide and not any(w.startswith("guid") for s in (seed, body) for w in _words(s)):
        words.append(guide.group(1).lower())
    return words


__all__ = ["PRODUCT_STEMS", "product_terms", "unsupported_products", "meta_unsupported_products"]
