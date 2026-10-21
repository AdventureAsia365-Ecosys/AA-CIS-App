"""AA-803 — deterministic check that a meal claim in an S1 itinerary is backed by the raw source.

``brand_audit_node`` raises ``ITINERARY_MEAL_TIME_INVENTED`` when a rewritten itinerary states meal
logistics ("Lunch is served at a local ger camp", "(B, L, D)", "Meals: Breakfast") or an am/pm clock
time. Until now a meal-claim sentence was only cleared by an enforced accept from the Jev question
``a1_claim_supported``. The S246 replay (AA-803 J04) showed that Jev cleared the code on 71 of 914 tours,
and the cases were the source's own meals: the same meal named on the same source day, or in INCLUSIONS
("all meals", "11 breakfasts and 8 dinners", "Chinese lunch"). This module makes that decision in code, so
the Jev question can be switched off without the code firing on every sourced meal.

A meal-claim sentence is SOURCED when every meal it claims is covered for its day:

* the same source day names the meal and does not negate it: a meal word ("enjoy lunch at a local ger
  camp", "picnic lunch", "evening meal" = dinner), a code list ("Meals: B, L, D", "Meals  B D",
  "(B/L)"), or a generic "all meals" / "full board" (B, L, D) / "half board" (B, D);
* or INCLUSIONS names it (same word, code and board rules; "11 breakfasts" counts). "All meals specified
  / as per / indicated in the itinerary" means the meals the itinerary names, so it adds nothing beyond the
  same-day rule (it is never read as "all three meals every day");
* a day whose source carries a structured meal list ("Meals: B", "Meals included: Breakfast | Dinner") is
  authoritative for that day: INCLUSIONS and passing mentions ("after lunch, we...") do not add meals the
  list leaves out; only the same day saying in so many words that the meal is provided does ("A picnic
  lunch on the Wall is provided");
* a meal negated in the inputs ("Lunch is not included", "optional dinner", "dinner at own expense",
  "No meals included today", "Meals: None"; a cue within NEG_WINDOW words of the meal, in the same clause)
  or named in EXCLUSIONS is never covered.

Never sourced: a sentence whose day cannot be matched to one source day (no "Day N" title, or the source
day split fell back on a multi-day tour; a one-day source is its own day) unless INCLUSIONS alone covers
it; a sentence that claims no positive meal after its negated parts are removed (left to the caller,
fail-closed); and a sentence with a clock time (am/pm or 24-hour) the source does not state. Meal TIMES
are never inferred. A meal the source has only on the neighbouring day is not sourced either.
"""
from __future__ import annotations

import re
from typing import Optional

MEALS = ("breakfast", "lunch", "dinner")
ALL = frozenset(MEALS)

_WORD_RES = {
    "breakfast": re.compile(r"\bbreakfasts?\b", re.IGNORECASE),
    "lunch": re.compile(r"\blunch(?:es)?\b|\b(?:picnic|packed)\s+meals?\b", re.IGNORECASE),
    "dinner": re.compile(r"\b(?:dinners?|suppers?|evening\s+meals?)\b", re.IGNORECASE),
}
_CODE_MEAL = {"B": "breakfast", "L": "lunch", "D": "dinner"}
# A code list: 1-3 of B/L/D joined by , / & + - or spaces ("B, L, D", "B D", "BLD", "B/L"). Case-sensitive.
_CODE_LIST = r"[BLD](?:\s*[,/&+\-]?\s*[BLD]){0,2}"
# Codes count only in a meal context, never as a bare capital letter in prose.
_CODE_RES = (
    re.compile(r"(?i:\bmeals?)(?:\s+(?i:included|provided))?\s*[:\-\u2013]?\s*(" + _CODE_LIST + r")(?![A-Za-z])"),
    re.compile(r"[(\[]\s*(" + _CODE_LIST + r")\s*[)\]]"),
    re.compile(r"(?<![A-Za-z])([BLD](?:\s*[/\-]\s*[BLD]){1,2})(?![A-Za-z])"),
)
# A structured per-day meal list: a "Meals" label followed by codes or meal words, or a code list in brackets.
_STRUCTURED_RE = re.compile(
    r"(?i:\bmeals?)(?:\s+(?i:included|provided))?\s*[:\-\u2013]?\s*[\u2022\u00b7*]?\s*(?:"
    + _CODE_LIST + r"(?![A-Za-z])"
    r"|(?i:breakfast|lunch|dinner))"
    r"|[(\[]\s*" + _CODE_LIST + r"\s*[)\]]")
_FULL_BOARD_RE = re.compile(
    r"\bfull[\s-]+board\b|\b(?:all|three|3)\s+(?:daily\s+)?meals\b|\bdaily\s+meals\b"
    r"|\bmeals\s+(?:are\s+)?(?:included|provided)\b(?!\s*[:\-\u2013])",
    re.IGNORECASE)
_HALF_BOARD_RE = re.compile(r"\bhalf[\s-]+board\b", re.IGNORECASE)
# "All meals specified in the itinerary" names no meal by itself: the meals are the ones the itinerary names.
_AS_PER_ITINERARY_RE = re.compile(
    r"\b(?:all\s+)?meals?\s+(?:are\s+)?(?:as\s+)?(?:specified|indicated|mentioned|listed|noted|stated|shown|"
    r"per|described|outlined)\b[^.;\n]{0,25}?\bitinerar(?:y|ies)\b"
    r"|\bmeals?\s+(?:are\s+)?(?:as\s+per|according\s+to|as\s+in)\s+(?:the\s+)?(?:\w+\s+)?itinerar(?:y|ies)\b",
    re.IGNORECASE)
_GENERIC_NEG_RE = re.compile(
    r"\bno[\s-]+meals?\b|\bmeals?\s+(?:are\s+|is\s+)?not\s+(?:included|provided)\b|\bwithout\s+meals\b"
    r"|\bmeals?(?:\s+included)?\s*[:\-\u2013]\s*(?:none|nil|no|not\s+included|n/?a)\b",
    re.IGNORECASE)
_NEG_CUE_RE = re.compile(
    r"\bnot\b(?!\s+(?:only|just|least|to\s+(?:forget|mention|miss))\b)|\bno\b|n['\u2019]t\b|\bnone\b|\bnil\b"
    r"|\bnever\b|\bexclud\w*|\bexcept\b|\bwithout\b|\boptional\b"
    r"|\bown\s+(?:expense|cost|account)\b|\bon\s+your\s+own\b|\bpay\s+(?:for|locally)\b"
    r"|\bextra\s+(?:cost|charge)\b|\bat\s+additional\b|\bsupplement\b|\bself[\s-]+funded\b",
    re.IGNORECASE)
# Clause boundaries for negation scope. Commas do NOT split: "Breakfast, lunch and dinner are not included"
# negates all three (fail-closed when a comma list mixes included and excluded meals). "|" and bullets do not
# split either: "Meals included: Breakfast | Lunch | Dinner" and "Meals Included: \u2022 Breakfast, Lunch" are one list.
_CLAUSE_SPLIT_RE = re.compile(r"[.;!?()\[\]\n]+|\b(?:but|however|whereas|while)\b", re.IGNORECASE)
_AMPM_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b", re.IGNORECASE)
_H24_RE = re.compile(r"(?<![\d.:])(\d{1,2})[:.h](\d{2})(?![\d])(?!\s?(?:am|pm)\b)", re.IGNORECASE)
_GENERIC_MEAL_RE = re.compile(r"\bmeals?\b", re.IGNORECASE)
_PROVIDED_RE = re.compile(r"\b(?:provided|included|served|arranged|supplied|offered|hosted)\b", re.IGNORECASE)


_BRACKET_CODES_RE = re.compile(r"[(\[]\s*(" + _CODE_LIST + r")\s*[)\]]")
_TOKEN_RE = re.compile(r"[A-Za-z']+|[^\sA-Za-z']")
NEG_WINDOW = 6               # a negation cue negates a meal only within this many words of it


def _clauses(text: str) -> list[str]:
    """Negation scopes. A bracketed code list "(L, D)" becomes its own "meals: L, D" clause first, so the
    bracket split below does not lose it."""
    text = _BRACKET_CODES_RE.sub(lambda m: f" ; meals: {m.group(1)} ; ", text or "")
    return [c for c in _CLAUSE_SPLIT_RE.split(text) if c and c.strip()]


def _near(clause: str, meal: str, cue_rx: re.Pattern, *, unnamed: bool) -> bool:
    """True when a ``cue_rx`` match sits within NEG_WINDOW words of a mention of ``meal`` in ``clause``.
    ``unnamed`` is the answer when the clause names the meal only through "all meals" / "full board"
    (then the whole clause is the scope)."""
    spans = [m.span() for m in _WORD_RES[meal].finditer(clause)]
    for rx in _CODE_RES:
        for m in rx.finditer(clause):
            if any(_CODE_MEAL.get(ch) == meal for ch in m.group(1)):
                spans.append(m.span())
    if not spans:
        return unnamed and bool(cue_rx.search(clause))
    for cue in cue_rx.finditer(clause):
        for start, end in spans:
            lo, hi = (cue.end(), start) if cue.end() <= start else (end, cue.start())
            if hi <= lo or len(_TOKEN_RE.findall(clause[lo:hi])) <= NEG_WINDOW:
                return True
    return False


def _negated(clause: str, meal: str) -> bool:
    """A negation cue within NEG_WINDOW words of the meal ("lunch is not included", "optional dinner"), not
    anywhere in a long sentence ("exploring the village on your own, relaxing at the hot springs and having
    a group dinner" keeps the dinner)."""
    return _near(clause, meal, _NEG_CUE_RE, unnamed=True)


def _stated_as_provided(clause: str, meal: str) -> bool:
    """The source says in so many words that the meal is provided ("A picnic lunch on the Wall is provided",
    "a picnic lunch that is provided by your guide")."""
    return _near(clause, meal, _PROVIDED_RE, unnamed=False)


def _meals_named(text: str) -> set[str]:
    """Meals a piece of text names: words, codes in a meal context, full/half board, generic all-meals."""
    found = {m for m, rx in _WORD_RES.items() if rx.search(text)}
    for rx in _CODE_RES:
        for m in rx.finditer(text):
            found |= {_CODE_MEAL[ch] for ch in m.group(1) if ch in _CODE_MEAL}
    if _HALF_BOARD_RE.search(text):
        found |= {"breakfast", "dinner"}
    if _FULL_BOARD_RE.search(_AS_PER_ITINERARY_RE.sub(" ", text)):
        found |= ALL
    return found


def meal_mentions(text: str) -> tuple[set[str], set[str]]:
    """(positive, negated) meals of ``text``, clause by clause. A clause with a negation cue negates every
    meal it names; a generic "no meals" / "meals not included" negates all three."""
    pos: set[str] = set()
    neg: set[str] = set()
    for clause in _clauses(text):
        named = _meals_named(clause)
        if _GENERIC_NEG_RE.search(clause):
            neg |= ALL
            continue
        if not named:
            continue
        for meal in named:
            (neg if _negated(clause, meal) else pos).add(meal)
    return pos, neg


def provided_meals(text: str) -> set[str]:
    """Meals the text states as provided in so many words (and does not negate)."""
    out: set[str] = set()
    for clause in _clauses(text):
        if _GENERIC_NEG_RE.search(clause):
            continue
        out |= {m for m in _meals_named(clause) if _stated_as_provided(clause, m) and not _negated(clause, m)}
    return out


def structured_meals(text: str) -> Optional[set[str]]:
    """The meals of a structured per-day list ("Meals: B, D", "Meals included: Breakfast", "(B/L)"), or
    None when the text has no such list."""
    if not _STRUCTURED_RE.search(text or ""):
        return None
    out: set[str] = set()
    for clause in _clauses(text):
        if _STRUCTURED_RE.search(clause):
            out |= {m for m in _meals_named(clause) if not _negated(clause, m)}
    return out


def claimed_meals(sentence: str) -> tuple[set[str], bool]:
    """(meals the output sentence claims as provided, generic_claim). Negated parts are dropped
    ("Breakfast is included; lunch is at your own expense" claims breakfast only). ``generic_claim`` is
    True when the sentence states meal logistics without naming a meal ("Meals included:")."""
    pos, _neg = meal_mentions(sentence)
    generic = not pos and bool(_GENERIC_MEAL_RE.search(sentence)) and not _NEG_CUE_RE.search(sentence)
    return pos, generic


def _clock_times(text: str) -> set[str]:
    out = {re.sub(r"\s+", "", m.group(0).lower()) for m in _AMPM_RE.finditer(text or "")}
    out |= {f"{int(m.group(1))}:{m.group(2)}" for m in _H24_RE.finditer(text or "")}
    return out


def _times_unsupported(sentence: str, source_all: str) -> bool:
    return bool(_clock_times(sentence) - _clock_times(source_all))


def day_coverage(day_text: Optional[str], inclusions: str, exclusions: str) -> set[str]:
    """Meals the source covers for one output day. ``day_text`` None = the day could not be matched to one
    source day (only INCLUSIONS can cover then)."""
    inc_pos, inc_neg = meal_mentions(inclusions)
    exc_named = _meals_named(_AS_PER_ITINERARY_RE.sub(" ", exclusions or ""))
    # A generic exclusion ("meals not mentioned in the itinerary", "drinks with meals") names no specific meal.
    exc_specific = {m for m, rx in _WORD_RES.items() if rx.search(exclusions or "")} & exc_named
    blocked = set(inc_neg) | exc_specific
    if day_text is None:
        return inc_pos - blocked
    day_pos, day_neg = meal_mentions(day_text)
    blocked |= day_neg
    listed = structured_meals(day_text)
    if listed is not None:
        # The day's own list wins over INCLUSIONS and passing mentions ("after lunch, we..."), but not over the
        # same day saying the meal is provided ("A picnic lunch on the Wall is provided. ... Meals: Breakfast").
        return (listed | provided_meals(day_text)) - blocked
    return (day_pos | inc_pos) - blocked


def meal_claim_sourced(sentence: str, *, day_text: Optional[str], inclusions: str = "", exclusions: str = "",
                       source_all: str = "") -> tuple[bool, str]:
    """(sourced, reason) for one output sentence that matched the meal-claim regex. ``source_all`` is the
    full labelled source (clock times are checked against it)."""
    if _times_unsupported(sentence, source_all):
        return False, "clock_time_not_in_source"
    claimed, generic = claimed_meals(sentence)
    cover = day_coverage(day_text, inclusions, exclusions)
    if not claimed:
        if generic and cover:
            return True, "generic_meals_covered"
        if _GENERIC_NEG_RE.search(sentence) and not cover and day_text is not None:
            return True, "no_meals_as_source"     # "Meals: None included." on a day the source gives no meal
        return False, "no_positive_meal_claim"
    missing = claimed - cover
    if missing:
        where = "day_unmatched" if day_text is None else "not_on_source_day"
        return False, f"{where}:{','.join(sorted(missing))}"
    return True, "sourced"


def itinerary_sentences(itin_text: str) -> list[dict]:
    """Every sentence of an output itinerary as {field, sentence, day}, split exactly like
    ``grounding.sentence_units`` but keeping day-title lines and sentences shorter than MIN_WORDS: a title
    "Day 3 — Hue (B, L)" or a bare "Lunch is included." is a meal claim too."""
    from services.content_generation import grounding as g

    out: list[dict] = []
    day: Optional[int] = None
    for ln in (itin_text or "").splitlines():
        if not ln.strip():
            continue
        m = g._GEN_DAY_NUM_RE.match(ln)
        if m:
            day = int(m.group(1))
        for c in g._SENT_SPLIT_RE.split(ln):
            s = c.strip()
            if s:
                out.append({"field": "itineraries", "sentence": s, "day": day})
    return out


def unit_meal_sourced(unit: dict, tour: dict, days: tuple[dict[int, str], bool]) -> tuple[bool, str]:
    """``meal_claim_sourced`` for a grounding unit ({field, sentence, day}) of ``tour``. ``days`` is
    ``grounding._source_days(tour)`` (computed once per tour by the caller)."""
    from services.content_generation import grounding as g
    from services.content_generation.day_parse import source_day_markers

    src_itin = g._as_text(tour.get("itineraries"))
    day_text_map, used_fallback = days
    day = unit.get("day")
    if len(day_text_map) == 1 and all(last <= 1 for _f, last, _i in source_day_markers(src_itin)):
        # A one-day source (a day tour: no "Day N" marker past Day 1): its whole text is that day, whatever
        # the output calls it.
        day_text: Optional[str] = next(iter(day_text_map.values()))
    elif used_fallback or day is None:
        day_text = None           # never guess which source day a sentence belongs to
    else:
        day_text = day_text_map.get(day)
    return meal_claim_sourced(unit["sentence"], day_text=day_text,
                              inclusions=g._as_text(tour.get("inclusions")),
                              exclusions=g._as_text(tour.get("exclusions")),
                              source_all=g.source_text(tour))
