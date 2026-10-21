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

Review round 4 (fail-closed): a meal the guest brings ("bring a picnic lunch with you"), is told they might like
("You might like to stop for lunch at ..."), or that is one of two ("lunch or dinner with the family") is not
provided; a meal of another day inside this day's text ("the following morning ... for breakfast", "last night's
dinner", "on the last evening of the tour") is not this day's; "Lunch, dinner and drinks ... are not included"
negates the whole list; EXCLUSIONS "Meals other than breakfast" block lunch and dinner. INCLUSIONS: a count must
be a count ("10-course dinner" is not ten dinners), and a daily cue or count tied to a place or some days ("in
Kathmandu", "on the cruise", "Day 1-2: ...", ", starting on Day 2") covers no day. Source days: a repeated or
backward "Day N" marker (body line, restarted extension, alternative day) leaves that day unmatched, and "Day 4 -
15 Oct" is not a range. Output: "7 p.m." and "six in the morning" are times (all compared as 24-hour), a meal
legend is a claim, a bracket annotation does not cut a code list, only a title line sets the day, and a meal
noun beside a meal word is still claimed; an untimed "a meal" claims lunch and dinner unless it restates a meal
the sentence names.

Review round 5 (fail-closed): source side, "however" / "while" and an abbreviation's period ("approx.", "Mt.") do not
cut a meal from its cue; "Lunch: N/A", "Lunch: -", "Meals: B, L (own expense), D", "neither ... nor", "for your
account" and "tự túc" / "libre" negate; "all meals" / "meals daily" in a day's text count only when the clause says
they are included or provided; a board or count term is only the meals it lists ("half board (breakfast and lunch)",
"2 meals daily (breakfast and dinner)", "2 meals daily" alone is none); "on the 2nd evening" / "night 2" is another
day. Day blocks: a run of the same "Day N" whose first line is a body sentence, whose headers carry a version or
season label, or that has two different titled headers is not one day, on a one-day tour too. Output side:
"every / each meal" is all three, "at lunchtime" lunch, a picnic / packed meal the meal of its time of day, a meal
word used as a time marker ("After breakfast, ... a meal is served") hides no untimed meal, "Meals including lunch"
and "where you can see" are a bare "meals" (all three), a placeholder does not cut a code list ("B/-/D"), bare and
military hours ("at 7", "0630 hrs", "19h") are times, and a sentence about more than its day ("every morning of the
tour", "for the rest of the trek", "two days later") is sourced only by INCLUSIONS that cover every day.
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
# "B, L and D", "B | L | D"). Case-sensitive. Review round 5: a placeholder for a meal not given ("B/-/D", "B/x/D",
# "-/L/D", "B/L/-") does not end the list.
_CODE_PH = r"[\-\u2013\u2014x\u00d7]"
_CODE_LIST = (r"(?:" + _CODE_PH + r"\s*[,/|]\s*){0,2}[BLD](?:\s*(?:[,/&+|\-·•]|and)?\s*(?:" + _CODE_PH
              + r"\s*[,/|]\s*)?[BLD]){0,2}(?:\s*[,/|]\s*" + _CODE_PH + r"(?![A-Za-z]))?")
# Codes count only in a meal context, never as a bare capital letter in prose.
_CODE_RES = (
    re.compile(r"(?i:\bmeals?)(?:\s+(?i:included|provided))?\s*[:\-\u2013]?\s*(" + _CODE_LIST + r")(?![A-Za-z])"),
    re.compile(r"[(\[]\s*(" + _CODE_LIST + r")\s*[)\]]"),
    re.compile(r"(?<![A-Za-z])([BLD](?:\s*[/\-]\s*(?:" + _CODE_PH + r"\s*/\s*)?[BLD]){1,2})(?![A-Za-z])"),
)
# A structured per-day meal list: a "Meals" label followed by codes or meal words (a bracketed code list
# "(L, D)" is rewritten to "meals: L, D" by _normalise first).
_STRUCTURED_RE = re.compile(
    r"(?i:\bmeals?)(?:\s+(?i:included|provided))?\s*[:\-\u2013]?\s*[\u2022\u00b7*]?\s*(?:"
    + _CODE_LIST + r"(?![A-Za-z])"
    r"|(?i:breakfast|lunch|dinner))")
# The rest of a structured list after its first item ("| dinner", ", L, D", " and dinner").
_LIST_TAIL_RE = re.compile(r"(?:\s*(?:[|,/&+\u2022\u00b7]|and)\s*(?:(?i:breakfast|lunch|dinner)\b|[BLD](?![A-Za-z])))*")
_FULL_BOARD_RE = re.compile(
    r"\bfull[\s-]+board\b|\b(?:all|three|3)\s+(?:daily\s+)?meals\b|\bdaily\s+meals\b|\bmeals\s+daily\b",
    re.IGNORECASE)
_HALF_BOARD_RE = re.compile(r"\bhalf[\s-]+board\b", re.IGNORECASE)
# "All meals specified in the itinerary", "Meals included as per itinerary", "Meals included where indicated"
# name no meal by themselves: the meals are the ones the itinerary names. Removed before meals are read.
_ITIN_WORD = r"(?:itinerar(?:y|ies)|program(?:me)?s?|schedule)"
_SAID_VERB = r"(?:specified|indicated|mentioned|listed|noted|stated|shown|described|outlined|detailed|marked)"
_MEALS_HEAD = r"\b(?:all\s+)?(?:the\s+)?meals?\s+(?:are\s+|is\s+)?(?:(?:included|provided|served)\s+)?"
# Only these may sit between "in / as per the" and "itinerary": "during the evening programme" is a meal claim
# (review round 4), "as per the tour itinerary" is not.
_ITIN_ADJ = (r"(?:(?:tour|trip|travel|daily|day['’]?s|day-by-day|detailed|above|below|given|attached|following|"
             r"final|full|program(?:me)?)\s+)?")
_AS_PER_ITINERARY_RE = re.compile(
    _MEALS_HEAD + r"(?:"
    r"(?:as|where|when)\s+" + _SAID_VERB + r"\b(?:[^.;\n]{0,25}?\b" + _ITIN_WORD + r"\b)?"
    r"|" + _SAID_VERB + r"\s+(?:in|on|by)\s+(?:the\s+)?" + _ITIN_ADJ + _ITIN_WORD + r"\b"
    r"|(?:as\s+per|per|according\s+to|as\s+in|in|on|during|following)\s+(?:the\s+)?" + _ITIN_ADJ + _ITIN_WORD
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
    r"|\b(?:time|weather)\s+permitting\b|\bshould\s+you\b"
    # review round 4: a suggestion ("You might like to stop for lunch at Sheroes Hangout", "you may wish to have
    # dinner", "Consider dinner at the night market") and the guest's own food ("bring your own packed lunch").
    r"|\b(?:might|may|could)\s+(?:also\s+)?(?:like|wish|want|choose|prefer|care|fancy|opt|consider|try|decide)\b"
    r"|\bconsider\w*|\bperhaps\b|\bmaybe\b|\bwhy\s+not\b|\bfeel\s+free\b"
    r"|\b(?:your|their|one['’]s)\s+own\s+(?:\w+\s+){0,2}?(?:packed|picnic|food|snacks?|" + _MEAL_WORD + r"|meals?)\b"
    # review round 5: "Neither lunch nor dinner is included", "for your account", and the non-English cues an English
    # source carries ("Lunch (tự túc)" = self-paid, "Lunch: libre" = on your own, "non inclus", "no incluido").
    r"|\bneither\b|\bnor\b|\bfor\s+(?:your|the\s+guests?['’]?s?|guests?['’]?|clients?['’]?s?|travell?ers?['’]?s?|"
    r"participants?['’]?s?|own)\s+account\b"
    r"|\btự\s+(?:túc|lo|trả|chi\s+trả|phục\s+vụ)|\bkhông\s+bao\s+gồm|\blibre\b|\bnon\s+inclus\w*|\bpas\s+inclus\w*"
    r"|\bà\s+(?:votre|la)\s+charge\b|\bno\s+incluid[oa]s?\b|\bpor\s+su\s+cuenta\b|\bnicht\s+inbegriffen\b"
    r"|\bauf\s+eigene\s+kosten\b",
    re.IGNORECASE)
# PRE: the traveller picks or finds the meal ("opt for a traditional dinner", "free to find a local dinner",
# "tips on great places to grab a local dinner", "Choose your lunch at various shops"). Only BEFORE the meal,
# within NEG_WINDOW words ("After breakfast, choose a hike or a rest" keeps the breakfast).
# Review round 4: "bring a picnic lunch with you" / "pack your own lunch" (the guest brings it; "bring you to a
# restaurant" is transport), "decide where to enjoy one final dinner".
_PRE_CUE_RE = re.compile(
    r"\bopt(?:s|ing|ed)?\s+(?:for|to|in|into)\b|\bfree\s+to\b|\btips?\b|\bgrab\b|\bfind\b|\bchoose\b"
    r"|\bbring(?:s|ing)?\b(?!\s+(?:you|us|them|guests?|everyone|the\s+group)\b)|\bcarry\b|\bpack\s+(?:your|a|some)\b"
    r"|\bbyo\b|\bdecid\w*",
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
# Source side, review round 4: a meal of another day inside this day's text ("the following morning ... for
# breakfast", "last night's dinner", "on the last evening of the tour", "at the end of the tour").
_ORD = r"(?:last|final|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|other|penultimate)"
_TRIP = r"(?:tour|trip|journey|holiday|stay|trek|cruise|programme|program)"
_SRC_OTHER_DAY_RE = re.compile(
    r"\btomorrow\b|\b(?:next|following)\s+(?:day|morning|evening|night|afternoon)\b|\byesterday\b"
    r"|\blast\s+(?:night|evening)\b|\bprevious\s+(?:day|evening|night|morning|afternoon)\b"
    r"|\bthe\s+(?:day|night|evening)\s+(?:before|after)\b"
    r"|\bon\s+(?:the|your|our)\s+" + _ORD + r"\s+(?:day|evening|night|morning|afternoon)\b"
    r"|\b" + _ORD + r"\s+(?:day|evening|night|morning)\s+of\s+(?:the|your|our)\s+" + _TRIP + r"\b"
    r"|\b(?:at|towards?|by)\s+the\s+end\s+of\s+(?:the|your|our)\s+" + _TRIP + r"\b"
    # review round 5: numeric ordinals and numbered nights ("Dinner on the 2nd evening", "Your 2nd-night dinner",
    # "on night 2"), and a word ordinal joined to its night ("your second-night dinner").
    r"|\b\d{1,2}(?:st|nd|rd|th)[\s\-]+(?:day|evening|night|morning|afternoon)s?\b"
    r"|\b(?:evening|night|morning)\s*#?\s*\d{1,2}\b(?!\s*(?:hours?|hrs?|km|m\b))"
    r"|\b(?:second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|final)-(?:day|evening|night|morning)\b",
    re.IGNORECASE)
# One of two meals ("lunch or dinner with the family", "breakfast or brunch"): neither is sourced.
_MEAL_ALT_RE = re.compile(r"\b" + _MEAL_WORD + r"\s*,?\s+(?:or|either)\s+(?:a\s+|an\s+|the\s+)?" + _MEAL_WORD + r"\b",
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
# Review round 4: "on your own" only as the end of a phrase ("dinner is on your own in town", not "on your own
# private balcony"), "recommend" / "suggest" only as a verb or noun ("your guide can recommend", not "a highly
# recommended restaurant"), and "without lunch" not in "no visit is complete without lunch".
_OUT_EXCL_RE = re.compile(
    _HARD_NEG_RE.pattern + r"|\boptional\b"
    r"|\bon\s+(?:your|their|one['’]s)\s+own\b(?=\s*(?:$|[^\w\s]|(?:in|at|as|and|or|if|for|before|after|while|"
    r"during|expense|account|cost|arrangements?|time|choice)\b))"
    r"|\b(?:recommends?|suggests?|recommendations?|suggestions?)\b"
    r"|\b(?:can|will|may|could|would|happy\s+to)\s+(?:\w+\s+)?(?:recommend|suggest)\w*"
    r"|\bno\s+(?:\w+\s+)?" + _MEAL_WORD + r"\b|(?<!complete\s)\bwithout\s+(?:a\s+)?" + _MEAL_WORD + r"\b",
    re.IGNORECASE)
# Clause boundaries for negation scope. Commas do NOT split: "Breakfast, lunch and dinner are not included"
# negates all three (fail-closed when a comma list mixes included and excluded meals). "|" and bullets do not
# split either: "Meals included: Breakfast | Lunch | Dinner" is one list. Brackets are handled by _normalise.
# Review round 5: "however" and "while" do not split ("Lunch, however, is at your own expense", "Lunch while in the
# Old Quarter is at your own expense" keep the meal with its cue), and neither does a "but" that opens the cue itself
# ("Lunch, but at your own expense", "lunch, but it is not included").
_CLAUSE_SPLIT_RE = re.compile(
    r"[.;!?\n]+|\b(?:but|whereas)\b(?!\s+(?:at|not|on|payable|own|optional|extra|for|with|paid|excl\w*|self\w*|is|are|"
    r"it|this|that|which|they|these|those|guests?|you|only)\b)", re.IGNORECASE)
# Output side, the split stays as it was: a cue after "while" / "however" / "but" is about the other clause ("All meals
# are served on board while drinks are at your own expense" claims all three).
_OUT_CLAUSE_SPLIT_RE = re.compile(r"[.;!?\n]+|\b(?:but|however|whereas|while)\b", re.IGNORECASE)
# A comma segment that is only meal words: an item of a list whose predicate comes later ("Lunch, dinner and
# drinks ... are not included").
_BARE_ITEM_RE = re.compile(r"\s*(?:(?:and|or|&|the|a|all|your)\s+)*" + _MEAL_WORD
                           + r"(?:\s*(?:and|or|&|/)\s*" + _MEAL_WORD + r")*\s*$", re.IGNORECASE)
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
# Review round 4: "7 p.m." / "6 A.M." are times too. Every time is compared as 24-hour "HH:MM" ("7 p.m." = "7:00 pm"
# = "19:00"), so the same time written another way is still the source's time.
_AMPM_RE = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s?([ap])\.?\s?m\b\.?", re.IGNORECASE)
_H24_RE = re.compile(r"(?<![\d.:])(\d{1,2})[:.h](\d{2})(?![\d])(?!\s?[ap]\.?\s?m\b)", re.IGNORECASE)
_NUM_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
              "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty")
_NUM_WORD_RE = "|".join(_NUM_WORDS)
# Times in words. "Midday" / "afternoon" are parts of the day, not times, and are not checked.
_WORD_TIME_RE = re.compile(
    r"\b(?:noon|midnight)\b|\b(?:\d{1,2}|" + _NUM_WORD_RE + r")\s*o['’]?\s*clock\b(?!\s+(?:in\s+the|at\s+night))"
    r"|\b(?:half|quarter)\s+(?:past|to)\s+(?:\d{1,2}|" + _NUM_WORD_RE + r")\b",
    re.IGNORECASE)
# "at six in the morning", "by 7 in the evening", "around 9 at night" (a preposition first: "Day 2 in the morning"
# is not a time).
_SPOKEN_TIME_RE = re.compile(
    r"\b(?:at|by|around|about|from|until|till|before|after|approx\.?|approximately|between|and|to)\s+"
    r"(\d{1,2}|" + _NUM_WORD_RE + r")(?:[:.](\d{2}))?\s*(?:o['’]?\s*clock\s+)?"
    r"(?:in\s+the\s+(morning|afternoon|evening)|at\s+(night))\b", re.IGNORECASE)
_GENERIC_MEAL_RE = re.compile(r"\bmeals?\b", re.IGNORECASE)
_PROVIDED_RE = re.compile(r"\b(?:provided|included|served|arranged|supplied|offered|hosted)\b", re.IGNORECASE)
_DAILY_RE = re.compile(
    r"\bdaily\b|\b(?:every|each)\s+(?:day|morning|evening|night)\b|\bper\s+day\b|\ba\s+day\b|\bnightly\b"
    r"|\bthroughout\s+(?:the\s+)?(?:tour|trip|stay|journey|holiday)\b",
    re.IGNORECASE)
# A count of meals: a number, a space (or "x"), at most one plain word, a PLURAL meal word ("11 breakfasts",
# "11 x breakfasts", "7 buffet breakfasts"). Review round 4: a number that describes the meal is not a count
# ("10-course dinner", "3-hour dinner cruise", "welcome 4-course dinner"), nor is the end of a day range
# ("Days 3-6 lunch"), nor a count with an occasion word ("11 welcome breakfasts").
_COUNT_RE = re.compile(
    r"(?<![\d\-–—/&])(?<![Dd]ay\s)(?<![Dd]ays\s)(?<![Dd]ay)\b(\d{1,2}|" + _NUM_WORD_RE + r")(?:\s*x\s*|\s+)"
    r"(?:(?!(?:welcome|farewell|gala|special|festive|celebration|christmas|themed|optional|extra)\b)[A-Za-z]+\s+)?"
    r"(breakfasts|brunches|lunch(?:es|eons)|dinners|suppers)\b", re.IGNORECASE)
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
    r"|\bexcept\b|\bonly\b|\bwhile\b|\bwhilst\b|\bduring\b|\bfrom\s+day\b|\buntil\b|\btill\b"
    # review round 4: "from the second morning", "starting on Day 2", "from day 2 onwards"
    r"|\bfrom\s+(?:the\s+)?(?:first|second|third|fourth|next|following|\d)|\bstarting\b|\bbeginning\b|\bcommencing\b"
    r"|\bonwards?\b",
    re.IGNORECASE)
# INCLUSIONS, review round 4: a place or a kind of day after a daily meal scopes it ("daily breakfast and dinner in
# Kathmandu", "breakfast and lunch daily on trekking days", "lunch and dinner daily on the cruise"). A venue does
# not ("daily breakfast at the hotel", "in 3-4 star hotels", "in all hotels", "on a twin share basis").
_VENUE = (r"(?:hotels?|accommodations?|lodges?|resorts?|guest\s?houses?|rooms?|restaurants?|ryokans?|inns?|"
          r"properties|homestays?|basis)")
_PLACE_SCOPE_RE = re.compile(
    r"\b(?:in|on|aboard|onboard|during|while|whilst|when|between|outside)\b(?!(?:\s+[\w\-]+){0,3}?\s+" + _VENUE
    + r"\b)", re.IGNORECASE)
# A later comma segment with no meal that opens with a day scope: ", starting on Day 2", ", from day 2 onwards",
# ", except on arrival day".
_TRAILING_SCOPE_RE = re.compile(
    r"\s*(?:and\s+|but\s+)?(?:starting|beginning|commencing|from|until|till|except|excluding|only|on\s+days?\b|"
    r"on\s+the\s+(?:first|second|third|last|final)|on\s+arrival|on\s+departure|days?\s*\d)",
    re.IGNORECASE)
# A clause that opens with a day number or range ("Day 1-2: Hotel in Kathmandu with daily breakfast", "Days 1 & 2 -
# daily breakfast") scopes every meal in it.
_LEADING_DAY_RE = re.compile(r"^\s*[\-•*·]?\s*(?:days?|nights?)\s*\d", re.IGNORECASE)
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
# Output side, review round 5: a sentence whose scope is more than its own day (the whole tour, the days to come, a
# later day): "every morning of the tour", "daily throughout the trip", "each day of the trek", "on every remaining
# day", "each of the following days", "over the next four days", "for the rest of the trek", "on both days of the
# cruise", "two days later", "later in the trip", "from today".
_MULTI_DAY_RE = re.compile(
    r"\b(?:every|each)\s+(?:(?:single|other|remaining|subsequent)\s+)?(?:day|morning|evening|night|afternoon)s?\b"
    # "daily" as an adverb or on a meal ("served daily", "daily breakfast"), not "daily monastic life"
    r"|\b(?:every|each)\s+of\s+the\b|\bdaily\b(?=\s*(?:$|[.,;:!?)\u2013\u2014]|(?:at|in|on|from|throughout|during|"
    r"for|and|with|by|basis)\b|" + _MEAL_WORD + r"\b|meals?\b))"
    r"|\bnightly\b|\bper\s+day\b|\b(?:meals?|times|once|twice)\s+a\s+day\b"
    r"|\bthroughout\s+(?:the\s+)?(?:\w+\s+)?" + _TRIP + r"\b"
    r"|\b(?:following|remaining|subsequent|coming)\s+(?:\w+\s+)?(?:days|mornings|evenings|nights)\b"
    r"|\bremaining\s+day\b|\bnext\s+(?:\w+\s+)?(?:days|mornings|evenings|nights)\b"
    r"|\bover\s+the\s+(?:next|coming|following)\b|\bfor\s+the\s+(?:next|duration|length)\b"
    r"|\b(?:rest|remainder)\s+of\s+(?:the|your|our)\s+(?!(?:day|afternoon|evening|morning|night)\b)"
    r"|\b(?:both|all)\s+(?:\w+\s+)?(?:days|mornings|evenings|nights)\b|\b(?:days?|nights?|weeks?)\s+later\b"
    r"|\blater\s+(?:in|on|during)\s+the\b|\bfrom\s+(?:today|now|this\s+(?:day|point|morning|evening))\b"
    r"|\b(?:day\s*\d+|today|now|then)\s+onwards?\b|\bfrom\s+(?:\w+\s+){0,3}?onwards?\b|\b(?:thereafter|henceforth)\b"
    r"|\b(?:on|for)\s+(?:the\s+)?(?:other|later|remaining)\s+days\b",
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
    # review round 5: only an exhaustive list says which ("meals (breakfast and dinner)", "Meals: B, L", "meals, namely
    # lunch"); "Meals including lunch", "meals such as lunch", "meals with lunch" name one of several.
    r"\s*(?:(?:are\s+|is\s+)?(?:included|provided|served)\s*)?(?:[(\[:\-\u2013\u2014]|,?\s*(?:"
    r"namely|i\.e\.?|of))\s*(?:the\s+|a\s+|an\s+)?(?:" + _MEAL_WORD + r"\b|evening\b|picnic\b"
    r"|packed\b|[BLD](?![A-Za-z])|none\b|nil\b|no\b|not\b)", re.IGNORECASE)
# Review round 5: a reference to the day's list in so many words ("as listed", "see the list below", "listed for the
# day"); "where you can see the karsts" and "listed among the bay's best" are not.
_MEALS_REFERENCE_RE = re.compile(
    r"\b(?:as|where|when)\s+(?:listed|noted|indicated|shown|specified|stated|mentioned|per|above|below)\b"
    r"|\blisted\s+(?:below|above|for\s+(?:the|each|this)\s+day|in\s+the\s+(?:itinerary|programme|program|"
    r"schedule|table|list|inclusions))\b"
    r"|\bsee\s+(?:the\s+)?(?:(?:daily|day['’]s|meal|meals|list|above|below|itinerary|programme|program|schedule|"
    r"table|details?|inclusions)\b)", re.IGNORECASE)
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


# Source side, review round 5 (all applied by ``_source_prepass`` before brackets are read):
# an abbreviation's period does not end a clause ("approx. 1 hour), at own expense", "near Mt. Fuji (own expense)");
_ABBREV_RE = re.compile(
    r"\b(approx|appr|ca|est|mt|mts|st|ste|ft|nos|nr|vs|etc|incl|excl|hrs?|mins?|kms?|dr|mr|mrs|ms|jr|sr|ave|rd|opp|"
    r"pp|pax|avg|tel|e\.g|i\.e)\.", re.IGNORECASE)
_AMPM_DOTS_RE = re.compile(r"\b([ap])\.\s?m\.", re.IGNORECASE)
# ... a per-meal field with a placeholder value says the meal is not given ("Lunch: N/A", "Dinner: -", "Lunch: none");
_PLACEHOLDER_RE = re.compile(
    r"\b(" + _MEAL_WORD + r")\s*[:=]\s*(?:n/?a|none|nil|no|not\s+included|tb[ac]|[\-–—x×✗✘]+)"
    r"(?=\s*(?:$|[|,;/.\n]|(?:breakfast|lunch|dinner|supper)\b))", re.IGNORECASE | re.MULTILINE)
# ... a code inside a code list with its own negating bracket ("Meals: B, L (own expense), D") is that meal negated,
# and the rest of the list stays whole;
_CODE_BRACKET_RE = re.compile(r"(\s*(?:[,/&+|]|\band)\s*)?(?<![A-Za-z])([BLD])\s*[(\[]([^()\[\]]*)[)\]]"
                              r"(\s*(?:[,/&+|]|\band)\s*(?=[BLD](?![A-Za-z])))?")
# ... a board or meal-count term followed by its own list of meals is those meals ("half board (breakfast and lunch)",
# "2 meals daily (breakfast and dinner)", "Daily meals: breakfast and dinner"; "breakfast and one other meal" is
# breakfast only), and a count under three with no list names no meal ("2 meals daily").
_BOARD_TERM = (r"(?:(?:half|full)[\s-]+board|(?:(?:\d|one|two|three)\s+)?(?:daily\s+)?meals?\s+(?:daily|a\s+day|per\s+"
               r"day)|(?:\d|one|two|three)\s+(?:daily\s+)?meals?|daily\s+meals?)")
_BOARD_LIST_RE = re.compile(r"\b" + _BOARD_TERM + r"\s*(?:[(\[]([^()\[\];\n]*)[)\]]?|[:\-–]\s*([^;\n|.]*))",
                            re.IGNORECASE)
_BOARD_LIST_WORDS_RE = re.compile(
    r"\b(?:and|or|&|plus|one|other|another|meals?|daily|each\s+day|per\s+day|a\s+day|included|provided|only)\b|[,/+&]",
    re.IGNORECASE)
_SMALL_COUNT_RE = re.compile(r"\b(?:[12]|one|two)\s+(?:daily\s+)?meals?\b(?:\s+(?:daily|a\s+day|per\s+day))?",
                             re.IGNORECASE)


def _source_prepass(text: str) -> str:
    """Source side, review round 5: abbreviations, per-meal placeholders, negated codes inside a code list and board
    terms with their own list (see the regexes above). Fail-closed: a term it cannot read names no meal."""
    t = _AMPM_DOTS_RE.sub(lambda m: m.group(1) + "m", text)
    t = _ABBREV_RE.sub(lambda m: m.group(1).replace(".", ""), t)
    t = _PLACEHOLDER_RE.sub(lambda m: f" ; {m.group(1)} not included ; ", t)

    def code(m: re.Match) -> str:
        inner = m.group(3)
        if (_word_meals(inner) or re.fullmatch(r"\s*" + _CODE_LIST + r"\s*", inner)
                or not _BRACKET_NEG_RE.search(inner) or not (m.group(1) or m.group(4))):
            return m.group(0)
        tail.append(f"{_CODE_MEAL[m.group(2)]} not included")
        return m.group(1) if m.group(1) and m.group(4) else " "

    tail: list[str] = []
    t = _CODE_BRACKET_RE.sub(code, t)
    if tail:
        t += " ; " + " ; ".join(tail) + " ; "

    def board(m: re.Match) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2) or ""
        named = _word_meals(inner)
        # only a plain list of meals ("breakfast and lunch", "breakfast and one other meal daily included"); anything
        # else ("dinner not included", "except lunch on day 3", "lunch boxes on trek days") is left as it was
        residue = _BOARD_LIST_WORDS_RE.sub(" ", _ANY_MEAL_WORD_RE.sub(" ", inner))
        if (not named and not re.search(r"\b(?:other|another)\s+meal", inner, re.IGNORECASE)) or re.search(
                r"[A-Za-z]", residue):
            return m.group(0)
        named -= {k for a in _MEAL_ALT_RE.finditer(inner) for k in _word_meals(a.group(0))}
        return " " + (" and ".join(k for k in MEALS if k in named) + " daily" if named else "") + " "

    t = _BOARD_LIST_RE.sub(board, t)
    return _SMALL_COUNT_RE.sub(" some meals ", t)


def _normalise(text: str, bracket_rx: re.Pattern = _BRACKET_NEG_RE, out: bool = False) -> str:
    """One pass before clauses are read: "no extra charge" is positive, a code legend and "meals as per the
    itinerary" are removed, a bracketed code list "(L, D)" becomes its own "meals: L, D" clause, a bracket
    that names a meal or says "no meals" becomes its own clause ("(Lunch is not included)", "(Overnight
    stay・No meal provided)"), a bracket with a ``bracket_rx`` cue and no meal becomes a hard "not included"
    that negates the whole comma segment before it ("lunch (own account)", and "[No cycling] After breakfast"
    leaves the breakfast alone), and any other bracket is read inline. ``out`` (output side): a bracket that
    names a meal or holds a cue is its own clause, so "(drinks not included)" never drops the meal before it; any
    other bracket is an annotation, moved to a clause of its own at the end ("Meals: B (hotel), D (homestay)"
    keeps its code list whole). On the output side a code legend is a claim ("Meals: Breakfast (B), Lunch (L)"),
    so it is kept (review round 4)."""
    t = _FREE_RE.sub(" included ", text or "")
    if not out:
        t = _source_prepass(t)
    if not out and len(_LEGEND_PAIR_RE.findall(t)) >= 2:
        t = _LEGEND_PAIR_RE.sub(" ", t)
    t = _BRACKET_CODES_RE.sub(lambda m: f" ; meals: {m.group(1)} ; ", t)
    moved: list[str] = []

    def bracket(m: re.Match) -> str:
        inner = m.group(1)
        if _word_meals(inner) or _GENERIC_NEG_RE.search(inner) or (out and bracket_rx.search(inner)):
            return f" ; {inner} ; "
        if out:
            moved.append(inner)
            return " "
        if bracket_rx.search(inner):
            return f" {_BRX if _BRACKET_STRONG_RE.search(inner) else _BRX_WEAK} , "
        return f" {inner} "

    for _ in range(3):                      # nested brackets, innermost first
        nxt = _BRACKET_RE.sub(bracket, t)
        if nxt == t:
            break
        t = nxt
    if moved:
        t += " ; " + " ; ".join(moved)
    return _AS_PER_ITINERARY_RE.sub(" ", t)


def _clauses(text: str, bracket_rx: re.Pattern = _BRACKET_NEG_RE, out: bool = False) -> list[str]:
    split = _OUT_CLAUSE_SPLIT_RE if out else _CLAUSE_SPLIT_RE
    return [c for c in split.split(_normalise(text, bracket_rx, out)) if c and c.strip()]


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
    included") or opens with a cue (", optional in your tour package", ", at own expense"). Review round 4: a
    meal in a bare list item ("Lunch, dinner and drinks at the hotel restaurant are not included") takes the
    negation of the first segment after the list that is not a bare item, whatever the distance."""
    segs = list(re.finditer(r"[^,]+", clause))
    for i, seg in enumerate(segs):
        if not _BARE_ITEM_RE.match(seg.group(0)) or not any(seg.start() <= s and e <= seg.end() for s, e in spans):
            continue
        nxt = next((t for t in segs[i + 1:] if not _BARE_ITEM_RE.match(t.group(0))), None)
        if nxt is not None and (hard_rx.search(nxt.group(0)) or _EXPLICIT_CUE_RE.search(nxt.group(0))):
            return True
    for seg in segs:
        body = seg.group(0)
        if _BRX in body:
            # ... but not back over a structured list ("Meals included: breakfast | dinner ... (not ...)").
            mark = seg.start() + body.index(_BRX)
            # (the whole list: "Meals included breakfast | dinner ... (ok maybe not that fast)" keeps the dinner)
            listed = [_LIST_TAIL_RE.match(clause, m.end()).end()
                      for m in _STRUCTURED_RE.finditer(clause, seg.start(), mark)]
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
    if any(a <= s and e <= b for a, b in (m.span() for m in _MEAL_ALT_RE.finditer(clause)) for s, e in spans):
        return "hard"                       # "lunch or dinner with the family": one of the two, not both
    if (_hard_negated(clause, spans) or _near(clause, spans, _EXPLICIT_CUE_RE, NEG_WINDOW, after=-1)
            or _near_seg(clause, spans, _EXPLICIT_CUE_RE, NEG_WINDOW)
            or _near(clause, spans, _PRE_CUE_RE, NEG_WINDOW, after=-1)):
        return "hard"
    if _near(clause, spans, _WEAK_CUE_RE, NEG_WINDOW) or _near(clause, spans, _SOFT_CUE_RE, NEG_WINDOW, SOFT_AFTER):
        return "weak"
    if _other_day(clause, spans):
        return "weak"
    return None


def _other_day(clause: str, spans: list[tuple[int, int]]) -> bool:
    """Review round 4: the meal belongs to another day of the tour ("Arriving in Lao Cai early the following
    morning, the group is taken to a local restaurant for breakfast", "After last night's khantoke dinner", "Our
    welcome dinner takes place on the last evening of the tour"): an other-day cue anywhere before the meal in
    its clause, or after it in its own comma segment ("After breakfast, board the night train, arriving the next
    morning" keeps the breakfast)."""
    for cue in _SRC_OTHER_DAY_RE.finditer(clause):
        for s, e in spans:
            if cue.end() <= s:
                return True
            if cue.start() >= e and "," not in clause[e:cue.start()]:
                return True
    return False


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


# Source day text, review round 5: "all meals" / "meals daily" / "three meals" names the day's meals only when the
# clause says they are included or provided, or it is a board term or a "Meals:" label ("All meals included"); "the
# Community Kitchen, serving thousands of free vegetarian meals daily to visitors" describes a place.
_GENERIC_PROVIDED_RE = re.compile(
    r"\b(?:full|half)[\s-]+board\b|\b(?:included|includes?|inclusive|provided|supplied)\b"
    r"|\bmeals?\s*(?:included|provided)?\s*[:\-\u2013]", re.IGNORECASE)


def _meals_named(text: str, source: bool = False) -> set[str]:
    """Meals a piece of (normalised) text names: words, codes in a meal context, full/half board, all meals
    (on the ``source`` side only when the clause says they are provided)."""
    found = _word_meals(text)
    for rx in _CODE_RES:
        for m in rx.finditer(text):
            found |= {_CODE_MEAL[ch] for ch in m.group(1) if ch in _CODE_MEAL}
    if source and not _GENERIC_PROVIDED_RE.search(text):
        return found
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
        for meal in _meals_named(clause, source=True):
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
        out |= {m for m in _meals_named(clause, source=True) if _stated_as_provided(clause, m)
                and not _negated(clause, m)}
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
            out |= {m for m in _meals_named(clause, source=True) if not _negated(clause, m)}
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
            if _DAY_QUALIFIER_RE.search(tail) or _scoped(clause, min(cue.start(), s), max(cue.end(), e), others):
                continue
            return True
    return False


def _scoped(clause: str, a: int, b: int, others: list[tuple[int, int]]) -> bool:
    """INCLUSIONS, review round 4: the item ``clause[a:b]`` (a daily cue and its meal, or a count) is tied to some
    days or places: a place or kind of day after it in its comma segment, before the next meal ("daily breakfast
    and dinner in Kathmandu", "lunch and dinner daily on the cruise"), a day qualifier before it in its comma
    segment or a day number opening the clause ("Day 1-2: Hotel in Kathmandu with daily breakfast"), or a later
    comma segment with no meal that opens with a day scope ("daily breakfast, starting on Day 2")."""
    seg_a = max(clause.rfind(c, 0, a) for c in ",;") + 1
    ends = [i for i in (clause.find(c, b) for c in ",;") if i >= 0]
    seg_b = min(ends) if ends else len(clause)
    nxt = [os for os, _oe in others if b <= os < seg_b]
    tail = clause[b:min(nxt) if nxt else seg_b]
    if _PLACE_SCOPE_RE.search(_WHOLE_TOUR_RE.sub(" ", tail)) or _DAY_QUALIFIER_RE.search(tail):
        return True
    if _DAY_QUALIFIER_RE.search(clause[seg_a:a]) or _LEADING_DAY_RE.match(clause):
        return True
    for seg in re.finditer(r"[^,;]+", clause[seg_b:]):
        if not _word_meals(seg.group(0)) and _TRAILING_SCOPE_RE.match(seg.group(0)):
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
                others = [sp for k in MEALS if k != meal for sp in _explicit_spans(clause, k)]
                if (meal and meal not in negated and _count_covers(meal, _count_of(m.group(1)), n_days, day)
                        and not _scoped(clause, m.start(), m.end(), others)):
                    cover.add(meal)
    cover |= _unscoped_board(inclusions)
    return cover - neg, neg


def _out_excluded(clause: str, meal: str) -> bool:
    """Output side: an explicit exclusion next to the meal ("lunch is not included", "optional dinner")."""
    spans = _explicit_spans(clause, meal)
    if not spans:
        return bool(_OUT_EXCL_RE.search(clause))
    return _near_seg(clause, spans, _OUT_EXCL_RE, OUT_WINDOW)


def _noun_meals(clause: str, sentence_meal: bool = False) -> set[str]:
    """Output side: meals claimed through a noun that names none ("a seafood feast awaits on board in the
    evening" = dinner, "a picnic is served at midday" = lunch, "a beach BBQ" = lunch and dinner, "Mid-day, a
    meal is served" = lunch). Skipped when a meal word modifies it or it modifies one, with nothing between
    ("BBQ dinner", "picnic breakfast", "barbecue-style dinner"), or it is excluded ("optional BBQ").

    Review round 4: "A midday meal and evening dinner" claims lunch too, and "After breakfast, a picnic" claims
    the picnic's lunch (a meal word two words away no longer hides the noun). The time of day counts only next to
    the noun (its comma segment, within TIME_WINDOW words) or in a short opening segment ("In the evening, a
    feast"), and not when it describes an activity ("After a morning of snorkelling, a beach barbecue" is not
    breakfast). A singular "a meal" with no time of day claims lunch and dinner, like a feast: it is one of the
    two, and the source must have both to clear it (fail-closed). When the sentence names a meal
    (``sentence_meal``), an untimed "a meal" is that meal again ("Lunch is taken at the house of local friends,
    featuring a meal prepared from local produce", "Dinner is served as kaiseki, a multi-course meal") and adds
    nothing."""
    out: set[str] = set()
    meal_spans = [sp for m in MEALS for sp in _explicit_spans(clause, m)]
    for rx, default in ((_OUT_FEAST_RE, {"lunch", "dinner"}), (_OUT_PICNIC_RE, {"lunch"}),
                        (_OUT_MEAL_RE, {"lunch", "dinner"})):
        for m in rx.finditer(clause):
            if any(_MODIFIER_GAP_RE.match(clause[e:m.start()]) if e <= m.start() else
                   _MODIFIER_GAP_RE.match(clause[m.end():s]) if s >= m.end() else True for s, e in meal_spans):
                continue
            if _near_seg(clause, [m.span()], _OUT_EXCL_RE, OUT_WINDOW):
                continue
            timed = _noun_time(clause, m.start(), m.end())
            if not timed and rx is _OUT_MEAL_RE and sentence_meal:
                continue
            out |= timed or default
    return out


TIME_WINDOW = 6              # output side: a time of day names a meal noun's meal only this close to it
_MODIFIER_GAP_RE = re.compile(r"[\s\-]*(?:style[\s\-]+)?$", re.IGNORECASE)
_ACTIVITY_TIME_RE = re.compile(r"\s+of\b", re.IGNORECASE)      # "a morning of snorkelling"


def _noun_time(clause: str, a: int, b: int) -> set[str]:
    """The meals a time of day next to the noun ``clause[a:b]`` names (see ``_noun_meals``)."""
    seg_a = clause.rfind(",", 0, a) + 1
    seg_b = clause.find(",", b)
    seg_b = len(clause) if seg_b < 0 else seg_b
    found: set[str] = set()
    for meal, trx in _TIME_OF_DAY:
        for t in trx.finditer(clause):
            if _ACTIVITY_TIME_RE.match(clause, t.end()):
                continue
            if seg_a <= t.start() and t.end() <= seg_b:
                near = _gap(clause, t.end(), a) if t.end() <= a else _gap(clause, b, t.start())
                if near <= TIME_WINDOW:
                    found.add(meal)
            elif t.end() <= seg_a:
                # a short opening segment of the clause: "In the evening, a seafood feast is served"
                opener_a = clause.rfind(",", 0, t.start()) + 1
                opener_b = clause.find(",", t.end())
                if opener_a == 0 and 0 <= opener_b and _gap(clause, 0, opener_b) <= 4:
                    found.add(meal)
    return found


def _bare_meals_claim(sentence: str) -> bool:
    """Output side: a bare plural "meals" that claims all three ("Meals are provided", "Breakfast is included,
    and meals on board the junk are prepared by the onboard chef"). Not when a list right after it says which
    ("meals (breakfast and dinner) are provided", "Meals: B, L"), it refers to the day's list ("as listed"),
    it is negated or excluded ("No meals", "meals at your own expense"), or it is "picnic / evening / all meals"
    (read as named meals elsewhere)."""
    for clause in _OUT_CLAUSE_SPLIT_RE.split(_AS_PER_ITINERARY_RE.sub(" ", sentence or "")):
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


_TIME_MARKER_MEAL_RE = re.compile(
    r"\b(?:after|before|following|until|till|since|post|pre)[\s\-]+(?:(?:a|an|the|your|our|their|early|late|"
    r"leisurely|hearty|quick|light)\s+){0,2}" + _MEAL_WORD + r"\b", re.IGNORECASE)
_OUT_EVERY_MEAL_RE = re.compile(r"\b(?:every|each)\s+(?:single\s+)?meal\b|\ball\s+(?:of\s+)?(?:the|your|our)\s+meals\b",
                                re.IGNORECASE)
_OUT_MEALTIME_RE = re.compile(r"\b(breakfast|lunch|dinner|supper)[\s\-]?times?\b", re.IGNORECASE)
_MEALTIME_MEAL = {"breakfast": "breakfast", "lunch": "lunch", "dinner": "dinner", "supper": "dinner"}
_PICNIC_MEAL_RE = re.compile(r"\b(?:picnic|packed)\s+meals?\b", re.IGNORECASE)


def _out_meals_named(clause: str) -> set[str]:
    """Output side: ``_meals_named``, except that a "picnic meal" / "packed meal" is the meal its time of day says
    (review round 5: "In the evening, a picnic meal is served around a campfire" is dinner, "a packed meal for the
    pre-dawn departure" breakfast), lunch only when no time of day is next to it."""
    found = _meals_named(clause)
    picnics = [m.span() for m in _PICNIC_MEAL_RE.finditer(clause)]
    if not picnics:
        return found
    if all(any(a <= s and e <= b for a, b in picnics) for s, e in _explicit_spans(clause, "lunch")):
        found.discard("lunch")              # lunch came only from the picnic / packed meal
    for a, b in picnics:
        found |= _noun_time(clause, a, b) or {"lunch"}
    return found


def claimed_meals(sentence: str) -> tuple[set[str], bool]:
    """(meals the output sentence claims as provided, generic_claim). A meal is dropped only on an explicit
    exclusion next to it ("Breakfast is included; lunch is at your own expense" claims breakfast only); any
    other "not" / "no" keeps it ("dinner is served at a restaurant not far from the river" claims dinner).
    ``generic_claim`` is True when the sentence states meal logistics without naming a meal ("Meals
    included:")."""
    claimed: set[str] = set()
    # Review round 5: a meal word used as a time marker ("After breakfast, drive to Mai Chau, where a meal is served")
    # is not the meal an untimed "a meal" restates.
    sentence_meal = bool(_ANY_MEAL_WORD_RE.search(_TIME_MARKER_MEAL_RE.sub(" ", sentence or "")))
    for clause in _clauses(sentence, _OUT_EXCL_RE, out=True):
        if _GENERIC_NEG_RE.search(clause):
            # "No meals are included except breakfast": a meal named next to a generic "no meals" is claimed, and so
            # is a meal noun ("apart from a picnic provided for the walk", review round 4).
            claimed |= {m for m in MEALS if _explicit_spans(clause, m)} | _noun_meals(clause, sentence_meal)
            continue
        claimed |= {m for m in _out_meals_named(clause) if not _out_excluded(clause, m)}
        claimed |= _noun_meals(clause, sentence_meal)
        # Review round 5: "every meal" / "each meal" is all three; "at lunchtime" / "at dinnertime" is that meal.
        for rx, meals in ((_OUT_EVERY_MEAL_RE, ALL), (_OUT_MEALTIME_RE, None)):
            for m in rx.finditer(clause):
                if not _near_seg(clause, [m.span()], _OUT_EXCL_RE, OUT_WINDOW):
                    claimed |= meals or {_MEALTIME_MEAL[m.group(1).lower()]}
    if _bare_meals_claim(sentence):
        claimed |= ALL
    generic = (not claimed and bool(_GENERIC_MEAL_RE.search(sentence)) and not _OUT_EXCL_RE.search(sentence)
               and not _GENERIC_NEG_RE.search(sentence))
    return claimed, generic


def _hour(token: str) -> int:
    return int(token) if token.isdigit() else _NUM_WORDS.index(token.lower()) + 1


def _hhmm(hour: int, minutes: Optional[str], pm: Optional[bool]) -> str:
    if pm is True and hour < 12:
        hour += 12
    elif pm is False and hour == 12:
        hour = 0
    return f"{hour:02d}:{minutes or '00'}"


def canonical_clock(text: str) -> str:
    """One am/pm time ("7 p.m.", "7:00 PM", "7pm") as 24-hour "HH:MM" ("19:00")."""
    m = _AMPM_RE.search(text or "")
    if not m:
        return re.sub(r"\s+", "", (text or "").lower())
    return _hhmm(int(m.group(1)), m.group(2), m.group(3).lower() == "p")


def _clock_times(text: str) -> set[str]:
    """Every time in ``text`` in one form: am/pm and spoken times ("six in the morning") as 24-hour "HH:MM",
    24-hour times as written ("19:30", "7.30" = "07:30"), and "noon" / "7 o'clock" / "half past seven" as words."""
    text = text or ""
    out = {_hhmm(int(m.group(1)), m.group(2), m.group(3).lower() == "p") for m in _AMPM_RE.finditer(text)}
    out |= {_hhmm(int(m.group(1)), m.group(2), None) for m in _H24_RE.finditer(text)}
    out |= {_hhmm(_hour(m.group(1)), m.group(2), m.group(3) is None or m.group(3).lower() != "morning")
            for m in _SPOKEN_TIME_RE.finditer(text)}
    out |= {re.sub(r"[\s'’]+", "", m.group(0).lower()) for m in _WORD_TIME_RE.finditer(text)}
    # Review round 5: military "0630 hrs" and "19h" / "at 19" are 24-hour times; a bare hour after a preposition ("at
    # 7", "at seven", "from 6") is a time too, kept as its own "~07" form (it matches only the same bare hour: the
    # source's "at 7" is not "7 pm").
    seen = [m.span() for rx in (_AMPM_RE, _H24_RE, _SPOKEN_TIME_RE, _WORD_TIME_RE) for m in rx.finditer(text)]
    out |= {f"{m.group(1)}:{m.group(2)}" for m in _MILITARY_RE.finditer(text)}
    for m in _BARE_HOUR_RE.finditer(text):
        if any(a < m.end() and m.start() < b for a, b in seen):
            continue
        hour = _hour(m.group(1) or m.group(4))
        if hour > 23:
            continue
        out.add(_hhmm(hour, m.group(2), None) if m.group(2) or m.group(3) or hour > 12 else f"~{hour:02d}")
    return out


_MILITARY_RE = re.compile(r"(?<![\d:.,])([01]\d|2[0-3])([0-5]\d)\s*(?:hrs?|hours|h)\b", re.IGNORECASE)
_TIME_PREP = r"\b(?:at|from|by|around|about|until|till|before|approx|approximately|circa)\s+"
_BARE_HOUR_RE = re.compile(
    _TIME_PREP + r"(?:(\d{1,2})(?:h?([0-5]\d)|(h\b|hrs?\b))?(?![\d,.:]\d)"
    r"(?=\s*(?:$|[.,;:!?)\]\u2013\u2014]|(?:and|at|in|on|before|after|sharp|to|for|until|till|when|with)\b))"
    r"|(" + _NUM_WORD_RE + r")(?=\s*(?:$|[.,;!?)\]]|(?:at|sharp|before|after)\b)))",
    re.IGNORECASE)


def _times_unsupported(sentence: str, scope: str) -> bool:
    return bool(_clock_times(sentence) - _clock_times(scope))


# EXCLUSIONS, review round 4: "Meals other than breakfast", "All meals except breakfast", "Meals (apart from
# breakfast)" exclude every meal but the ones they name.
_EXC_EXCEPT_RE = re.compile(
    r"\b(?:all\s+|any\s+|other\s+)?meals?\b[^.;\n]{0,20}?\b(?:other\s+than|except(?:\s+for)?|apart\s+from|"
    r"aside\s+from|besides|excluding|but|save\s+for)\b([^.;\n]*)", re.IGNORECASE)


def exclusion_meals(exclusions: str) -> set[str]:
    """Meals EXCLUSIONS block: the meals it names, and every meal but the named ones after "meals other than /
    except / apart from". A generic exclusion ("meals not mentioned in the itinerary", "drinks with meals")
    names no specific meal and blocks nothing."""
    if not exclusions:
        return set()
    flat = re.sub(r"[()\[\]]", " ", exclusions)
    out: set[str] = set()
    for m in _EXC_EXCEPT_RE.finditer(flat):
        kept = _word_meals(re.split(r",|\band\s+(?!(?:the\s+)?" + _MEAL_WORD + r")", m.group(1), maxsplit=1)[0])
        if kept:                # "Meals except those mentioned in the itinerary" names no meal: generic
            out |= ALL - kept
    rest = _EXC_EXCEPT_RE.sub(" ; ", flat)
    return out | set().union(*(_word_meals(c) for c in _clauses(rest)))


def day_coverage(day_text: Optional[str], inclusions: str, exclusions: str,
                 n_days: Optional[int] = None, day: Optional[int] = None) -> set[str]:
    """Meals the source covers for one output day (number ``day``). ``day_text`` None = the day could not be
    matched to one source day (only INCLUSIONS can cover then)."""
    inc_cover, inc_neg = inclusion_meals(inclusions, n_days, day)
    blocked = set(inc_neg) | exclusion_meals(exclusions)
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
    if _MULTI_DAY_RE.search(sentence):
        # Review round 5: "Breakfast is included every morning of the tour", "For the rest of the trek, lunch is
        # served", "Two days later, dinner ...": the claim is about other days too, so only INCLUSIONS that cover
        # every day can source it, never the one day the sentence is written under.
        day_text, day = None, None
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


# Output side, review round 4: only a title line sets the day: "Day N" then a separator or the end of the line
# ("Day 3 — Hue", "Day 3: Hue", "Day 3 (B, L)"). A body line that opens "Day 2's steep climb ..." or "Day 4 is the
# route's hardest stage" stays on the day it is written under.
_OUT_TITLE_RE = re.compile(r"^\s*Day\s+(\d+)(?![\w'’])\s*(?:[—–\-:|.(\[]|$)")


def itinerary_sentences(itin_text: str) -> list[dict]:
    """Every sentence of an output itinerary as {field, sentence, day}, split exactly like
    ``grounding.sentence_units`` but keeping day-title lines and sentences shorter than MIN_WORDS: a title
    "Day 3 — Hue (B, L)" or a bare "Lunch is included." is a meal claim too. Only a title line sets the day
    (``_OUT_TITLE_RE``); ``sentence_units`` also reads "Day 2's climb ..." as day 2, so such a sentence, when
    unsourced, is not askable and keeps the code (fail-closed)."""
    from services.content_generation import grounding as g

    out: list[dict] = []
    day: Optional[int] = None
    for ln in (itin_text or "").splitlines():
        if not ln.strip():
            continue
        m = _OUT_TITLE_RE.match(ln)
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
        if last > first and m and m.group(2) and _MONTH_RE.match(stripped[m.end(2):]):
            last = first            # "Day 4 - 15 Oct" is day 4 on 15 October, not days 4 to 15 (review round 4)
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


_MONTH_RE = re.compile(r"\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b\.?", re.IGNORECASE)


_ALT_DAY_RE = re.compile(r"\b(?:alternative\w*|option(?:al)?|extension|extra|instead|or)\b", re.IGNORECASE)
# Review round 5: on a header line, a version or season label marks another version of the day too.
_VARIANT_RE = re.compile(
    r"\b(?:alternative\w*|options?|extension|instead|plan|route|version|variant|"
    r"season\w*|summer|winter|spring|autumn|monsoon|rainy|dry|wet|peak|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\b", re.IGNORECASE)
# The day marker's separator: "Day 3:", "Day 3 -", "Day 3 (", "Day 3 |", "Day 3." (what makes the line a header).
_MARKER_SEP_RE = re.compile(r"^[\s►▶•*#>\-–—·|]*days?\s*#?\s*\w+\b(?:\s*[-–—]\s*\d{1,3}\b)?[*\s]*([:\-–—|.(\[/])",
                            re.IGNORECASE)


def _squash(line: str) -> str:
    return re.sub(r"\W+", "", (line or "").lower())


def _header_like(line: str) -> bool:
    """Review round 5: a "Day N" line that reads as a header: a separator after the number ("Day 3: Hue", "Day 3 -
    Hue", "Day 3 (B, L)"), the number alone ("Day 1"), or a short title with no closing period ("Day One Notes",
    "DAY 3 HALONG BAY"). "Day 3 begins with tai chi on the sundeck at dawn, so dinner tonight is served early." is a
    sentence of the day before."""
    from services.content_generation.day_parse import marker_match

    stripped = (line or "").strip()
    if _MARKER_SEP_RE.match(stripped):
        return True
    m = marker_match(stripped)
    rest = stripped[m.end():] if m else stripped
    return not rest or (len(rest.split()) <= 6 and not re.search(r"[.!?]\s*$", rest) and "," not in rest)


def _titled_header(line: str) -> bool:
    """A "Day N" header with a separator and a title after it ("Day 3: Thimphu - Punakha by the valley road")."""
    stripped = (line or "").strip()
    m = _MARKER_SEP_RE.match(stripped)
    return bool(m) and bool(re.search(r"[A-Za-z]{3}", stripped[m.end():]))


def _marker_runs(src_itin: str, blocks: dict[int, set[int]]) -> tuple[Optional[set[int]], dict[int, str]]:
    """Review round 4: (source days whose text the splitter may have replaced, or None when no day can be
    trusted; {day: merged text} for days written as several blocks in a row).

    The splitter keys day text by the marker's number, so a later marker with a number already seen silently
    replaces that day's text: a body line "Day 2 of cycling is the queen stage" inside Day 3, a post-tour extension
    that starts again at "Day 1", an "alternative" Day 3 after Day 4. Such a number is untrusted, and so is the
    block just before it (cut short there). The same number again right after itself ("Day One Notes", "Day One
    Schedule", "Day three marks a gentle shift" under "Day 3") is one day in several blocks: its text is all of
    them, unless the repeated line offers another version of the day ("Day 3 (alternative in the rainy season)").
    Grouped blocks that overlap another marker's day ("Day 2-3" then "Day 3") mean the ranges are misread:
    nothing is trusted."""
    from services.content_generation.day_parse import _DAY_MARKER_RE, source_day_markers

    markers = source_day_markers(src_itin)
    firsts = [first for first, _last, _idx in markers]
    for first, days in blocks.items():
        if any(f in days and f != first for f in firsts):
            return None, {}
    lines = (src_itin or "").splitlines()
    bad: set[int] = set()
    merged: dict[int, str] = {}
    for i, n in enumerate(firsts):
        if i and n == firsts[i - 1]:
            j = i - 1
            while j and firsts[j - 1] == n:
                j -= 1                      # the first block of the run
            first_line, line = lines[markers[j][2]], lines[markers[i][2]]
            # Review round 5: another version of the day, labelled on either header ("Day 3 (Plan A)" / "Day 3 (Plan
            # B)", "(summer)" / "(winter)", "Day 1 (winter schedule)"), or a second titled header ("Day 3: Thimphu -
            # Punakha via Dochu La" then "Day 3: Thimphu - Punakha by the valley road"), is not the same day.
            if (_ALT_DAY_RE.search(line) or any(_header_like(x) and _VARIANT_RE.search(x) for x in (line, first_line))
                    or (_titled_header(line) and _squash(line) != _squash(first_line))):
                bad.add(n)
            # ... and a run whose first line is a body sentence ("Day 3 begins with tai chi at dawn, so dinner tonight
            # is served early") is a preview inside the day before: neither day can be trusted.
            if not _header_like(first_line):
                bad |= {n} | ({firsts[j - 1]} if j else set())
            merged[n] = ""
        elif i and n < max(firsts[:i]) or n in firsts[:i]:
            bad |= {n, firsts[i - 1]}
    for n in merged:
        idx = [k for k, f in enumerate(firsts) if f == n]
        end = markers[idx[-1] + 1][2] if idx[-1] + 1 < len(markers) else len(lines)
        part = []
        for ln_idx in range(markers[idx[0]][2], end):
            ln = lines[ln_idx]
            part.append(_DAY_MARKER_RE.sub("", ln.strip(), count=1) if any(ln_idx == markers[k][2] for k in idx)
                        else ln)
        merged[n] = " ".join(part).strip()
    return bad, merged


def _source_day_text(day_text_map: dict[int, str], src_itin: str, day: int,
                     out_days: Optional[set[int]]) -> Optional[str]:
    """The source text of output day ``day``, or None when the match cannot be trusted (see
    ``unit_meal_sourced``)."""
    keys = set(day_text_map)
    blocks = _grouped_blocks(src_itin)
    untrusted, merged = _marker_runs(src_itin, blocks)
    if untrusted is None or day in untrusted:
        return None
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
    text = merged.get(day) or day_text_map.get(day)
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
        # that day, whatever the output calls it. Review round 5: unless it is written as several "Day 1" blocks that
        # are not one day (an alternative or seasonal version, "Day 1 (alternative in the rainy season)"): the
        # splitter keeps only the last block, so the same guard as on a multi-day tour applies.
        untrusted, merged = _marker_runs(src_itin, _grouped_blocks(src_itin))
        key = next(iter(day_text_map))
        day_text: Optional[str] = (None if untrusted is None or untrusted
                                   else merged.get(key) or day_text_map[key])
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
