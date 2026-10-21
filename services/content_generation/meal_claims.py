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
  a code legend ("B = Breakfast, L = Lunch, D = Dinner") add nothing. A scoped board ("All meals during
  the trek", "Full board on safari days") covers no day, and a daily cue belongs to its own meal only
  ("Daily breakfast and lunch on Day 3" covers breakfast);
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
  generic forms ("No meals included today", "Meals: None", also in brackets: "(No meal provided)") negate
  all three. More cues (review round 3): guest-paid ("at passenger's expense", "(own)", "(extra)", a price,
  "pay direct", "self-arranged"), opt-in / find-your-own before the meal ("opt for", "free to find", "tips",
  "choose your lunch"), conditions ("if time permits") and part of the group ("Those who wish", "Guests on
  the deluxe option") for the whole clause. A tour-wide count in a day's text ("7 breakfasts") is ignored.

Output side, a meal is dropped from the claim only on an explicit exclusion next to it in its own comma
segment ("lunch is not included", "optional dinner", "dinner at your own expense"). Any other "not" / "no",
a cue about something else ("(drinks not included)", "after an optional kayak, lunch is served"), keeps the
meal in the claim. A bare plural "meals" claims all three unless a list says which; "feast", "BBQ", "picnic",
"brunch" and "a meal" claim the meal their time of day says. A sentence about another day ("tomorrow",
"Day 3") is not sourced.

Never sourced: a sentence whose day cannot be matched to one source day unless INCLUSIONS alone covers it.
Unmatched: no "Day N" title; the source day split fell back on a multi-day tour; the output days are not the
source days one to one (a writer that split or merged a day); a grouped source block ("DAY 11 & 12") the
writer split; a source block whose text names another "Day N" (inline or quoted headers, "Extra Day 01").
A one-day source is its own day when the output is one day too. Also never sourced: a sentence that claims
no positive meal (left to the caller, fail-closed); and a sentence with a time the same source day (or
INCLUSIONS) does not state: am/pm, 24-hour, "noon", "7 o'clock". Meal TIMES are never inferred. A meal the
source has only on the neighbouring day is not sourced either.
"""
from __future__ import annotations

import re
from typing import Optional

MEALS = ("breakfast", "lunch", "dinner")
ALL = frozenset(MEALS)

_WORD_RES = {
    "breakfast": re.compile(r"\bbreakfasts?\b|\bbrunch(?:es)?\b", re.IGNORECASE),
    "lunch": re.compile(r"\blunch(?:es|eons?)?\b|\b(?:picnic|packed)\s+meals?\b|\bbrunch(?:es)?\b", re.IGNORECASE),
    "dinner": re.compile(r"\b(?:dinners?|suppers?|evening\s+meals?)\b", re.IGNORECASE),
}
_MEAL_WORD = r"(?:breakfasts?|brunch(?:es)?|lunch(?:es|eons?)?|dinners?|suppers?)"
_ANY_MEAL_WORD_RE = re.compile(r"\b" + _MEAL_WORD + r"\b", re.IGNORECASE)
_CODE_MEAL = {"B": "breakfast", "L": "lunch", "D": "dinner"}
# A code list: 1-3 of B/L/D joined by , / & + - | · • "and" or spaces ("B, L, D", "B D", "BLD", "B/L",
# "B, L and D", "B | L | D"). Case-sensitive.
_CODE_LIST = r"[BLD](?:\s*(?:[,/&+|\-·•]|and)?\s*[BLD]){0,2}"
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
# A price next to a meal means the traveller pays it ("(approx. USD 10 pp)", "lunch $8").
_CURRENCY = (r"(?:usd|us\$|eur|gbp|inr|rs|thb|vnd|jpy|rmb|cny|npr|lkr|idr|myr|sgd|aud|nzd|php|khr|lak|mmk|bdt|"
             r"yen|baht|rupees?|dollars?|euros?|dong|kip|riel|ringgit|rupiah|yuan)")
# What a negating bracket is rewritten to (see _normalise): a STRONG bracket (a hard, explicit or clause cue)
# negates its whole comma segment before it, up to BRACKET_WINDOW words ("Enjoy a delicious lunch at a premium
# 5-star hotel or a charming rooftop cafe with a view of the Taj Mahal (optional, depending on your package)");
# a WEAK one ("(no cycling)", "(not far)") is a hard cue within HARD_WINDOW, like before. Neither token is a
# plain "not", so it never reads as a weak cue on the meal AFTER the bracket.
_BRX = "xbracketnegx"
_BRX_WEAK = "xbracketweakx"
_HARD_NEG_RE = re.compile(
    r"\b" + _BRX + r"\b|\b" + _BRX_WEAK + r"\b"
    r"|\bnot\s+(?:be\s+)?(?:included|provided|inclusive|covered|part\s+of)\b|\bexclud\w*|\bexcl\b"
    r"|\bexclusive\s+of\b|\bown\s+(?:expense|cost|account)\b|\bself[\s-]+(?:pay|paid|funded|arranged|cater\w*)\b"
    r"|\bpayable\b|\bexpenses?\b|\bown\b(?=\s*(?:[)\].,;:!?]|$))|\bextra\b(?=\s*(?:[)\].,;:!?]|$))|\bat\s+extra\b"
    r"|\bfor\s+purchase\b|\b(?:extra|additional)\s+(?:cost|charge|fee|payment)s?\b|\bsurcharge\b|\bat\s+additional\b"
    r"|\bpay\s+(?:for|locally|direct(?:ly)?|separately|on\s+the\s+spot|yourself|extra)\b"
    r"|\bpaid\s+(?:locally|direct(?:ly)?|separately|on\s+the\s+spot|by\s+(?:you|yourself|guests?|clients?|"
    r"travell?ers?|participants?|passengers?))\b"
    r"|[$€£¥₹]\s?\d|\b" + _CURRENCY + r"\.?\s?\d|\d[\d,.]*\s?" + _CURRENCY + r"\b",
    re.IGNORECASE)
# EXPLICIT: the meal is the traveller's to arrange or pay ("optional dinner", "lunch on own", "dinner can be
# arranged on request", "your guide can recommend a restaurant for dinner", "if time permits"). Within
# NEG_WINDOW words either side.
_EXPLICIT_CUE_RE = re.compile(
    r"\boptional(?:ly)?\b|\bunless\b|\bown\s+(?:choice|arrangement)s?\b|\bon\s+(?:your|their|our|one['’]s)?\s*own\b"
    r"|\bpurchas\w*|\bbuy\b|\bsupplement\b"
    r"|\bcan\s+be\s+(?:arranged|booked|organi[sz]ed|purchased|bought|added|requested)\b"
    r"|\b(?:on|upon|by)\s+request\b|\bupgrade\w*|\brecommend\w*|\bsuggest\w*"
    r"|\bif\s+(?:time|you|desired|preferred|wished|requested|interested|required|needed)\b"
    r"|\b(?:time|weather)\s+permitting\b|\bshould\s+you\b",
    re.IGNORECASE)
# PRE: the traveller picks or finds the meal ("opt for a traditional dinner", "free to find a local dinner",
# "tips on great places to grab a local dinner", "Choose your lunch at various shops"). Only BEFORE the meal,
# within NEG_WINDOW words ("After breakfast, choose a hike or a rest" keeps the breakfast).
_PRE_CUE_RE = re.compile(
    r"\bopt(?:s|ing|ed)?\s+(?:for|to|in|into)\b|\bfree\s+to\b|\btips?\b|\bgrab\b|\bfind\b|\bchoose\b",
    re.IGNORECASE)
# CLAUSE: the meal is for part of the group or depends on something ("Those who wish can join the guide for
# lunch", "Guests on the deluxe option have lunch at the palace", "depending on your package"). Negates
# every meal of its clause.
_CLAUSE_CUE_RE = re.compile(
    r"\bthose\s+who\b|\bfor\s+those\b|\banyone\s+who\b|\bwhoever\b"
    r"|\b(?:guests?|clients?|travell?ers?|participants?|passengers?)\s+(?:on|who|taking|choosing|opting|booked|"
    r"booking|staying|travelling|traveling|in)\b"
    r"|\bdepend\w*\s+on\b|\bsubject\s+to\b"
    r"|\b(?:on|with|in|choosing|taking|booking)\s+the\s+\w+\s+(?:option|package|upgrade)\b"
    r"|\b(?:deluxe|premium|superior|luxury|upgraded?|vip)\s+(?:option|package|category|version)\b",
    re.IGNORECASE)
# A strong bracket ("(optional, depending on your package)", "(own account)") reaches the whole comma segment
# before it; a bracket with only a weak cue ("(no cycling)") stays a windowed hard cue.
_BRACKET_STRONG_RE = re.compile("|".join(rx.pattern for rx in (_HARD_NEG_RE, _EXPLICIT_CUE_RE, _CLAUSE_CUE_RE)),
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
    r"|\btomorrow\b|\bnext\s+(?:day|morning|evening)\b",
    re.IGNORECASE)
# Any cue at all (used where a clause names meals only through "all meals" / "full board").
_ANY_CUE_RE = re.compile("|".join(rx.pattern for rx in (_HARD_NEG_RE, _EXPLICIT_CUE_RE, _PRE_CUE_RE,
                                                        _CLAUSE_CUE_RE, _WEAK_CUE_RE, _SOFT_CUE_RE)),
                         re.IGNORECASE)
# A bracket holding one of these, and no meal, negates the segment before it: "lunch (own account)",
# "lunch at a local restaurant (optional, not included)". A bracket with only a soft cue is read inline.
_BRACKET_NEG_RE = re.compile("|".join(rx.pattern for rx in (_HARD_NEG_RE, _EXPLICIT_CUE_RE, _CLAUSE_CUE_RE,
                                                            _WEAK_CUE_RE)), re.IGNORECASE)
# Output side: only an explicit exclusion next to the meal drops it from the claim.
_OUT_EXCL_RE = re.compile(
    _HARD_NEG_RE.pattern + r"|\boptional\b|\bon\s+(?:your|their|one['’]s)\s+own\b|\brecommend\w*|\bsuggest\w*"
    r"|\bno\s+(?:\w+\s+)?" + _MEAL_WORD + r"\b|\bwithout\s+(?:a\s+)?" + _MEAL_WORD + r"\b",
    re.IGNORECASE)
# Clause boundaries for negation scope. Commas do NOT split: "Breakfast, lunch and dinner are not included"
# negates all three (fail-closed when a comma list mixes included and excluded meals). "|" and bullets do not
# split either: "Meals included: Breakfast | Lunch | Dinner" is one list. Brackets are handled by _normalise.
_CLAUSE_SPLIT_RE = re.compile(r"[.;!?\n]+|\b(?:but|however|whereas|while)\b", re.IGNORECASE)
# A comma segment that is only a predicate ("is not included") belongs to the subject before it.
_BARE_PREDICATE_RE = re.compile(r"\s*(?:which\s+|that\s+)?(?:is|are|was|were|will\s+be|be|remains?)\b",
                                re.IGNORECASE)
# ... and so does a comma segment that opens with a cue: ", not included", ", at own expense", ", optional in
# your tour package", ", depending on your package", ", on request".
_LEADING_CUE_RE = re.compile(
    r"\s*(?:(?:and|or|which|that|is|are|will|be|was|were|also|at|the|your|their|a|an|all|of)\s+){0,4}(?:"
    + _HARD_NEG_RE.pattern + r"|\boptional(?:ly)?\b(?=\s*(?:$|in\b|on\b|at\b|for\b|depend|subject|upon|"
    + _BRX + r"))|" + _CLAUSE_CUE_RE.pattern + r"|\b(?:on|upon|by)\s+request\b)",
    re.IGNORECASE)
# ... and a short last segment that is only a condition (", if time permits.", ", time permitting", ", if
# desired") qualifies what comes before it. One that goes on ("and if you have enough free time, visit a
# ceramics factory") is about the next activity.
_TRAILING_CONDITION_RE = re.compile(
    r"\s*(?:and\s+|or\s+)?(?:only\s+)?(?:if\s+(?:time|desired|required|requested|preferred|wished|you\s+wish|"
    r"you\s+like|you\s+prefer|available|possible)\b|(?:time|weather)\s+permitting\b)(?:\s+\w+){0,2}\s*$",
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
    r"|\bthroughout\s+(?:the\s+)?(?:tour|trip|stay|journey|holiday)\b",
    re.IGNORECASE)
_COUNT_RE = re.compile(
    r"(?<![Dd]ay\s)(?<![Dd]ay)\b(\d{1,2}|" + _NUM_WORD_RE + r")\s*(?:x\s*)?(?:[A-Za-z\-]+\s+)?("
    + _MEAL_WORD + r")\b", re.IGNORECASE)
# A tour-wide count in a day's text ("Meals included: 7 breakfasts, 3 lunches, 2 dinners" after the last day) is
# not that day's meal: removed from day text before it is read. Plural meal words only ("a three course dinner"
# stays).
_DAY_COUNT_RE = re.compile(
    r"\b(?:\d{1,2}|" + _NUM_WORD_RE + r")\s*(?:x\s*)?(?:[A-Za-z\-]+\s+)?"
    r"(?:breakfasts|brunches|lunch(?:es|eons)|dinners|suppers)\b", re.IGNORECASE)
# INCLUSIONS: a meal (or the daily cue) followed by one of these is tied to some days, not every day
# ("Daily breakfast and lunch on Day 3", "Breakfast daily, dinner on Days 3-5").
_DAY_QUALIFIER_RE = re.compile(
    r"\bon\s+(?:the\s+)?(?:days?|nights?|first|last|final|second|third|arrival|departure|\d)"
    r"|\b(?:days?|nights?)\s*\d|\b(?:first|last|final|arrival|departure|welcome|farewell)\s+(?:days?|nights?|evening)\b"
    r"|\bexcept\b|\bonly\b|\bwhile\b|\bwhilst\b|\bduring\b|\bfrom\s+day\b|\buntil\b|\btill\b",
    re.IGNORECASE)
# INCLUSIONS: "full board" / "all meals" covers every day only when nothing scopes it ("All meals during the
# trek", "Full board on safari days", "All Meals while on trek from day 1st dinner to day 8th breakfast").
_SCOPE_AFTER_RE = re.compile(
    r"\b(?:on|during|while|whilst|in|at|from|until|till|to|for|when|except|only|between|after|before|"
    r"throughout|days?|nights?)\b", re.IGNORECASE)
_SCOPE_BEFORE_RE = re.compile(r"\b(?:days?|nights?|during|while|whilst|when|except|only)\b", re.IGNORECASE)
_WHOLE_TOUR_RE = re.compile(r"\bthroughout\s+(?:the\s+)?(?:tour|trip|stay|journey|holiday)\b|\bdaily\b"
                            r"|\b(?:every|each)\s+day\b|\bbasis\b", re.IGNORECASE)
# Output side: a sentence that speaks of another day ("breakfast is provided again tomorrow", "breakfast is
# included today and tomorrow", "on arrival the next morning") is not judged against the current day.
_OTHER_DAY_RE = re.compile(
    r"\btomorrow\b|\b(?:next|following)\s+(?:day|morning|evening|night|afternoon)\b|\bthe\s+day\s+after\b"
    r"|\bthe\s+(?:day|night|evening)\s+before\b|\byesterday\b|\bprevious\s+(?:day|evening|night)\b",
    re.IGNORECASE)
_NUM_WORD_DAY = "|".join(sorted(_NUM_WORDS + ("twenty-one", "twenty-two", "twenty-three", "twenty-four",
                                               "twenty-five", "twenty-six", "twenty-seven", "twenty-eight",
                                               "twenty-nine", "thirty", "first", "second", "third", "fourth",
                                               "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"),
                                key=len, reverse=True))
_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth")
# A "Day N" written inside a source day's text or an output sentence ("Day 3: Boat on Phewa Lake", '"Day 5 /
# -Stage 4-', "Extra Day 01", "On day two, take a guided safari"). In a source day block it means the block
# holds another day's text; in an output sentence, a meal on another day.
_DAY_REF_RE = re.compile(r"\bdays?\s*#?\s*(\d{1,3}|" + _NUM_WORD_DAY + r")(?:st|nd|rd|th)?\b", re.IGNORECASE)
# A source marker line that groups days under one header: "DAY 11 & 12 - Ella", "Day 3, 4:", "Days 5/6", "Day 7
# and 8". The block is filed under its first number only.
_GROUPED_TAIL_RE = re.compile(r"^\s*(?:&|,|\+|/|and|,\s*and)\s*(?:days?\s*)?(\d{1,3})\b(?!\s*(?:hours?|hrs?|km|kms|"
                              r"kilomet|miles?|mins?|minutes?|nights?|days?|m\b|ft\b|metres?|meters?|people|pax))",
                              re.IGNORECASE)
# Output side: meal nouns that name no meal by themselves. A time of day in the clause says which meal; without
# one, a feast / barbecue / banquet claims lunch and dinner, a bare picnic lunch.
_OUT_FEAST_RE = re.compile(r"\b(?:feasts?|barbecues?|barbeques?|bbqs?|banquets?|cook-?outs?|hot\s?pots?)\b",
                           re.IGNORECASE)
_OUT_PICNIC_RE = re.compile(r"\bpicnics?\b", re.IGNORECASE)
_OUT_MEAL_RE = re.compile(r"\bmeal\b", re.IGNORECASE)          # "Mid-day, a meal is served" = lunch
_TIME_OF_DAY = (
    ("breakfast", re.compile(r"\b(?:morning|dawn|sunrise)\b", re.IGNORECASE)),
    ("lunch", re.compile(r"\b(?:midday|mid-day|noon|lunchtime|afternoon)\b", re.IGNORECASE)),
    ("dinner", re.compile(r"\b(?:evening|night|tonight|sunset|dusk|nightfall)\b", re.IGNORECASE)),
)
# Output side: a bare plural "meals" ("Meals are provided", "meals on board are prepared by the onboard chef")
# claims all three, unless a list right after it says which ("meals (breakfast and dinner) are provided",
# "Meals: B, L") or it refers to the day's list ("Meals included: as listed for the day").
_BARE_MEALS_RE = re.compile(r"\bmeals\b", re.IGNORECASE)
_MEALS_SPECIFIED_RE = re.compile(
    r"\s*(?:(?:are\s+|is\s+)?(?:included|provided|served)\s*)?(?:[(\[:\-\u2013\u2014]|,?\s*(?:including|incl\.?|"
    r"namely|such\s+as|i\.e\.?|of|with))\s*(?:the\s+|a\s+|an\s+)?(?:" + _MEAL_WORD + r"\b|evening\b|picnic\b"
    r"|packed\b|[BLD](?![A-Za-z])|none\b|nil\b|no\b|not\b)", re.IGNORECASE)
_MEALS_REFERENCE_RE = re.compile(
    r"\b(?:as|where|when)\s+(?:listed|noted|indicated|shown|specified|stated|mentioned|per|above|below)\b"
    r"|\blisted\b|\bsee\b", re.IGNORECASE)
_NOT_BARE_MEALS_RE = re.compile(r"\b(?:picnic|packed|evening|all|three|3|daily|board)\s+meals\b"
                                r"|\bmeals\s+daily\b", re.IGNORECASE)

_BRACKET_CODES_RE = re.compile(r"[(\[]\s*(" + _CODE_LIST + r")\s*[)\]]")
_BRACKET_RE = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
_TOKEN_RE = re.compile(r"[A-Za-z']+|[^\sA-Za-z']")
NEG_WINDOW = 6               # source side: a negation cue negates a meal only within this many words of it
SOFT_AFTER = 1               # ... a soft cue only this close when it follows the meal
HARD_WINDOW = 12             # a hard exclusion reaches this far inside its comma segment
OUT_WINDOW = 3               # output side: an explicit exclusion drops a meal only this close to it
BRACKET_WINDOW = 25          # a strong negating bracket reaches this far back inside its comma segment


def _word_meals(text: str) -> set[str]:
    return {m for m, rx in _WORD_RES.items() if rx.search(text)}


def _normalise(text: str, bracket_rx: re.Pattern = _BRACKET_NEG_RE, out: bool = False) -> str:
    """One pass before clauses are read: "no extra charge" is positive, a code legend and "meals as per the
    itinerary" are removed, a bracketed code list "(L, D)" becomes its own "meals: L, D" clause, a bracket
    that names a meal or says "no meals" becomes its own clause ("(Lunch is not included)", "(Overnight
    stay・No meal provided)"), a bracket with a ``bracket_rx`` cue and no meal becomes a hard "not included"
    that negates the whole comma segment before it ("lunch (own account)", and "[No cycling] After breakfast"
    leaves the breakfast alone), and any other bracket is read inline. ``out`` (output side): every bracket is
    its own clause, so "(drinks not included)" never drops the meal before it."""
    t = _FREE_RE.sub(" included ", text or "")
    if len(_LEGEND_PAIR_RE.findall(t)) >= 2:
        t = _LEGEND_PAIR_RE.sub(" ", t)
    t = _BRACKET_CODES_RE.sub(lambda m: f" ; meals: {m.group(1)} ; ", t)

    def bracket(m: re.Match) -> str:
        inner = m.group(1)
        if out or _word_meals(inner) or _GENERIC_NEG_RE.search(inner):
            return f" ; {inner} ; "
        if bracket_rx.search(inner):
            return f" {_BRX if _BRACKET_STRONG_RE.search(inner) else _BRX_WEAK} , "
        return f" {inner} "

    for _ in range(3):                      # nested brackets, innermost first
        nxt = _BRACKET_RE.sub(bracket, t)
        if nxt == t:
            break
        t = nxt
    return _AS_PER_ITINERARY_RE.sub(" ", t)


def _clauses(text: str, bracket_rx: re.Pattern = _BRACKET_NEG_RE, out: bool = False) -> list[str]:
    return [c for c in _CLAUSE_SPLIT_RE.split(_normalise(text, bracket_rx, out)) if c and c.strip()]


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


def _near_seg(clause: str, spans: list[tuple[int, int]], cue_rx: re.Pattern, window: int) -> bool:
    """Output side: ``_near`` limited to the meal's own comma segment ("after an optional kayak, lunch is
    served" keeps the lunch)."""
    for seg in re.finditer(r"[^,]+", clause):
        inside = [(s - seg.start(), e - seg.start()) for s, e in spans if seg.start() <= s and e <= seg.end()]
        if inside and _near(seg.group(0), inside, cue_rx, window):
            return True
    return False


def _hard_negated(clause: str, spans: list[tuple[int, int]], hard_rx: re.Pattern = _HARD_NEG_RE) -> bool:
    """A hard exclusion in the meal's comma segment within HARD_WINDOW words; a negating bracket anywhere
    after the meal in its comma segment; or a segment after it, with no meal of its own, that is a bare
    predicate with a hard cue ("Dinner at the hotel restaurant, which is famous for its wine, is not
    included") or opens with a cue (", optional in your tour package", ", at own expense")."""
    for seg in re.finditer(r"[^,]+", clause):
        body = seg.group(0)
        if _BRX in body:
            # ... but not back over a structured list ("Meals included: breakfast | dinner ... (not ...)").
            mark = seg.start() + body.index(_BRX)
            listed = [m.end() for m in _STRUCTURED_RE.finditer(clause, seg.start(), mark)]
            floor = max(listed) if listed else seg.start()
            if any(floor <= s and e <= mark and _gap(clause, e, mark) <= BRACKET_WINDOW for s, e in spans):
                return True
        cues = list(hard_rx.finditer(body))
        trailing = not _word_meals(body) and (
            (cues and _BARE_PREDICATE_RE.match(body)) or _LEADING_CUE_RE.match(body)
            or (seg.end() == len(clause) and _TRAILING_CONDITION_RE.match(body)))
        if trailing:
            if any(e <= seg.start() for _s, e in spans):
                return True
            continue
        if not cues:
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
    if _CLAUSE_CUE_RE.search(clause):
        return "hard"                       # "Those who wish can join the guide for lunch"
    if not spans:
        if _HARD_NEG_RE.search(clause) or _EXPLICIT_CUE_RE.search(clause) or _PRE_CUE_RE.search(clause):
            return "hard"
        return "weak" if _WEAK_CUE_RE.search(clause) or _SOFT_CUE_RE.search(clause) else None
    if (_hard_negated(clause, spans) or _near(clause, spans, _EXPLICIT_CUE_RE, NEG_WINDOW, after=-1)
            or _near_seg(clause, spans, _EXPLICIT_CUE_RE, NEG_WINDOW)
            or _near(clause, spans, _PRE_CUE_RE, NEG_WINDOW, after=-1)):
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


def _daily_covers(clause: str, meal: str) -> bool:
    """INCLUSIONS: a daily cue belongs to ``meal`` ("Hotel with daily breakfast", "Breakfast, lunch and dinner
    daily"), not to a neighbour ("Daily breakfast and lunch on Day 3" covers breakfast only, "Breakfast daily,
    dinner on Days 3-5" breakfast only). Between the cue and the meal there is either nothing but other meals
    of the same list, or no other meal and no comma; and no day qualifier follows the meal or the cue."""
    spans = _explicit_spans(clause, meal)
    others = [sp for m in MEALS if m != meal for sp in _explicit_spans(clause, m)]
    for cue in _DAILY_RE.finditer(clause):
        cue_tail = re.split(r"[,;]", clause[cue.end():], maxsplit=1)[0]
        if _DAY_QUALIFIER_RE.match(cue_tail.strip()) or _DAY_QUALIFIER_RE.search(cue_tail[:20]):
            continue
        for s, e in spans:
            if cue.end() <= s:
                a, b = cue.end(), s
            elif cue.start() >= e:
                a, b = e, cue.start()
            else:
                return True
            between = clause[a:b]
            if _gap(clause, a, b) > NEG_WINDOW or _DAY_QUALIFIER_RE.search(between):
                continue
            if any(a <= os and oe <= b for os, oe in others):
                rest = _ANY_MEAL_WORD_RE.sub(" ", between)
                if re.sub(r"\b(?:and|or|the|a)\b|[\s,&/+]", "", rest, flags=re.IGNORECASE):
                    continue                # not a plain list of meals
            elif re.search(r"[,;]", between):
                continue
            tail = re.split(r"[,;]", clause[e:], maxsplit=1)[0]
            nxt = [os - e for os, _oe in others if os >= e]
            if nxt:
                tail = tail[:min(nxt)]
            if _DAY_QUALIFIER_RE.search(tail):
                continue
            return True
    return False


def _unscoped_board(inclusions: str) -> set[str]:
    """INCLUSIONS: "full board" / "half board" / "all meals" that nothing scopes ("Stay with all meals, bonfire",
    "Half board accommodation", "All meals throughout the tour"). Scoped forms cover no day by themselves:
    "All meals during the trek", "Full board on safari days", "All Meals while on trek (Veg) from day 1st dinner
    to day 8th breakfast", "Trek days on full board". A cue in the same clause ("all meals except drinks")
    blocks it too."""
    norm = _normalise(inclusions)
    out: set[str] = set()
    bounds = [0] + [m.end() for m in _CLAUSE_SPLIT_RE.finditer(norm)] + [len(norm) + 1]
    for rx, meals in ((_FULL_BOARD_RE, ALL), (_HALF_BOARD_RE, {"breakfast", "dinner"})):
        for m in rx.finditer(norm):
            seg_a = max(norm.rfind(c, 0, m.start()) for c in ",;.!?\n") + 1
            ends = [i for i in (norm.find(c, m.end()) for c in ",;.!?\n") if i >= 0]
            seg_b = min(ends) if ends else len(norm)
            head, tail = norm[seg_a:m.start()], _WHOLE_TOUR_RE.sub(" ", norm[m.end():seg_b])
            if _SCOPE_AFTER_RE.search(tail) or _SCOPE_BEFORE_RE.search(head):
                continue
            ca = max(b for b in bounds if b <= m.start())
            cb = min(b for b in bounds if b > m.start())
            if _ANY_CUE_RE.search(norm[ca:cb]):
                continue
            out |= meals
    return out


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
        for meal in explicit - negated:
            if n_days == 1 or _daily_covers(clause, meal):
                cover.add(meal)
        if n_days:
            for m in _COUNT_RE.finditer(clause):
                meal = next((k for k, rx in _WORD_RES.items() if rx.fullmatch(m.group(2))), None)
                if meal and meal not in negated and _count_covers(meal, _count_of(m.group(1)), n_days, day):
                    cover.add(meal)
    cover |= _unscoped_board(inclusions)
    return cover - neg, neg


def _out_excluded(clause: str, meal: str) -> bool:
    """Output side: an explicit exclusion next to the meal ("lunch is not included", "optional dinner")."""
    spans = _explicit_spans(clause, meal)
    if not spans:
        return bool(_OUT_EXCL_RE.search(clause))
    return _near_seg(clause, spans, _OUT_EXCL_RE, OUT_WINDOW)


def _noun_meals(clause: str) -> set[str]:
    """Output side: meals claimed through a noun that names none ("a seafood feast awaits on board in the
    evening" = dinner, "a picnic is served at midday" = lunch, "a beach BBQ" = lunch and dinner, "Mid-day, a
    meal is served" = lunch; a bare "a meal" with no time of day stays a generic claim). Skipped when
    a meal word sits next to it ("BBQ dinner", "picnic breakfast") or it is excluded ("optional BBQ")."""
    out: set[str] = set()
    meal_spans = [sp for m in MEALS for sp in _explicit_spans(clause, m)]
    for rx, default in ((_OUT_FEAST_RE, {"lunch", "dinner"}), (_OUT_PICNIC_RE, {"lunch"}), (_OUT_MEAL_RE, set())):
        for m in rx.finditer(clause):
            if any(_gap(clause, e, m.start()) <= 2 if e <= m.start() else
                   _gap(clause, m.end(), s) <= 2 if s >= m.end() else True for s, e in meal_spans):
                continue
            if _near_seg(clause, [m.span()], _OUT_EXCL_RE, OUT_WINDOW):
                continue
            timed = {meal for meal, trx in _TIME_OF_DAY if trx.search(clause)}
            out |= timed or default
    return out


def _bare_meals_claim(sentence: str) -> bool:
    """Output side: a bare plural "meals" that claims all three ("Meals are provided", "Breakfast is included,
    and meals on board the junk are prepared by the onboard chef"). Not when a list right after it says which
    ("meals (breakfast and dinner) are provided", "Meals: B, L"), it refers to the day's list ("as listed"),
    it is negated or excluded ("No meals", "meals at your own expense"), or it is "picnic / evening / all meals"
    (read as named meals elsewhere)."""
    for clause in _CLAUSE_SPLIT_RE.split(_AS_PER_ITINERARY_RE.sub(" ", sentence or "")):
        if not clause or _GENERIC_NEG_RE.search(clause) or _MEALS_REFERENCE_RE.search(clause):
            continue
        skip = [m.span() for m in _NOT_BARE_MEALS_RE.finditer(clause)]
        for m in _BARE_MEALS_RE.finditer(clause):
            if any(a <= m.start() and m.end() <= b for a, b in skip):
                continue
            if _MEALS_SPECIFIED_RE.match(clause, m.end()):
                continue
            if _near_seg(clause, [m.span()], _OUT_EXCL_RE, OUT_WINDOW):
                continue
            return True
    return False


def claimed_meals(sentence: str) -> tuple[set[str], bool]:
    """(meals the output sentence claims as provided, generic_claim). A meal is dropped only on an explicit
    exclusion next to it ("Breakfast is included; lunch is at your own expense" claims breakfast only); any
    other "not" / "no" keeps it ("dinner is served at a restaurant not far from the river" claims dinner).
    ``generic_claim`` is True when the sentence states meal logistics without naming a meal ("Meals
    included:")."""
    claimed: set[str] = set()
    for clause in _clauses(sentence, _OUT_EXCL_RE, out=True):
        if _GENERIC_NEG_RE.search(clause):
            # "No meals are included except breakfast": a meal named next to a generic "no meals" is claimed.
            claimed |= {m for m in MEALS if _explicit_spans(clause, m)}
            continue
        claimed |= {m for m in _meals_named(clause) if not _out_excluded(clause, m)}
        claimed |= _noun_meals(clause)
    if _bare_meals_claim(sentence):
        claimed |= ALL
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
    # A tour-wide count in the day's text ("Meals included: 7 breakfasts, 3 lunches" after the last day) is not
    # that day's meal.
    day_text = _DAY_COUNT_RE.sub(" ", day_text)
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
    if _OTHER_DAY_RE.search(sentence) or (day is not None and _day_refs(sentence) - {day}):
        return False, "other_day_reference"    # "breakfast is provided again tomorrow": not this day's meal
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


def _day_value(token: str) -> int:
    from services.content_generation.day_parse import _day_num

    t = token.lower()
    return _ORDINALS.index(t) + 1 if t in _ORDINALS else _day_num(t)


def _day_refs(text: str) -> set[int]:
    """The day numbers a text writes as "Day N" ("Day 3:", '"Day 5 /', "Extra Day 01", "on day two")."""
    return {_day_value(m.group(1)) for m in _DAY_REF_RE.finditer(text or "")} - {0}


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


def _grouped_blocks(src_itin: str) -> dict[int, set[int]]:
    """{first day: every day} of each source block that holds more than one day: a range ("Days 1-3") or a
    grouped header ("DAY 11 & 12 - Ella", "Day 8, 9 and 10 in Passikudah"). Such a block is filed under its
    first number only, so its meals may belong to any of its days."""
    from services.content_generation.day_parse import marker_match, source_day_markers

    lines = (src_itin or "").splitlines()
    out: dict[int, set[int]] = {}
    for first, last, idx in source_day_markers(src_itin):
        stripped = lines[idx].strip()
        m = marker_match(stripped)
        days = set(range(first, last + 1))
        tail = stripped[m.end():] if m else ""
        while True:
            g = _GROUPED_TAIL_RE.match(tail)
            if not g or not max(days) < int(g.group(1)) <= first + 14:
                break               # "Day 6, 2,500m climb" is not days 6 and 2
            days.add(int(g.group(1)))
            tail = tail[g.end():]
        if len(days) > 1:
            out[first] = days
    return out


def _source_day_text(day_text_map: dict[int, str], src_itin: str, day: int,
                     out_days: Optional[set[int]]) -> Optional[str]:
    """The source text of output day ``day``, or None when the match cannot be trusted (see
    ``unit_meal_sourced``)."""
    keys = set(day_text_map)
    blocks = _grouped_blocks(src_itin)
    in_block = set().union(*blocks.values()) if blocks else set()
    if out_days is None:
        # Day numbers of the output unknown: nothing on or after a grouped block.
        if blocks and day >= min(blocks):
            return None
    elif set(out_days) == keys:
        pass                        # one output day per source block, grouped blocks included
    elif blocks and set(out_days) == keys | in_block:
        if day in in_block:
            return None             # the writer split a grouped block: which of its days has the meal?
    else:
        return None                 # output and source days do not line up one to one
    text = day_text_map.get(day)
    if text is None or _day_refs(text) - (blocks.get(day) or {day}):
        return None
    return text


def unit_meal_sourced(unit: dict, tour: dict, days: tuple[dict[int, str], bool],
                      out_days: Optional[set[int]] = None) -> tuple[bool, str]:
    """``meal_claim_sourced`` for a grounding unit ({field, sentence, day}) of ``tour``. ``days`` is
    ``grounding._source_days(tour)`` (computed once per tour by the caller); ``out_days`` the day numbers of
    the output itinerary (``output_day_numbers``).

    The sentence's day is matched to a source day only when that match can be trusted; otherwise only
    INCLUSIONS can cover it (fail-closed):
    * the output days are exactly the source days (a writer that split or merged a day shifts every later
      day: output Day 5 would be checked against source Day 5, which is another day). A grouped source block
      ("DAY 11 & 12 - Ella", "Days 1-3") counts once when the writer kept it as one day; when the writer gave
      each of its days its own output day, the other days still match and the block's days do not;
    * the source block's text names no other "Day N" (an inline "Day 3:", a quoted '"Day 5 /' header the
      marker regex does not read, an "Extra Day 01" extension, "on day two" of a two-night block);
    * a one-day source is its own day only when the output is one day too (an "OVERNIGHT TRIP" with no
      markers is two days)."""
    from services.content_generation import grounding as g
    from services.content_generation.day_parse import source_day_markers

    src_itin = g._as_text(tour.get("itineraries"))
    day_text_map, used_fallback = days
    day = unit.get("day")
    out_max = max(out_days) if out_days else 0
    n_days = max(tour_days(tour, day_text_map) or 0, out_max) or None
    if (len(day_text_map) == 1 and all(last <= 1 for _f, last, _i in source_day_markers(src_itin))
            and out_max <= 1):
        # A one-day source (a day tour: no "Day N" marker past Day 1) and a one-day output: its whole text is
        # that day, whatever the output calls it.
        day_text: Optional[str] = next(iter(day_text_map.values()))
    elif used_fallback or day is None:
        day_text = None           # never guess which source day a sentence belongs to
    else:
        day_text = _source_day_text(day_text_map, src_itin, day, out_days)
    return meal_claim_sourced(unit["sentence"], day_text=day_text,
                              inclusions=g._as_text(tour.get("inclusions")),
                              exclusions=g._as_text(tour.get("exclusions")),
                              source_all=g.source_text(tour), n_days=n_days, day=day)


def output_day_numbers(units: list[dict]) -> set[int]:
    """The day numbers (> 0) of an output itinerary, from ``itinerary_sentences`` units. A "Day 0" the writer
    kept from the source is not a source day either (the marker regex skips day 0)."""
    return {u["day"] for u in units if u.get("day")}
