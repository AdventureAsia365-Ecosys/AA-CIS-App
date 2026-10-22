"""AA-803 (S247), part B — clear an itinerary meal claim only on STRUCTURED source evidence.

``brand_audit_node.meal_time_claims_supported`` raises ITINERARY_MEAL_TIME_INVENTED on a meal-inclusion claim
the source does not back. Until now only an enforced Jev accept (``a1_claim_supported``) cleared it. Draft PR
#905 tried a free-text rule (negation tiers, day drift, scoped inclusions, ...) and three adversarial review
rounds kept finding about 20 new false clears each (65 in all). This module is the narrowed design: it reads
only two kinds of evidence and sends everything else to Jev, as before.

1. **A same-day meal list.** The matched source day carries a ``Meals: B, L, D`` / ``Meals included: Breakfast,
   Dinner`` list or a bracket of meal codes (``(B, L)``, ``(B/-/D)``), and the list is *pure*: only meal words,
   B/L/D codes, separators and placeholders. Anything else in the list (``(own expense)``, ``on request``, a
   count, a venue) makes the day unreadable. Narrative mentions ("lunch at a local restaurant") never count.
2. **An unscoped every-day statement in INCLUSIONS.** One INCLUSIONS item that is, as a whole, ``All meals`` /
   ``Full board`` / ``Half board`` (breakfast only) / ``Daily breakfast`` / ``Breakfast and dinner daily`` /
   ``Three meals a day``. Any other word in the item ("during the trek", "in Kathmandu", "except", a day
   number) leaves it unread. It never covers the first or the last output day (arrival and departure days
   rarely have every meal), and never a day whose source has its own meal list.

A claim is cleared only when every meal it names is covered and nothing in sight casts doubt on it:

- **Output sentence.** It names its meals in plain words or codes; a generic meal noun (meals, feast, picnic,
  barbecue, banquet, board, buffet) claims all three. It is not cleared when it holds a digit or a time word, a
  negation or an option ("not", "own", "optional", "or", "if", ...), or a reference to another or several days
  ("tomorrow", "every", "daily", "next", "day", ...).
- **Day matching.** The output days are exactly 1..N, the source has exactly one single-day "Day n" marker for
  each of 1..N in order, the tour's stated duration (when exact) is N, and the matched source day names no
  other day and no other night (an inline "Day 3", "the 2nd evening", "two nights", "next morning").
- **The day's free text is silent on the meal.** Outside its pure lists, code brackets and bare time markers
  ("After breakfast,"), the source day may not name a claimed meal or meals in general: when the text talks about
  the meal, the text (not the list) would have to be read, so the claim goes to Jev.
- **Doubt in the source.** A strong cue anywhere in the source day (``own expense``, ``not included``,
  ``optional``, ``on request``, ``payable``, ``upgrade``, ``supplement``, ...), or a weak or soft cue (``no``,
  ``not``, ``except``, ``if``, ``or``, ``at leisure``, ``free time``, ...) in a sentence of that day that names a
  meal. A sentence of ANY other source day (or after the last day) that names a claimed meal with a strong cue,
  or with a negation-type cue and a tour-wide or remark marker ("Note: lunches are at your own expense throughout
  the trip."). A day that names a variant (option, package, premium, ...), or a list with a non-section heading or
  an audience word (guests, cabin, group, ...) above it in its paragraph: the list may hold for one option only.
  A meal named in EXCLUSIONS (any generic meal word there blocks all three). For the INCLUSIONS path:
  another INCLUSIONS item that names the same meal, a source meal list on any day that lacks it, a layout that can
  hide a scope (plain multi-line text, an indented sub-list, a colon heading, a dash), an exception or scope word
  anywhere in INCLUSIONS, or any meal word in SUMMARY / DESCRIPTION / PRICE.

Every rule errs the same way: when in doubt, the sentence is not cleared and goes to Jev (or keeps the code once
the question is off). Pure functions, no I/O.
"""
from __future__ import annotations

import re
from typing import Optional

B, L, D = "B", "L", "D"
ALL = frozenset({B, L, D})

# ── output side ─────────────────────────────────────────────────────────────────────────────────────────────

# "picnic lunch", "buffet breakfast", "BBQ dinner", "lunch buffet": the meal word names the meal, the noun adds none.
_COMPOUND_BEFORE_RE = re.compile(
    r"\b(?:picnic|packed|buffet|bbq|barbecue(?:-style)?|set|box(?:ed)?)[\s-]+(breakfast|lunch|dinner|supper)\b",
    re.IGNORECASE)
_COMPOUND_AFTER_RE = re.compile(r"\b(breakfast|lunch|dinner|supper)[\s-]+(?:buffet|picnic|box)\b", re.IGNORECASE)
_WORD_MEALS = (
    (re.compile(r"\bbrunch(?:es)?\b", re.IGNORECASE), {B, L}),
    (re.compile(r"\bbreakfast", re.IGNORECASE), {B}),
    (re.compile(r"\blunch", re.IGNORECASE), {L}),
    (re.compile(r"\b(?:dinner|supper)", re.IGNORECASE), {D}),
)
_GENERIC_RE = re.compile(
    r"\b(?:meals?|feasts?|picnics?|banquets?|barbecues?|bbqs?|buffets?)\b|\b(?:full|half)[\s-]+board\b", re.IGNORECASE)
_DAY_TITLE_PREFIX_RE = re.compile(r"^\s*Day\s+\d+\s*[—–:-]?\s*", re.IGNORECASE)
# A sentence with any of these is not cleared (sent to Jev): a figure or time, a negation or option, another day.
_OUT_REFUSE_RE = re.compile(
    r"\d|\b(?:noon|midnight|o'clock|o’clock|half\s+past|quarter\s+(?:past|to)|one|two|three|four|five|six|seven"
    r"|eight|nine|ten|eleven|twelve)\b"
    r"|\b(?:not|no|never|none|nor|neither|without|except|excluding|excluded|exclude|unless|if|or|optional|option"
    r"|options|own|extra|supplement|upgrade|request|payable|pay|paid|purchase|cost|price|charge|may|might|could"
    r"|can|should|would|choose|choice|opt|recommend\w*|suggest\w*|available|leisure|free|instead|alternative\w*"
    r"|tomorrow|yesterday|tonight|next|following|previous|prior|later|earlier|every|each|daily|throughout"
    r"|remaining|both|whole|entire|days|nights|mornings|evenings|week|weeks)\b"
    r"|\brest\s+of\b|\b(?:day|night|morning|evening)\s+(?:after|before)\b"
    r"|\b(?:first|second|third|fourth|fifth|sixth|final|last)\s+(?:full\s+)?(?:day|night|morning|evening)\b"
    r"|\ball\s+(?:the\s+)?(?:days?|nights?|tours?"
    r"|trips?|treks?)\b|\w+n['’]t\b",
    re.IGNORECASE)


def claimed_meals(sentence: str) -> frozenset[str]:
    """The meals an output claim sentence names (B / L / D). A generic meal noun, or a ``Meals:`` list that is not
    a pure code list, claims all three; nothing recognisable also claims all three (over-claiming only means the
    sentence is not cleared)."""
    text = _DAY_TITLE_PREFIX_RE.sub("", sentence or "", count=1)
    got: set[str] = set()
    for m in _LABEL_RE.finditer(text):
        listed = _pure_list(_list_tail(text[m.end():]))
        if listed is None:
            return ALL
        got |= listed
    text_wo_labels = _LABEL_RE.sub(" ", text)
    for m in _BRACKET_RE.finditer(text_wo_labels):
        codes = _pure_codes(m.group(1))
        if codes:
            got |= codes
    plain = _BRACKET_RE.sub(" ", text_wo_labels)
    plain = _COMPOUND_AFTER_RE.sub(r"\1", _COMPOUND_BEFORE_RE.sub(r"\1", plain))
    if _GENERIC_RE.search(plain):
        return ALL
    for rx, meals in _WORD_MEALS:
        if rx.search(plain):
            got |= meals
    return frozenset(got) if got else ALL


def output_refused(sentence: str) -> bool:
    """True when the sentence carries something the rule never vouches for (see module docstring)."""
    text = _DAY_TITLE_PREFIX_RE.sub("", sentence or "", count=1)
    return bool(_OUT_REFUSE_RE.search(_LABEL_RE.sub(" ", text)))


# ── structured source lists ───────────────────────────────────────────────────────────────────────────────

_LABEL_RE = re.compile(r"\bmeals?(?:\s+(?:included|provided|incl\.?))?\s*[:=\-–—]\s*", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\(([^()]*)\)")
# Meal words in any case; the codes B / L / D only in capitals ("(l)" or "(d)" are not meal codes).
_TOKEN = r"(?:(?i:breakfast|lunch|dinner|supper|n/a|none|nil)|[BLDxX×\-–—])"
_SEP = r"(?:\s*(?:[,/&|·•+]|(?i:\band\b))\s*|\s+)"
_PURE_LIST_RE = re.compile(rf"^\s*{_TOKEN}(?:{_SEP}{_TOKEN})*\s*\.?\s*$")
_CODE_TOKEN = r"[BLDxX×\-–—]"
_PURE_CODES_RE = re.compile(rf"^\s*{_CODE_TOKEN}(?:\s*(?:[,/&|·•+]|\band\b)?\s*{_CODE_TOKEN})*\s*$")
_TOKEN_MEAL = {"breakfast": B, "b": B, "lunch": L, "l": L, "dinner": D, "supper": D, "d": D}
_LIST_END_RE = re.compile(r"[.;](?:\s|$)|\n|\)")
# A "Meals:" label counts only where a list can start: at the start of a line or after a sentence end, a bracket,
# a pipe, a dash or a bullet. "Group B meals: B, L", "Premium meals: ..." or "Extension meals: ..." qualify the
# list, so the day is unreadable.
_LABEL_START_OK = set(".!?;|(–—-•*\n")


def _list_tail(rest: str) -> str:
    """The list after a ``Meals:`` label: up to the end of the line or the first sentence end. A label alone on its
    line takes the next line, without its bullet ("Meals Included:\n• Breakfast, Lunch")."""
    if not rest.split("\n", 1)[0].strip() and "\n" in rest:
        rest = rest.split("\n", 1)[1].lstrip("\n")
    rest = re.sub(r"^\s*[•·▪●*]\s*", "", rest)
    m = _LIST_END_RE.search(rest)
    return rest[: m.start()] if m else rest


def _tokens(text: str) -> set[str]:
    out = set()
    for tok in re.findall(r"(?i:breakfast|lunch|dinner|supper)|\b[BLD]\b", text):
        out.add(_TOKEN_MEAL[tok.lower()])
    return out


def _pure_list(text: str) -> Optional[frozenset[str]]:
    """Meals of a pure list ("B, L, D", "Breakfast | Dinner", "B/-/D", "None"), else None."""
    if not _PURE_LIST_RE.match(text or ""):
        return None
    return frozenset(_tokens(text))


def _pure_codes(text: str) -> Optional[frozenset[str]]:
    """Meals of a bracket that holds only codes ("B, L", "B/-/D"); None for anything else or no meal code."""
    if not _PURE_CODES_RE.match(text or ""):
        return None
    meals = frozenset(_tokens(text))
    return meals or None


# A list may belong to one option, cabin, class or group only. The ``_LABEL_START_OK`` test sees only the character
# before "Meals:"; a qualifier on the line before or in the sentence before is in another sentence and raises no
# meal-sentence doubt. Three guards, each making the day's lists unreadable:
# - a variant word anywhere in the day ("Option 1 - bike tour. Meals: B, L", "Option A: kayak ...", "premium");
_VARIANT_RE = re.compile(
    r"\b(?:options?|optional\w*|packages?|premium|superior|deluxe|luxury|versions?|variants?|tiers?|categor\w+"
    r"|choices?|choose)\b",
    re.IGNORECASE)
# - an audience or class word in the list's paragraph before it, or on the non-empty line above it ("Guests on the
#   long trek.\nMeals: B, L"); in an earlier paragraph these words are narrative ("check into your cabin",
#   "charms every traveler");
_AUDIENCE_RE = re.compile(
    r"\b(?:cabins?|class(?:es)?|standard|guests?|groups?|travell?ers?|clients?|participants?|passengers?|members?"
    r"|children|kids|adults?|vegetarians?|trekkers?|hikers?|walkers?|riders?|cyclists?|bikers?|divers?|climbers?"
    r"|students?|seniors?|couples?|famil(?:y|ies)|singles?|solo|those|whoever|anyone)\b",
    re.IGNORECASE)
# - a heading line (ending with a colon) above the list that is not one of the plain section headings of the
#   structured layouts ("Accommodation:", "Included activities:", "Transportation:"): "Superior cabin guests:",
#   "Group A:", "Note:".
_SECTION_HEADING_RE = re.compile(
    r"^\W*(?:(?:hotel\s+)?accommodations?|included(?:\s+(?:transport(?:ation)?|activities|and|&|,|\s)+)?"
    r"|transport(?:ation)?|activities|overnight|lodging|highlights?)\s*:$",
    re.IGNORECASE)


def _list_scoped(block: str, pos: int) -> bool:
    """True when the text above the list at ``pos`` may scope it: a non-section heading line anywhere before it, or
    an audience / class word in the list's paragraph before it (text since the last blank line) or on the non-empty
    line above it."""
    head = block[:pos]
    lines = head.split("\n")
    if any(ln.strip().endswith(":") and not _SECTION_HEADING_RE.match(ln.strip()) for ln in lines[:-1]):
        return True
    paragraph = re.split(r"\n[ \t]*\n", head)[-1]
    above = [ln for ln in lines[:-1] if ln.strip()][-1:]
    return bool(_AUDIENCE_RE.search(paragraph) or any(_AUDIENCE_RE.search(ln) for ln in above))


def day_lists(block: str) -> tuple[list[frozenset[str]], bool]:
    """(the pure meal lists of a source day block, whether it holds a meal list that is NOT pure or that the block
    may scope: see ``_VARIANT_RE`` / ``_list_scoped``)."""
    lists: list[frozenset[str]] = []
    impure = False
    for m in _LABEL_RE.finditer(block):
        before = block[: m.start()].rstrip(" \t")
        listed = _pure_list(_list_tail(block[m.end():]))
        if listed is None or (before and before[-1] not in _LABEL_START_OK) or _list_scoped(block, m.start()):
            impure = True
        else:
            lists.append(listed)
    for m in _BRACKET_RE.finditer(block):
        inner = m.group(1)
        if _LABEL_RE.search(inner):
            continue                                     # "(Meals: B, D)": read by the label loop above
        codes = _pure_codes(inner)
        if codes:
            if _list_scoped(block, m.start()):
                impure = True
            else:
                lists.append(codes)
    if (lists or impure) and _VARIANT_RE.search(block):
        impure = True
    return lists, impure


# ── doubt in the source ─────────────────────────────────────────────────────────────────────────────────────

_STRONG_CUE_RE = re.compile(
    r"\bown\s+(?:expense|account|cost|arrangement)|\bat\s+(?:your|their|guest'?s?|passenger'?s?)?\s*own\b"
    r"|\bon\s+(?:your|their)\s+own\b|\bnot\s+(?:be\s+)?(?:included|provided|covered)\b|\bexclud\w*|\bexcl\b"
    r"|\bpayable\b|\bpay\s+(?:direct|locally|on)\b|\b(?:extra|additional)\s+(?:cost|charge)\b|\boptional\w*"
    r"|\bon\s+request\b|\bupon\s+request\b|\bsupplement\w*|\bupgrade\w*|\bself[\s-]?(?:pay|paid|arranged|catered)"
    r"|\bfor\s+(?:purchase|your\s+account)\b|\bexpense\b|\bnot\s+inclu|\bno\s+inclu|\bnon\s+inclu|t[ựu]\s*t[úu]c"
    r"|\blibre\b|\$|\b(?:usd|eur|thb|vnd|baht|inr)\b",
    re.IGNORECASE)
_WEAK_CUE_RE = re.compile(
    r"\b(?:no|not|never|none|nor|neither|without|except|excluding|unless|if|or|may|might|could|can|should"
    r"|would|perhaps|maybe|consider|choos\w*|choice|opt|recommend\w*|suggest\w*|bring|carry|pack|available"
    r"|instead|alternative\w*|option\w*|wish|want|like|leisure|free|own|extra|either|tips?|grab|find|places?"
    r"|spots?|self|yourself|yourselves|independent\w*|separate\w*|pay|paid|purchase|buy|cost|price|charge"
    r"|later|tomorrow|tonight|next|following|previous|yesterday|only|those|premium|deluxe|package|depending"
    r"|subject|guests?|travell?ers?'?s?|clients?|responsib\w*|account|discretion|tba|tbc|bill|settle\w*|budget"
    r"|themselves|cash|bought"
    r"|n/a)\b|\w+n['’]t\b|\bup\s+to\s+you\b|\b(?:by|on)\s+you\b|\bto\s+be\s+(?:confirmed|advised|decided|arranged)\b",
    re.IGNORECASE)
_MEAL_MENTION_RE = re.compile(
    r"\b(?:breakfasts?|brunch\w*|lunch\w*|dinners?|suppers?|meals?|feasts?|picnics?|banquets?|barbecues?|bbqs?"
    r"|buffets?|food|eat\w*|din(?:e|ing))\b|\b(?:full|half)[\s-]+board\b|\(\s*[bld]\b|\bmeals?\s*[:=]",
    re.IGNORECASE)
_TIME_MARKER_RE = re.compile(r"\b(?:after|before|following|post)\s+(?:breakfast|lunch|dinner)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n")
_OTHER_DAY_RE = re.compile(
    r"\bdays?\s*#?\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|last|next|before"
    r"|after|following|previous)\b"
    r"|\b(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|last|next|following|previous|prior)[\s-]+"
    r"(?:day|night|evening|morning|afternoon)s?\b"
    r"|\bnights?\s*#?\s*\d+|\b(?:\d+|two|three|four|five|six|seven)\s+nights\b"
    r"|\b(?:tomorrow|yesterday|alternative\w*|version|extension|plan\s+[a-z]|route\s+\d|post-tour|pre-tour"
    r"|add-on)\b",
    re.IGNORECASE)


def _narrative(block: str) -> str:
    """The day text without its ``Meals:`` lists, its pure code brackets and bare time markers ("After lunch,")."""
    text = block or ""
    for m in reversed(list(_LABEL_RE.finditer(text))):
        rest = text[m.end():]
        tail = _list_tail(rest)
        if _pure_list(tail) is not None:
            text = text[: m.start()] + " " + rest[rest.find(tail) + len(tail):]
    text = _BRACKET_RE.sub(lambda m: " " if _pure_codes(m.group(1)) else m.group(0), text)
    return _TIME_MARKER_RE.sub(" ", text)


def narrative_names(block: str, meals: frozenset[str]) -> bool:
    """True when the day's free text talks about one of ``meals`` (or about meals in general): then the text, not
    the list, would have to be read, and the claim is left to Jev."""
    text = _narrative(block)
    if re.search(r"\b(?:meals?|food|feasts?|picnics?|banquets?|barbecues?|bbqs?|buffets?|eat\w*|din(?:e|ing))\b"
                 r"|\b(?:full|half)[\s-]+board\b", text, re.IGNORECASE):
        return True
    return bool(_tokens(re.sub(r"\b[bld]\b", " ", text)) & meals)


def day_doubt(block: str) -> bool:
    """True when the source text casts doubt on any meal of it (see module docstring)."""
    if _STRONG_CUE_RE.search(block):
        return True
    for s in _SENTENCE_SPLIT_RE.split(block):
        # "After breakfast, you can ..." names the meal only as a time marker; the cue is about something else.
        if _MEAL_MENTION_RE.search(_TIME_MARKER_RE.sub(" ", s)) and _WEAK_CUE_RE.search(s):
            return True
    return False


# Another day's sentence reaches the claimed day when it is tour-wide (a tour-wide word, a reference to another day)
# or reads as a general remark (a note word, a plural meal noun: "Lunches can be bought at stalls."). A sentence
# scoped to its own day ("There are no meals included today") does not, unless it is also tour-wide.
_TOURWIDE_RE = re.compile(
    r"\b(?:throughout|every|each|daily|always|whole|entire|tour|trip|journey|programme|program|itinerary"
    r"|departures?|holiday|days|nights|remaining|general\w*|usually|unless|except\w*)\b|\brest\s+of\b"
    r"|\ball\s+(?:the\s+)?(?:meals?|breakfasts?|lunch\w*|dinners?|suppers?|days?|nights?|tours?|trips?|times?)\b",
    re.IGNORECASE)
_REMARK_RE = re.compile(
    r"\b(?:note|nb|n\.b|important|please|remarks?|attention|reminder)\b|\b(?:breakfasts|lunches|dinners|suppers)\b",
    re.IGNORECASE)
_SAME_DAY_RE = re.compile(r"\b(?:today|tonight|this\s+(?:day|morning|afternoon|evening|night))\b", re.IGNORECASE)
# Weak cues that turn a remark against a meal (negation, option, payment, self-arranging); the wider _WEAK_CUE_RE
# words ("or", "places", "find", "like", "guests") are narrative in other days' text.
_NEG_CUE_RE = re.compile(
    r"\b(?:no|not|never|none|nor|neither|without|except\w*|excluding|unless|if|may|might|could|can|cannot|should"
    r"|option\w*|choos\w*|choice|alternative\w*|depend\w*|subject|vary|varies|own|self|yourself|yourselves"
    r"|independent\w*|separate\w*|pay|paid|purchase|buy|bought|cost|price|charge|extra|cash|bring|carry|pack"
    r"|responsib\w*|account|budget|bill|settle\w*|tba|tbc)\b|\w+n['’]t\b|\bup\s+to\s+you\b|\b(?:by|on)\s+you\b"
    r"|\bto\s+be\s+(?:confirmed|advised|decided|arranged)\b",
    re.IGNORECASE)


def _sentence_meals(sentence: str) -> frozenset[str]:
    """The meals another day's sentence names, outside bare time markers ("After breakfast,"). ``meals`` / ``food`` /
    ``dining`` / ``full board`` name all three; a one-off meal word (picnic, feast, banquet, barbecue, buffet, eat,
    dine) names all three only in a tour-wide sentence or a remark (otherwise it is that day's own meal)."""
    text = _TIME_MARKER_RE.sub(" ", sentence or "")
    if not _MEAL_MENTION_RE.search(text):
        return frozenset()
    if re.search(r"\b(?:meals?|food|dining)\b|\b(?:full|half)[\s-]+board\b", text, re.IGNORECASE):
        return ALL
    named = frozenset(_tokens(text))
    if named:
        return named
    reaches = _TOURWIDE_RE.search(text) or _REMARK_RE.search(text) or _OTHER_DAY_RE.search(text)
    return ALL if reaches else frozenset()


def itinerary_doubt(blocks: dict[int, str], day: int, meals: frozenset[str]) -> bool:
    """True when a sentence of the source itinerary outside the matched day (any other day's block, which also
    holds any text after the last marker) names a claimed meal and casts doubt on it, so it may reach the claimed
    day: "Note: lunches are at your own expense throughout the trip." on Day 1, or "Important: on private
    departures lunch is not included." after the last day. Jev's per-unit source holds that text too, so the
    rule may not clear what Jev could refuse. Doubt: a strong cue, or a negation-type cue in a remark; reach: a
    tour-wide word or another-day reference (always), else not when the sentence is scoped to its own day. Other
    days' pure lists and code brackets are their own structured facts ("Meals: None"), not remarks."""
    for n, block in blocks.items():
        if n == day:
            continue                                     # the matched day: day_doubt, stricter
        for s in _SENTENCE_SPLIT_RE.split(_narrative(block)):
            if not _sentence_meals(s) & meals:
                continue
            strong = bool(_STRONG_CUE_RE.search(s))
            if not strong and not _NEG_CUE_RE.search(s):
                continue
            if _TOURWIDE_RE.search(s) or _OTHER_DAY_RE.search(s):
                return True
            if _SAME_DAY_RE.search(s):
                continue
            if strong or _REMARK_RE.search(s):
                return True
    return False


def exclusion_blocks(exclusions: str) -> frozenset[str]:
    """Meals EXCLUSIONS rule out: a named meal, or all three for any generic meal word."""
    text = exclusions or ""
    if re.search(r"\b(?:meals?|food|feasts?|buffets?|picnics?|banquets?|dining|f&b)\b|\b(?:full|half)[\s-]+board\b",
                 text, re.IGNORECASE):
        return ALL
    return frozenset(_tokens(text))


# ── INCLUSIONS ──────────────────────────────────────────────────────────────────────────────────────────────

_MEAL_WORD = r"(?:breakfast|lunch|dinner|supper)"
_MEAL_WORDS = rf"{_MEAL_WORD}(?:\s*(?:,|&|/|\band\b)\s*{_MEAL_WORD}){{0,2}}"
_EVERY_DAY = r"(?:daily|every\s*day|each\s+day|everyday|every\s+morning|each\s+morning|every\s+evening|each\s+evening)"
_TAIL = r"(?:\s+(?:included|provided|inclusive))?\s*\.?"
_INCLUSION_FORMS = (
    (re.compile(rf"^(?:all|three)\s+meals(?:\s+(?:a|per)\s+day|\s+daily)?{_TAIL}$", re.IGNORECASE), ALL),
    (re.compile(rf"^(?:all\s+meals\s+)?\(\s*breakfast\s*,\s*lunch\s*(?:,|and|&)\s*dinner\s*\){_TAIL}$",
                re.IGNORECASE), ALL),
    (re.compile(rf"^full[\s-]+board(?:\s+(?:basis|meals))?{_TAIL}$", re.IGNORECASE), ALL),
    (re.compile(rf"^half[\s-]+board(?:\s+basis)?{_TAIL}$", re.IGNORECASE), frozenset({B})),
    (re.compile(rf"^{_EVERY_DAY}\s+(?P<m>{_MEAL_WORDS}){_TAIL}$", re.IGNORECASE), None),
    (re.compile(rf"^(?P<m>{_MEAL_WORDS})\s+{_EVERY_DAY}{_TAIL}$", re.IGNORECASE), None),
)
# A line with any of these is never split on commas (a later segment may scope an earlier one).
_LINE_SCOPE_RE = re.compile(
    r"\d|[–—]|\s-\s|\b(?:days?|nights?|trek\w*|hik\w*|safari|cruise|boat|train|camp\w*|tent\w*|homestay|teahouse"
    r"|lodge\w*"
    r"|during|while|when|except|excluding|exclude\w*|not|no|only|from|until|till|starting|onwards?|if|optional"
    r"|supplement|upgrade|extra|own|payable|but|apart|other|first|last|arrival|departure|welcome|farewell"
    r"|specified|mentioned|indicated|per|itinerary|programme|program|listed|as|where|some|selected|certain|most"
    r"|between|before|after|in|at|on|for|with|of|to|by)\b",
    re.IGNORECASE)
_INLINE_SPLIT_RE = re.compile(r"[;•·▪●|]|\s\*\s")
_BULLET_RE = re.compile(r"^(\s*)(?:[•·▪●*\-–]|\d{1,2}[.)])\s+")
_INCL_HEADING_RE = re.compile(
    r"^\s*(?:included|inclusions?|includes|(?:the\s+)?(?:price|tour|trip)\s+includes?)\s*:\s*", re.IGNORECASE)


def _inclusion_lines(inclusions: str) -> Optional[list[str]]:
    """The INCLUSIONS lines without their bullets, or None when the layout can hide a scope: several lines that
    are not all top-level bullets (a plain line may be a heading for the lines under it, "Trek portion"), an
    indented sub-list, or a colon left after the opening "Included:" heading ("Kathmandu: daily breakfast")."""
    text = _INCL_HEADING_RE.sub("", inclusions or "", count=1)
    if ":" in text:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        marks = [_BULLET_RE.match(ln) for ln in lines]
        if not all(marks) or len({m.group(1) for m in marks}) > 1:
            return None
    return [_BULLET_RE.sub("", ln, count=1).strip() for ln in lines]


def _top_level_commas(line: str) -> list[str]:
    """Split on commas outside brackets ("All meals (breakfast, lunch and dinner), guide" is two items)."""
    out, depth, cur = [], 0, []
    for ch in line:
        depth += ch == "("
        depth -= ch == ")" and depth > 0
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _items(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        for seg in _INLINE_SPLIT_RE.split(line):
            seg = seg.strip()
            if not seg:
                continue
            parts = [seg] if _LINE_SCOPE_RE.search(seg) else [p.strip() for p in _top_level_commas(seg)]
            out.extend(p for p in parts if p)
    return out


# Anywhere in INCLUSIONS, these make every item doubtful (an exception or a scope may sit on another line).
_INCL_DOUBT_RE = re.compile(
    r"\b(?:except\w*|exclud\w*|excl|but|not|no|only|apart|other\s+than|unless|optional\w*|supplement\w*|request"
    r"|upgrade\w*|extra|own|if|when|while|during|until|till|starting|onwards?)\b", re.IGNORECASE)
# An item that continues the one before it: "(on trek days)", "on safari days", "and lunch on day 2".
_CONTINUATION_RE = re.compile(
    r"^(?:[(\[]|(?:on|during|while|from|until|till|for|in|at|except|excluding|only|when|starting|but|and|or"
    r"|with|without|plus)\b)", re.IGNORECASE)


def inclusion_meals(inclusions: str) -> frozenset[str]:
    """Meals INCLUSIONS state for every day (see module docstring), minus any meal another item also names."""
    lines = _inclusion_lines(inclusions)
    if lines is None or _INCL_DOUBT_RE.search(inclusions or ""):
        return frozenset()
    covered: set[str] = set()
    rest_mentions: set[str] = set()
    prev_hit: frozenset[str] = frozenset()
    for item in _items(lines):
        norm = " ".join(item.split())
        if prev_hit and _CONTINUATION_RE.match(norm):
            rest_mentions |= prev_hit                    # the item before was qualified by this one
        hit = None
        for rx, meals in _INCLUSION_FORMS:
            m = rx.match(norm)
            if m:
                hit = meals if meals is not None else frozenset(_tokens(m.group("m")))
                break
        prev_hit = hit or frozenset()
        if hit is not None:
            covered |= hit
        elif _MEAL_MENTION_RE.search(norm):
            generic = re.search(r"\b(?:meals?|food|feasts?|buffets?|picnics?|banquets?|board)\b", norm, re.IGNORECASE)
            rest_mentions |= ALL if generic else _tokens(re.sub(r"\b[bld]\b", " ", norm))
    return frozenset(covered - rest_mentions)


# ── day matching ────────────────────────────────────────────────────────────────────────────────────────────

_GROUPED_AFTER_RE = re.compile(
    r"^\s*(?:&|and|\+|,|/|to|through|thru)\s*(?:\d|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE)


def source_day_blocks(itineraries: str, duration: str = "") -> Optional[dict[int, str]]:
    """{n: text} of the source days 1..N when the split can be trusted, else None (see module docstring)."""
    from .day_parse import _DAY_MARKER_RE, parse_duration, source_day_markers

    text = itineraries or ""
    lines = text.splitlines()
    markers = source_day_markers(text)
    if len(markers) < 2:
        return None
    if [first for first, _last, _i in markers] != list(range(1, len(markers) + 1)):
        return None
    if any(first != last for first, last, _i in markers):
        return None
    for _first, _last, idx in markers:
        line = lines[idx].strip()
        m = _DAY_MARKER_RE.match(line)
        if not m or re.match(r"^\W*days\b", line, re.IGNORECASE) or _GROUPED_AFTER_RE.match(line[m.end(1):]):
            return None
    exact = parse_duration(duration).exact_days
    if exact is not None and exact != len(markers):
        return None
    if markers[0][2] > 0 and _MEAL_MENTION_RE.search("\n".join(lines[: markers[0][2]])):
        return None                                      # meal text before Day 1 belongs to no day
    blocks: dict[int, str] = {}
    for i, (n, _last, idx) in enumerate(markers):
        end = markers[i + 1][2] if i + 1 < len(markers) else len(lines)
        first = lines[idx].strip()
        m = _DAY_MARKER_RE.match(first)
        body = [first[m.end():]] + lines[idx + 1:end]
        blocks[n] = "\n".join(body).strip()
    return blocks


def output_day_numbers(claims: list[dict], itin_text: str) -> Optional[list[int]]:
    """The output's "Day N" title numbers when they are exactly 1..N in order, else None."""
    from .grounding import _GEN_DAY_NUM_RE

    nums = [int(m.group(1)) for ln in (itin_text or "").splitlines() if (m := _GEN_DAY_NUM_RE.match(ln))]
    return nums if nums and nums == list(range(1, len(nums) + 1)) else None


_ALIGN_STOP = frozenset("""
    after again along also around arrive arrival back before breakfast city day days depart departure dinner drive
    early evening explore free from full guide head here hotel include included into journey later local lunch meal
    meals morning night noon over overnight provided rest restaurant return road route served short stage start stay
    take that then there this time today tour town transfer trip visit walk well where while with your
""".split())


def _content_words(text: str) -> set[str]:
    return {w.lower().strip("'’-") for w in re.findall(r"[A-Za-z][A-Za-z'’-]{3,}", text or "")} - _ALIGN_STOP


def days_aligned(out_day_text: str, src_block: str) -> bool:
    """A day-count match is not enough (a writer can merge one day and split another): the output day and the
    source day it is matched to must share at least one specific word (a place, a sight)."""
    return bool(_content_words(out_day_text) & _content_words(src_block))


def output_day_text(itin_text: str, day: int) -> str:
    """Title and body of output day ``day`` (canonical "Day N — Title" blocks)."""
    from .grounding import _GEN_DAY_NUM_RE

    out, cur = [], None
    for ln in (itin_text or "").splitlines():
        m = _GEN_DAY_NUM_RE.match(ln)
        if m:
            cur = int(m.group(1))
        if cur == day:
            out.append(ln)
    return "\n".join(out)


def _meal_sentence_doubt(text: str) -> bool:
    """A sentence of the tour's other text (summary, description, price) that names a meal and a cue."""
    for s in _SENTENCE_SPLIT_RE.split(text or ""):
        if _MEAL_MENTION_RE.search(s) and (_WEAK_CUE_RE.search(s) or _STRONG_CUE_RE.search(s)):
            return True
    return False


# ── the rule ────────────────────────────────────────────────────────────────────────────────────────────────

def _as_text(value) -> str:
    from .grounding import _as_text as g_as_text

    return g_as_text(value)


def claim_sourced(claim: dict, tour: dict, itin_text: str) -> tuple[bool, str]:
    """(cleared, reason) for one itinerary claim sentence ({field, sentence, day}) against the raw tour."""
    sentence = claim.get("sentence") or ""
    if claim.get("field", "itineraries") != "itineraries":
        return False, "not_itinerary"
    if output_refused(sentence):
        return False, "output_refused"
    meals = claimed_meals(sentence)
    if meals & exclusion_blocks(_as_text(tour.get("exclusions"))):
        return False, "excluded"
    out_days = output_day_numbers([claim], itin_text)
    day = claim.get("day")
    if not out_days or day is None or day not in out_days:
        return False, "day_unknown"
    blocks = source_day_blocks(_as_text(tour.get("itineraries")), _as_text(tour.get("duration")))
    if blocks is None or sorted(blocks) != out_days:
        return False, "days_unmatched"
    block = blocks[day]
    if _OTHER_DAY_RE.search(block):
        return False, "source_day_names_another_day"
    if day_doubt(block):
        return False, "source_day_doubt"
    if itinerary_doubt(blocks, day, meals):
        return False, "source_itinerary_doubt"
    if _VARIANT_RE.search(block):
        return False, "source_day_has_variants"           # a list or an inclusion may hold for one option only
    if narrative_names(block, meals):
        return False, "source_day_text_names_the_meal"
    if not days_aligned(output_day_text(itin_text, day), block):
        return False, "output_day_not_the_source_day"
    if _meal_sentence_doubt(" \n".join(_as_text(tour.get(f)) for f in ("summary", "description", "price"))):
        return False, "tour_text_doubt"
    lists, impure = day_lists(block)
    if impure:
        return False, "source_list_unreadable"
    if lists:
        if any(lst != lists[0] for lst in lists):
            return False, "source_lists_disagree"
        return (True, "day_list") if meals <= lists[0] else (False, "not_in_day_list")
    if day in (out_days[0], out_days[-1]):
        return False, "first_or_last_day"
    other = " \n".join(_as_text(tour.get(f)) for f in ("summary", "description", "price"))
    if _MEAL_MENTION_RE.search(_TIME_MARKER_RE.sub(" ", other)):
        return False, "tour_text_names_meals"             # "Meals are provided on trek days" scopes INCLUSIONS
    covered = inclusion_meals(_as_text(tour.get("inclusions")))
    if not meals <= covered:
        return False, "not_in_inclusions"
    for other in blocks.values():
        other_lists, other_impure = day_lists(other)
        if other_impure or any(not meals <= lst for lst in other_lists):
            return False, "a_day_list_lacks_the_meal"
    return True, "inclusions"
