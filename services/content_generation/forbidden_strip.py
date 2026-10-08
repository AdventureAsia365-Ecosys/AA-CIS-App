"""AA-738: deterministic forbidden-word strip on the writer output (no LLM).

S217 Nepal wave: 9/86 tours went to review with "retried with sonnet, still FORBIDDEN_WORD". None of
the hits were in the AA core list — all were the AA brand's own forbidden words (explore ×10,
package ×2, nestled ×2, vibrant ×1): ordinary words both Haiku and Sonnet write naturally, so a
model retry never cleared them. A fixed substitution clears them for free.

Runs on the writer output before validate_node scores it (graph._apply_forbidden_strip), so
FORBIDDEN_WORD only fires for what this module could not clean. Order per hit:
  1. substitute a neutral word (keeps capitalisation, plural "s", fixes a/an);
  2. prose fields only — drop the sentence, if its paragraph keeps another sentence and it is not
     a "Day N" line; highlights — drop the item if >= 3 remain;
  3. otherwise leave it: FORBIDDEN_WORD fires and the AA-736 gate sends the tour to review.
"""
import re

from .forbidden_words import all_forbidden, has_word
from .seo_meta_utils import meta_in_band

# Neutral substitutes. None of them may be on the list being stripped (checked at runtime too).
# "explore" is handled by _explore_substitute (transitive "visit" / intransitive "wander").
_SUBSTITUTES = {
    # AA core list (forbidden_words.VALIDATE_FORBIDDEN)
    "curated": "selected",
    "pristine": "unspoiled",
    "refined": "elegant",
    "tailored": "customized",
    "bespoke": "custom",
    "stunning": "striking",
    "breathtaking": "dramatic",
    "magical": "memorable",
    "paradise": "haven",
    # AA brand list (shared.tenant_brand_rules 'default' v2)
    "nestled": "set",
    "vibrant": "lively",
    "package": "trip",
    "discover": "see",
    "unforgettable": "memorable",
    "iconic": "well-known",
    "world-class": "high-quality",
    "amazing": "remarkable",
    "exciting": "lively",
    "epic": "major",
    "glittering": "gleaming",
    "tapestry": "mix",
    "treasure-trove": "wealth",
    "must-visit": "notable",
    "must-see": "notable",
    "delve": "look",
    "embark on": "begin",
    "hidden gem": "lesser-known spot",
    "immersive experience": "hands-on experience",
    "seamless journey": "smooth journey",
}

# Words after "explore" that mean it is used without an object ("rest, explore, or shop").
_INTRANSITIVE_NEXT = frozenset({
    "at", "on", "in", "and", "or", "around", "further", "freely", "independently", "with", "by",
    "before", "after", "until", "for", "as", "nearby", "more", "alone", "later",
})

# Fields written as prose, where dropping a whole sentence is acceptable.
_PROSE_FIELDS = frozenset({"summary", "description", "itineraries"})
# Metadata the writer does not author (DFS keywords) — never rewritten here, and never scanned for
# FORBIDDEN_WORD by validate_node either (S218: "cheap summer getaways" / a source title as keyword).
SKIP_FIELDS = frozenset({"seo_keywords_used"})
_MIN_HIGHLIGHTS = 3  # validate_node fires HIGHLIGHTS_TOO_FEW below this

_DAY_MARKER = re.compile(r"\bday\s*\d+", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_ARTICLE_BEFORE = re.compile(r"\b([Aa]n?)(\s+)$")


def _word_pattern(word: str) -> re.Pattern:
    # Same whole-word rule as forbidden_words._word_re (plus plural "s"), on the original-case text.
    return re.compile(r"(?<![A-Za-z0-9])(" + re.escape(word) + r")(s?)(?![A-Za-z0-9])", re.IGNORECASE)


def _match_case(src: str, repl: str) -> str:
    if len(src) > 1 and src.isupper():
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def _explore_substitute(text: str, end: int) -> str:
    nxt = re.match(r"\s*([A-Za-z]+)?", text[end:])
    word = (nxt.group(1) or "").lower() if nxt else ""
    return "wander" if (not word or word in _INTRANSITIVE_NEXT) else "visit"


def _substitute(text: str, word: str, forbidden: list[str]) -> str:
    """Replace every whole-word hit of `word` in `text` with its neutral substitute.
    Returns `text` unchanged when there is no usable substitute."""
    if word != "explore" and word not in _SUBSTITUTES:
        return text
    pat = _word_pattern(word)
    out, last = [], 0
    for m in pat.finditer(text):
        base = _explore_substitute(text, m.end()) if word == "explore" else _SUBSTITUTES[word]
        if any(has_word(base, w) for w in forbidden):
            return text  # the substitute is itself forbidden for this brand
        repl = _match_case(m.group(1), base) + m.group(2)
        prefix = text[last:m.start()]
        art = _ARTICLE_BEFORE.search(prefix)
        if art:
            an = repl[:1].lower() in "aeiou"
            fixed = ("An" if an else "A") if art.group(1)[0].isupper() else ("an" if an else "a")
            prefix = prefix[:art.start(1)] + fixed + art.group(2)
        out.append(prefix + repl)
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _hits(text: str, forbidden: list[str]) -> list[str]:
    return [w for w in forbidden if has_word(text, w)]


def _drop_sentences(text: str, forbidden: list[str]):
    """Drop each sentence that still holds a forbidden word. Returns the new text, or None when a
    clean drop is impossible (a Day-marker sentence, or a paragraph that would be left empty)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not _hits(line, forbidden):
            continue
        sentences = _SENTENCE_SPLIT.split(line.strip())
        keep = [s for s in sentences if not _hits(s, forbidden)]
        dropped = [s for s in sentences if _hits(s, forbidden)]
        if not keep or any(_DAY_MARKER.search(s) for s in dropped):
            return None
        indent = line[:len(line) - len(line.lstrip())]
        lines[i] = indent + " ".join(keep)
    return "\n".join(lines)


def _substitute_all(field: str, text: str, forbidden: list[str], report: dict) -> str:
    for w in _hits(text, forbidden):
        new = _substitute(text, w, forbidden)
        if new != text:
            report["replaced"].append((field, w))
            text = new
    return text


def _strip_text(field: str, text: str, forbidden: list[str], report: dict) -> str:
    text = _substitute_all(field, text, forbidden, report)
    left = _hits(text, forbidden)
    if not left:
        return text
    if field in _PROSE_FIELDS:
        dropped = _drop_sentences(text, forbidden)
        if dropped is not None:
            report["dropped"].extend((field, w) for w in left)
            return dropped
    report["unresolved"].extend((field, w) for w in left)
    return text


def strip_forbidden(generated: dict, tenant_words=None) -> tuple[dict, dict]:
    """Return (cleaned copy of `generated`, report). The report lists (field, word) pairs under
    "replaced", "dropped" (sentence/item removed) and "unresolved" (left for the AA-736 gate)."""
    forbidden = all_forbidden(tenant_words)
    report = {"replaced": [], "dropped": [], "unresolved": []}
    out = dict(generated or {})
    for field, value in out.items():
        if field in SKIP_FIELDS:
            continue
        if isinstance(value, str) and _hits(value, forbidden):
            new = _strip_text(field, value, forbidden, report)
            if field == "seo_meta" and meta_in_band(value) and not meta_in_band(new):
                # A substitute that pushes an in-band meta out of band is worse than the hit.
                report["replaced"] = [r for r in report["replaced"] if r[0] != field]
                report["unresolved"].extend((field, w) for w in _hits(value, forbidden))
                continue
            out[field] = new
        elif isinstance(value, list) and any(isinstance(v, str) and _hits(v, forbidden) for v in value):
            items = [_substitute_all(field, v, forbidden, report) if isinstance(v, str) else v
                     for v in value]
            dirty = [v for v in items if isinstance(v, str) and _hits(v, forbidden)]
            if dirty and len(items) - len(dirty) >= _MIN_HIGHLIGHTS:
                report["dropped"].extend((field, w) for v in dirty for w in _hits(v, forbidden))
                items = [v for v in items if not (isinstance(v, str) and _hits(v, forbidden))]
            else:
                report["unresolved"].extend((field, w) for v in dirty for w in _hits(v, forbidden))
            out[field] = items
    return out, report
