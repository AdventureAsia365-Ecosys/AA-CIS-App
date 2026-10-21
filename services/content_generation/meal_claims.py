"""AA-803 — deterministic check that a meal claim in an S1 itinerary is backed by the raw source.

``brand_audit_node`` raises ``ITINERARY_MEAL_TIME_INVENTED`` when a rewritten itinerary states meal
logistics ("Lunch is served at a local ger camp", "(B, L, D)", "Meals: Breakfast") or an am/pm clock
time. Until now a meal-claim sentence was only cleared by an enforced accept from the Jev question
``a1_claim_supported``. The S246 replay (AA-803 J04) showed that Jev cleared the code on 71 of 914 tours,
and the cases were the source's own meals. This module makes that decision in code, so the Jev question
can be switched off without the code firing on every sourced meal. Everything it does not clear stays
with the caller (Jev while the question is on, the code once it is off): it must never clear a meal the
source does not provide.

A meal-claim sentence is SOURCED when every meal it claims is covered for its day:

* the same source day names the meal and does not negate it: a meal word ("enjoy lunch at a local ger
  camp", "picnic lunch", "evening meal" = dinner), a code list ("Meals: B, L, D", "Meals  B D",
  "(B/L)"), or a generic "all meals" / "full board" (B, L, D) / "half board" (B, D);
* or INCLUSIONS covers it for EVERY day: only an unqualified daily form does ("daily breakfast",
  "breakfast every morning", full or half board, "all meals" not tied to the itinerary, a count at least
  the number of days: "11 breakfasts" on an 11-day tour). A qualified mention ("welcome dinner",
  "1 lunch", "Lunch on Day 2", "Chinese lunch", "8 dinners" on 11 days) counts only through the same
  day's own text. "Meals [included] as per / in / specified in / where indicated in the itinerary" and
  a code legend ("B = Breakfast, L = Lunch, D = Dinner") add nothing;
* a day whose source carries a structured meal list ("Meals: B", "Meals included: Breakfast | Dinner") is
  authoritative for that day: INCLUSIONS and passing mentions ("after lunch, we...") do not add meals the
  list leaves out; only the same day saying in so many words that the meal is provided does ("A picnic
  lunch on the Wall is provided");
* a meal the source negates is never covered, and neither is a meal named in EXCLUSIONS. Negation, source
  side, in tiers: HARD ("not included", "own expense", "own account", "payable", "self-pay", "excl.") over
  the meal's comma segment within HARD_WINDOW words, or back over the clause from a bare predicate ("Dinner
  at the hotel restaurant, which is famous for its wine, is not included"); EXPLICIT within NEG_WINDOW
  words ("optional dinner", "lunch on own", "dinner can be arranged on request", "your guide can recommend
  a restaurant for dinner"); WEAK ("not", "no") within NEG_WINDOW words and SOFT ("free time for lunch",
  "tomorrow's dinner") before the meal or right after it. WEAK and SOFT give way to the same day's list or
  "provided" statement; HARD and EXPLICIT never do. A bracket that holds a cue and no meal negates the
  segment before it ("lunch at a local restaurant (own account)", "lunch ... (optional, not included)");
  generic forms ("No meals included today", "Meals: None") negate all three.

Output side, a meal is dropped from the claim only on an explicit exclusion next to it ("lunch is not
included", "optional dinner", "dinner at your own expense"). Any other "not" / "no" keeps the meal in the
claim ("dinner is served at a restaurant not far from the river" still claims dinner).

Never sourced: a sentence whose day cannot be matched to one source day (no "Day N" title, or the source
day split fell back on a multi-day tour; a one-day source is its own day) unless INCLUSIONS alone covers
it; a sentence that claims no positive meal (left to the caller, fail-closed); and a sentence with a time
the same source day (or INCLUSIONS) does not state: am/pm, 24-hour, "noon", "7 o'clock". Meal TIMES are
never inferred. A meal the source has only on the neighbouring day is not sourced either.
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
_MEAL_WORD = r"(?:breakfasts?|lunch(?:es)?|dinners?|suppers?)"
_CODE_MEAL = {"B": "breakfast", "L": "lunch", "D": "dinner"}
# A code list: 1-3 of B/L/D joined by , / & + - or spaces ("B, L, D", "B D", "BLD", "B/L"). Case-sensitive.
_CODE_LIST = r"[BLD](?:\s*[,/&+\-]?\s*[BLD]){0,2}"
# Codes count only in a meal context, never as a bare capital letter in prose.
_CODE_RES = (
    re.compile(r"(?i:\bmeals?)(?:\s+(?i:included|provided))?\s*[:\-\u2013]?\s*(" + _CODE_LIST + r")(?![A-Za-z])"),
    re.compile(r"[(\[]\s*(" + _CODE_LIST + r")\s*[)\]]"),
    re.compile(r"(?<![A-Za-z])([BLD](?:\s*[/\-]\s*[BLD]){1,2})(?![A-Za-z])"),
)
# A structured per-day meal list: a "Meals" label followed by codes or meal words (a bracketed code list
# "(L, D)" is rewritten to "meals: L, D" by _normalise first).
_STRUCTURED_RE = re.compile(
    r"(?i:\bmeals?)(?:\s+(?i:included|provided))?\s*[:\-\u2013]?\s*[\u2022\u00b7*]?\s*(?:"
    + _CODE_LIST + r"(?![A-Za-z])"
    r"|(?i:breakfast|lunch|dinner))")
_FULL_BOARD_RE = re.compile(
    r"\bfull[\s-]+board\b|\b(?:all|three|3)\s+(?:daily\s+)?meals\b|\bdaily\s+meals\b|\bmeals\s+daily\b",
    re.IGNORECASE)
_HALF_BOARD_RE = re.compile(r"\bhalf[\s-]+board\b", re.IGNORECASE)
# "All meals specified in the itinerary", "Meals included as per itinerary", "Meals included where indicated"
# name no meal by themselves: the meals are the ones the itinerary names. Removed before meals are read.
_ITIN_WORD = r"(?:itinerar(?:y|ies)|program(?:me)?s?|schedule)"
_SAID_VERB = r"(?:specified|indicated|mentioned|listed|noted|stated|shown|described|outlined|detailed|marked)"
_MEALS_HEAD = r"\b(?:all\s+)?(?:the\s+)?meals?\s+(?:are\s+|is\s+)?(?:(?:included|provided|served)\s+)?"
_AS_PER_ITINERARY_RE = re.compile(
    _MEALS_HEAD + r"(?:"
    r"(?:as|where|when)\s+" + _SAID_VERB + r"\b(?:[^.;\n]{0,25}?\b" + _ITIN_WORD + r"\b)?"
    r"|" + _SAID_VERB + r"\s+(?:in|on|by)\s+(?:the\s+)?(?:\w+\s+)?" + _ITIN_WORD + r"\b"
    r"|(?:as\s+per|per|according\s+to|as\s+in|in|on|during|following)\s+(?:the\s+)?(?:\w+\s+)?" + _ITIN_WORD
    + r"\b)",
    re.IGNORECASE)
# A code legend: "B = Breakfast, L = Lunch, D = Dinner", "Breakfast (B), Lunch (L)". Two or more pairs.
_LEGEND_PAIR_RE = re.compile(
    r"(?<![A-Za-z])[BLD]\s*[=:\-\u2013]\s*(?i:" + _MEAL_WORD + r")\b"
    r"|\b(?i:" + _MEAL_WORD + r")\s*[=:]?\s*[(\[]\s*[BLD]\s*[)\]]")
# "with no extra charge", "free of charge": a positive statement, not a negation.
_FREE_RE = re.compile(
    r"\b(?:at\s+|with\s+)?no\s+(?:extra|additional)\s+(?:cost|charge|fee)s?\b|\bfree\s+of\s+charge\b"
    r"|\bwithout\s+(?:any\s+)?(?:extra|additional)\s+(?:cost|charge|fee)s?\b|\bat\s+no\s+(?:extra\s+)?cost\b",
    re.IGNORECASE)
_GENERIC_NEG_RE = re.compile(
    r"\bno[\s-]+meals?\b|\bmeals?\s+(?:are\s+|is\s+)?not\s+(?:included|provided)\b|\bwithout\s+meals\b"
    r"|\bmeals?(?:\s+included)?\s*[:\-\u2013]\s*(?:none|nil|no|not\s+included|n/?a)\b",
    re.IGNORECASE)
# Source-side negation comes in tiers.
# HARD: an exclusion in so many words. It negates every meal of its comma segment within HARD_WINDOW words
# ("Enjoy lunch at a recommended local restaurant or 5-star hotel, not included"), and reaches back over the
# clause when its segment is a bare predicate ("Dinner at the hotel restaurant, which ..., is not included").
_HARD_NEG_RE = re.compile(
    r"\bnot\s+(?:be\s+)?(?:included|provided|inclusive|covered|part\s+of)\b|\bexclud\w*|\bexcl\b"
    r"|\bexclusive\s+of\b|\bown\s+(?:expense|cost|account)\b|\bself[\s-]+(?:pay|paid|funded)\b|\bpayable\b"
    r"|\bfor\s+purchase\b|\b(?:extra|additional)\s+(?:cost|charge|fee)s?\b|\bsurcharge\b|\bat\s+additional\b",
    re.IGNORECASE)
# EXPLICIT: the meal is the traveller's to arrange or pay ("optional dinner", "lunch on own", "dinner can be
# arranged on request", "your guide can recommend a restaurant for dinner"). Within NEG_WINDOW words.
_EXPLICIT_CUE_RE = re.compile(
    r"\boptional\b|\bunless\b|\bown\s+(?:choice|arrangement)s?\b|\bon\s+(?:your|their|our|one['’]s)?\s*own\b"
    r"|\bpay\s+(?:for|locally|directly|separately)\b|\bpurchas\w*|\bbuy\b|\bsupplement\b"
    r"|\bcan\s+be\s+(?:arranged|booked|organi[sz]ed|purchased|bought|added|requested)\b"
    r"|\b(?:on|upon|by)\s+request\b|\bupgrade\w*|\brecommend\w*|\bsuggest\w*",
    re.IGNORECASE)
# WEAK: a plain negation word within NEG_WINDOW words. SOFT: a cue that the meal is not on the programme
# ("free time for lunch", "lunch at leisure", "tomorrow's dinner", "lunch available"): within NEG_WINDOW words
# before the meal, or SOFT_AFTER words after it ("after breakfast, free time" keeps the breakfast).
# WEAK and SOFT give way to the same day's explicit list or "provided" statement; HARD and EXPLICIT do not.
_WEAK_CUE_RE = re.compile(
    r"\bnot\b(?!\s+(?:only|just|least|to\s+(?:forget|mention|miss))\b)|\bno\b|n['’]t\b|\bnone\b|\bnil\b"
    r"|\bnever\b|\bexcept\b|\bwithout\b",
    re.IGNORECASE)
_SOFT_CUE_RE = re.compile(
    r"\bat\s+(?:your\s+)?(?:own\s+)?leisure\b|\bfree\s+time\b|\bat\s+will\b|\bavailable\b"
    r"|\btomorrow\b|\bnext\s+(?:day|morning|evening)\b|\bif\s+(?:you|time)\b",
    re.IGNORECASE)
# Any cue at all (used where a clause names meals only through "all meals" / "full board").
_ANY_CUE_RE = re.compile("|".join(rx.pattern for rx in (_HARD_NEG_RE, _EXPLICIT_CUE_RE, _WEAK_CUE_RE,
                                                        _SOFT_CUE_RE)), re.IGNORECASE)
# A bracket holding one of these, and no meal, negates the segment before it: "lunch (own account)",
# "lunch at a local restaurant (optional, not included)". A bracket with only a soft cue is read inline.
_BRACKET_NEG_RE = re.compile("|".join(rx.pattern for rx in (_HARD_NEG_RE, _EXPLICIT_CUE_RE, _WEAK_CUE_RE)),
                             re.IGNORECASE)
# Output side: only an explicit exclusion next to the meal drops it from the claim.
_OUT_EXCL_RE = re.compile(
    _HARD_NEG_RE.pattern + r"|\boptional\b|\bon\s+(?:your|their|one['’]s)\s+own\b"
    r"|\bno\s+(?:\w+\s+)?" + _MEAL_WORD + r"\b|\bwithout\s+(?:a\s+)?" + _MEAL_WORD + r"\b",
    re.IGNORECASE)
# Clause boundaries for negation scope. Commas do NOT split: "Breakfast, lunch and dinner are not included"
# negates all three (fail-closed when a comma list mixes included and excluded meals). "|" and bullets do not
# split either: "Meals included: Breakfast | Lunch | Dinner" is one list. Brackets are handled by _normalise.
_CLAUSE_SPLIT_RE = re.compile(r"[.;!?\n]+|\b(?:but|however|whereas|while)\b", re.IGNORECASE)
# A comma segment that is only a predicate ("is not included") belongs to the subject before it.
_BARE_PREDICATE_RE = re.compile(r"\s*(?:which\s+|that\s+)?(?:is|are|was|were|will\s+be|be|remains?)\b",
                                re.IGNORECASE)
_AMPM_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b", re.IGNORECASE)
_H24_RE = re.compile(r"(?<![\d.:])(\d{1,2})[:.h](\d{2})(?![\d])(?!\s?(?:am|pm)\b)", re.IGNORECASE)
_NUM_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
              "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty")
_NUM_WORD_RE = "|".join(_NUM_WORDS)
# Times in words. "Midday" / "afternoon" are parts of the day, not times, and are not checked.
_WORD_TIME_RE = re.compile(
    r"\b(?:noon|midnight)\b|\b(?:\d{1,2}|" + _NUM_WORD_RE + r")\s*o['’]?\s*clock\b"
    r"|\b(?:half|quarter)\s+(?:past|to)\s+(?:\d{1,2}|" + _NUM_WORD_RE + r")\b",
    re.IGNORECASE)
_GENERIC_MEAL_RE = re.compile(r"\bmeals?\b", re.IGNORECASE)
_PROVIDED_RE = re.compile(r"\b(?:provided|included|served|arranged|supplied|offered|hosted)\b", re.IGNORECASE)
_DAILY_RE = re.compile(
    r"\bdaily\b|\b(?:every|each)\s+(?:day|morning|evening|night)\b|\bper\s+day\b|\ba\s+day\b|\bnightly\b"
    r"|\bthroughout\s+(?:the\s+)?(?:tour|trip|trek|stay|journey|holiday)\b",
    re.IGNORECASE)
_COUNT_RE = re.compile(
    r"(?<![Dd]ay\s)(?<![Dd]ay)\b(\d{1,2}|" + _NUM_WORD_RE + r")\s*(?:x\s*)?(?:[A-Za-z\-]+\s+)?("
    + _MEAL_WORD + r")\b", re.IGNORECASE)

_BRACKET_CODES_RE = re.compile(r"[(\[]\s*(" + _CODE_LIST + r")\s*[)\]]")
_BRACKET_RE = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
_TOKEN_RE = re.compile(r"[A-Za-z']+|[^\sA-Za-z']")
NEG_WINDOW = 6               # source side: a negation cue negates a meal only within this many words of it
SOFT_AFTER = 1               # ... a soft cue only this close when it follows the meal
HARD_WINDOW = 12             # a hard exclusion reaches this far inside its comma segment
OUT_WINDOW = 3               # output side: an explicit exclusion drops a meal only this close to it


def _word_meals(text: str) -> set[str]:
    return {m for m, rx in _WORD_RES.items() if rx.search(text)}


def _normalise(text: str, bracket_rx: re.Pattern) -> str:
    """One pass before clauses are read: "no extra charge" is positive, a code legend and "meals as per the
    itinerary" are removed, a bracketed code list "(L, D)" becomes its own "meals: L, D" clause, a bracket
    that names a meal becomes its own clause ("(Lunch is not included)"), a bracket with a ``bracket_rx``
    cue and no meal becomes a hard "not included" closing the segment before it ("lunch (own account)",
    and "[No cycling] After breakfast" leaves the breakfast alone), and any other bracket is read inline."""
    t = _FREE_RE.sub(" included ", text or "")
    if len(_LEGEND_PAIR_RE.findall(t)) >= 2:
        t = _LEGEND_PAIR_RE.sub(" ", t)
    t = _BRACKET_CODES_RE.sub(lambda m: f" ; meals: {m.group(1)} ; ", t)

    def bracket(m: re.Match) -> str:
        inner = m.group(1)
        if _word_meals(inner):
            return f" ; {inner} ; "
        if bracket_rx.search(inner):
            return " not included , "
        return f" {inner} "

    for _ in range(3):                      # nested brackets, innermost first
        nxt = _BRACKET_RE.sub(bracket, t)
        if nxt == t:
            break
        t = nxt
    return _AS_PER_ITINERARY_RE.sub(" ", t)


def _clauses(text: str, bracket_rx: re.Pattern = _BRACKET_NEG_RE) -> list[str]:
    return [c for c in _CLAUSE_SPLIT_RE.split(_normalise(text, bracket_rx)) if c and c.strip()]


def _explicit_spans(clause: str, meal: str) -> list[tuple[int, int]]:
    """Where ``clause`` names ``meal`` by a word or a code."""
    spans = [m.span() for m in _WORD_RES[meal].finditer(clause)]
    for rx in _CODE_RES:
        for m in rx.finditer(clause):
            if any(_CODE_MEAL.get(ch) == meal for ch in m.group(1)):
                spans.append(m.span())
    return spans


def _gap(clause: str, a: int, b: int) -> int:
    return len(_TOKEN_RE.findall(clause[a:b])) if b > a else 0


def _near(clause: str, spans: list[tuple[int, int]], cue_rx: re.Pattern, before: int,
          after: Optional[int] = None) -> bool:
    """True when a ``cue_rx`` match sits within ``before`` words ahead of one of ``spans``, or within
    ``after`` (default the same) words behind it."""
    after = before if after is None else after
    for cue in cue_rx.finditer(clause):
        for start, end in spans:
            if cue.end() <= start:
                if _gap(clause, cue.end(), start) <= before:
                    return True
            elif cue.start() >= end:
                if _gap(clause, end, cue.start()) <= after:
                    return True
            else:
                return True                 # overlapping ("self-pay lunch" never overlaps; "excl." might)
    return False


def _hard_negated(clause: str, spans: list[tuple[int, int]], hard_rx: re.Pattern = _HARD_NEG_RE) -> bool:
    """A hard exclusion in the meal's comma segment within HARD_WINDOW words, or in a bare-predicate segment
    after it ("Dinner at the hotel restaurant, which is famous for its wine, is not included")."""
    for seg in re.finditer(r"[^,]+", clause):
        cues = list(hard_rx.finditer(seg.group(0)))
        if not cues:
            continue
        if _BARE_PREDICATE_RE.match(seg.group(0)) and not _word_meals(seg.group(0)):
            if any(e <= seg.start() for _s, e in spans):
                return True
            continue
        for cue in cues:
            c0, c1 = seg.start() + cue.start(), seg.start() + cue.end()
            for s, e in spans:
                if seg.start() <= s and e <= seg.end():
                    if _gap(clause, e, c0) <= HARD_WINDOW and _gap(clause, c1, s) <= HARD_WINDOW:
                        return True
    return False


def _negation(clause: str, meal: str) -> Optional[str]:
    """Source side: "hard" when the clause excludes ``meal`` outright (HARD / EXPLICIT tiers), "weak" for a
    plain negation word or a soft cue near it, None when it does not negate it. When the clause names the
    meal only through "all meals" / "full board", any cue in the clause negates it (hard)."""
    spans = _explicit_spans(clause, meal)
    if not spans:
        if _HARD_NEG_RE.search(clause) or _EXPLICIT_CUE_RE.search(clause):
            return "hard"
        return "weak" if _WEAK_CUE_RE.search(clause) or _SOFT_CUE_RE.search(clause) else None
    if _hard_negated(clause, spans) or _near(clause, spans, _EXPLICIT_CUE_RE, NEG_WINDOW):
        return "hard"
    if _near(clause, spans, _WEAK_CUE_RE, NEG_WINDOW) or _near(clause, spans, _SOFT_CUE_RE, NEG_WINDOW, SOFT_AFTER):
        return "weak"
    return None


def _negated(clause: str, meal: str) -> bool:
    return _negation(clause, meal) is not None


def _stated_as_provided(clause: str, meal: str) -> bool:
    """The source says in so many words that the meal is provided ("A picnic lunch on the Wall is provided",
    "a picnic lunch that is provided by your guide")."""
    return _near(clause, _explicit_spans(clause, meal), _PROVIDED_RE, NEG_WINDOW)


def _generic_meals(text: str) -> set[str]:
    out: set[str] = set()
    if _HALF_BOARD_RE.search(text):
        out |= {"breakfast", "dinner"}
    if _FULL_BOARD_RE.search(text):
        out |= ALL
    return out


def _meals_named(text: str) -> set[str]:
    """Meals a piece of (normalised) text names: words, codes in a meal context, full/half board, all meals."""
    found = _word_meals(text)
    for rx in _CODE_RES:
        for m in rx.finditer(text):
            found |= {_CODE_MEAL[ch] for ch in m.group(1) if ch in _CODE_MEAL}
    return found | _generic_meals(text)


def _mentions(text: str) -> tuple[set[str], set[str], set[str]]:
    """Source side: (positive, hard-negated, weak-negated) meals of ``text``, clause by clause; a generic
    "no meals" / "meals not included" hard-negates all three."""
    pos: set[str] = set()
    hard: set[str] = set()
    weak: set[str] = set()
    for clause in _clauses(text):
        if _GENERIC_NEG_RE.search(clause):
            hard |= ALL
            continue
        for meal in _meals_named(clause):
            kind = _negation(clause, meal)
            (pos if kind is None else hard if kind == "hard" else weak).add(meal)
    return pos, hard, weak


def meal_mentions(text: str) -> tuple[set[str], set[str]]:
    """Source side: (positive, negated) meals of ``text``."""
    pos, hard, weak = _mentions(text)
    return pos, hard | weak


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
    clauses = _clauses(text)
    if not any(_STRUCTURED_RE.search(c) for c in clauses):
        return None
    out: set[str] = set()
    for clause in clauses:
        if _STRUCTURED_RE.search(clause) and not _GENERIC_NEG_RE.search(clause):
            out |= {m for m in _meals_named(clause) if not _negated(clause, m)}
    return out


def _count_of(word: str) -> int:
    return int(word) if word.isdigit() else _NUM_WORDS.index(word.lower()) + 1


def _count_covers(meal: str, count: int, n_days: int, day: Optional[int]) -> bool:
    """A count covers every day when it reaches the tour length, and the nights for breakfast (days 2..N)
    or dinner (days 1..N-1) when it reaches N-1 ("11 breakfasts" on a 12-day tour with 11 nights)."""
    if count >= n_days:
        return True
    if count >= n_days - 1 and day is not None and n_days > 1:
        return (meal == "breakfast" and 2 <= day <= n_days) or (meal == "dinner" and 1 <= day <= n_days - 1)
    return False


def inclusion_meals(inclusions: str, n_days: Optional[int] = None,
                    day: Optional[int] = None) -> tuple[set[str], set[str]]:
    """(meals INCLUSIONS cover on output day ``day``, meals INCLUSIONS negate). Only an unqualified daily
    form covers a day: "daily breakfast" / "breakfast every morning", full or half board, "all meals" (not tied
    to the itinerary: that phrase is removed first), or a count that reaches the tour length (see
    ``_count_covers``). "Welcome dinner", "1 lunch", "Lunch on Day 2", "Chinese lunch", "8 dinners" on 11 days
    cover nothing here: they count only through the same day's own text. On a one-day tour every named meal
    is that day's."""
    cover: set[str] = set()
    neg: set[str] = set()
    for clause in _clauses(inclusions):
        if _GENERIC_NEG_RE.search(clause):
            neg |= ALL
            continue
        explicit = {m for m in MEALS if _explicit_spans(clause, m)}
        negated = {m for m in explicit if _negated(clause, m)}
        neg |= negated
        generic = _generic_meals(clause)
        if generic and not _ANY_CUE_RE.search(clause):
            cover |= generic
        for meal in explicit - negated:
            if n_days == 1 or _near(clause, _explicit_spans(clause, meal), _DAILY_RE, NEG_WINDOW):
                cover.add(meal)
        if n_days:
            for m in _COUNT_RE.finditer(clause):
                meal = next((k for k, rx in _WORD_RES.items() if rx.fullmatch(m.group(2))), None)
                if meal and meal not in negated and _count_covers(meal, _count_of(m.group(1)), n_days, day):
                    cover.add(meal)
    return cover - neg, neg


def _out_excluded(clause: str, meal: str) -> bool:
    """Output side: an explicit exclusion next to the meal ("lunch is not included", "optional dinner")."""
    spans = _explicit_spans(clause, meal)
    if not spans:
        return bool(_OUT_EXCL_RE.search(clause))
    return _hard_negated(clause, spans) or _near(clause, spans, _OUT_EXCL_RE, OUT_WINDOW)


def claimed_meals(sentence: str) -> tuple[set[str], bool]:
    """(meals the output sentence claims as provided, generic_claim). A meal is dropped only on an explicit
    exclusion next to it ("Breakfast is included; lunch is at your own expense" claims breakfast only); any
    other "not" / "no" keeps it ("dinner is served at a restaurant not far from the river" claims dinner).
    ``generic_claim`` is True when the sentence states meal logistics without naming a meal ("Meals
    included:")."""
    claimed: set[str] = set()
    for clause in _clauses(sentence, _OUT_EXCL_RE):
        if _GENERIC_NEG_RE.search(clause):
            # "No meals are included except breakfast": a meal named next to a generic "no meals" is claimed.
            claimed |= {m for m in MEALS if _explicit_spans(clause, m)}
            continue
        claimed |= {m for m in _meals_named(clause) if not _out_excluded(clause, m)}
    generic = (not claimed and bool(_GENERIC_MEAL_RE.search(sentence)) and not _OUT_EXCL_RE.search(sentence)
               and not _GENERIC_NEG_RE.search(sentence))
    return claimed, generic


def _clock_times(text: str) -> set[str]:
    out = {re.sub(r"\s+", "", m.group(0).lower()) for m in _AMPM_RE.finditer(text or "")}
    out |= {f"{int(m.group(1))}:{m.group(2)}" for m in _H24_RE.finditer(text or "")}
    out |= {re.sub(r"[\s'’]+", "", m.group(0).lower()) for m in _WORD_TIME_RE.finditer(text or "")}
    return out


def _times_unsupported(sentence: str, scope: str) -> bool:
    return bool(_clock_times(sentence) - _clock_times(scope))


def day_coverage(day_text: Optional[str], inclusions: str, exclusions: str,
                 n_days: Optional[int] = None, day: Optional[int] = None) -> set[str]:
    """Meals the source covers for one output day (number ``day``). ``day_text`` None = the day could not be
    matched to one source day (only INCLUSIONS can cover then)."""
    inc_cover, inc_neg = inclusion_meals(inclusions, n_days, day)
    # A generic exclusion ("meals not mentioned in the itinerary", "drinks with meals") names no specific meal.
    exc_specific = set().union(*(_word_meals(c) for c in _clauses(exclusions))) if exclusions else set()
    blocked = set(inc_neg) | exc_specific
    if day_text is None:
        return inc_cover - blocked
    day_pos, day_hard, day_weak = _mentions(day_text)
    listed = structured_meals(day_text)
    provided = provided_meals(day_text)
    # A plain "no" / soft cue gives way to the day's own list or "provided" statement ("Enjoy dinner then an
    # evening at leisure. Meals Included: Breakfast, Dinner"); an outright exclusion never does.
    blocked |= day_hard | (day_weak - (listed or set()) - provided)
    if listed is not None:
        # The day's own list wins over INCLUSIONS and passing mentions ("after lunch, we..."), but not over the
        # same day saying the meal is provided ("A picnic lunch on the Wall is provided. ... Meals: Breakfast").
        return (listed | provided) - blocked
    return (day_pos | inc_cover) - blocked


def meal_claim_sourced(sentence: str, *, day_text: Optional[str], inclusions: str = "", exclusions: str = "",
                       source_all: str = "", n_days: Optional[int] = None,
                       day: Optional[int] = None) -> tuple[bool, str]:
    """(sourced, reason) for one output sentence that matched the meal-claim regex. A time in the sentence
    must be stated on the same source day or in INCLUSIONS (``source_all``, the full labelled source, when
    the day is unknown). ``n_days`` is the tour's length and ``day`` the output day (for INCLUSIONS counts)."""
    scope = source_all if day_text is None else f"{day_text}\n{inclusions}"
    if _times_unsupported(sentence, scope):
        return False, "clock_time_not_in_source"
    claimed, generic = claimed_meals(sentence)
    cover = day_coverage(day_text, inclusions, exclusions, n_days, day)
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


_DURATION_DAYS_RE = re.compile(r"(\d{1,3})\s*days?\b", re.IGNORECASE)


def tour_days(tour: dict, day_text_map: dict[int, str]) -> Optional[int]:
    """The tour's length in days: the larger of the source's last day and the "N Days" duration."""
    n = max(day_text_map) if day_text_map else 0
    m = _DURATION_DAYS_RE.search(str(tour.get("duration") or ""))
    if m:
        n = max(n, int(m.group(1)))
    return n or None


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
                              source_all=g.source_text(tour), n_days=tour_days(tour, day_text_map), day=day)
