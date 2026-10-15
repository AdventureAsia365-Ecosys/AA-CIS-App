"""AA-793 (F-d) — background-fact allowance for the T3 numeric grounding check.

T3 flags every number a tenant rewrite states that the master content never states
(`tenant_pipeline._t3_grounding_check`). The T2 writer, though, is told it may add short,
well-known background about a place the source names — what it is, its era, its setting
(writer rule 6, `prompts.py`; memory rule "writer source fidelity"). S235 measured the cost: the
check flagged "1954 battle", "17th-century dzong" and "3,120 m" and forced 12 of 14 versions
through 2 full rewrites (report 12 §2.2 cause 4).

This module decides, per novel number, whether it is place background of one of three narrow
kinds. Everything else stays a violation:

- a year in the past ("built in 1954", "the 1630s", "1637–38", "500 BC") — never this year or a
  future one (a departure or festival date is a promise, not background), never a price or count;
- a century or millennium ordinal ("17th-century", "the 17th and 18th centuries");
- an altitude of at least 1,000 m/ft, stated as an elevation ("Dochula Pass (3,120 m)",
  "at 4,460 m", "3,120 m above sea level") — a length ("a 22-metre-long Buddha"), a distance or an
  elevation gain stays flagged.

A sentence in the brand's first person ("we", "our", "us") never gets the allowance: a year or
figure there is a claim about the operator ("since 2010 our guides…"), not about a place. Nor does
a sentence about where the traveller sleeps or eats or who serves them (lodge, hotel, camp, stay,
overnight, restaurant, guide…): "a lodge family-run since 1998" or "camp at 4,460 m" is a claim about
THIS tour's accommodation or service, which the fidelity rule forbids.

An elevation is only background as the height of a place: a sentence that climbs, ascends,
descends or gains right at the number ("climbs 1,200 m to the pass", "a 1,500 m ascent", "a gain
of 1,500 m") states a tour fact.

Durations, clock times, distances, prices, group sizes and counts are never background: those are
the tour promises the fidelity rule forbids, and T3 keeps flagging them. Review hardening: a
4-digit number is a year only with a year cue right before it ("in", "since", "the", "(") or at the
start of the sentence, never with a thousands comma or a plural noun after it ("1,500 monks", "the
1500 statues") or a currency ("1200 ngultrum"); a metre figure next to a walking / route word
("a short walk (1,500 m)", "the trail runs 1,200 m") or followed by "of" ("1,500 m of rapids") is a
distance, and the elevation cue must sit near the number, not anywhere in the sentence.
"""
from __future__ import annotations

import datetime
import re

from services.acp_shared.grounding import _NUM_RE, _YEAR_RANGE_RE, _range_years

YEAR_MIN = 1000
ALTITUDE_MIN = 1000

_FIRST_PERSON_RE = re.compile(r"\b(?:we|we're|we've|we'll|our|ours|us)\b", re.IGNORECASE)
# What follows the digits of an ordinal century: "th-century", "th and 18th centuries".
_CENTURY_AFTER_RE = re.compile(
    r"^(?:st|nd|rd|th)\b[\s\-–—]*"
    r"(?:(?:and|or|to|[\-–—/])\s*\d{1,2}(?:st|nd|rd|th)\b[\s\-–—]*)?"
    r"(?:century|centuries|millennium|millennia)\b",
    re.IGNORECASE)
_ERA_AFTER_RE = re.compile(r"^\s*(?:BC|BCE|AD|CE)\b")
_ERA_BEFORE_RE = re.compile(r"\b(?:AD|CE)\s*$")
# An elevation unit right after the number, not followed by a length word ("22-metre-long").
_ELEV_UNIT_AFTER_RE = re.compile(
    r"^[\s\-–—]*(?:m|metres?|meters?|masl|ft|feet|foot)\b(?![\s\-–—]*(?:long|wide|deep|across)\b)",
    re.IGNORECASE)
_ELEV_CUE_RE = re.compile(
    r"altitude|elevation|above sea level|\b(?:a\.?s\.?l\.?|masl)\b|\bhigh\b|\bheight\b|\bsummit\b"
    r"|\bpeak\b|\bpass\b|\bperched\b|\bplateau\b",
    re.IGNORECASE)
_AT_BEFORE_RE = re.compile(r"\bat\s*$", re.IGNORECASE)
# A climb / descent is a tour fact, not the height of a place ("climbs 1,200 m", "gain of 1,500 m").
_GAIN_RE = re.compile(r"\b(?:climb\w*|ascen\w*|descen\w*|gain\w*|vertical|uphill|downhill)\b", re.IGNORECASE)
_GAIN_WINDOW = 20   # chars either side of the number that a gain word must fall in
_GAIN_BEFORE_RE = re.compile(r"\b(?:up|down|by)\s*$", re.IGNORECASE)
# Accommodation, dining and service words: a figure in such a sentence is about THIS tour.
_SERVICE_RE = re.compile(
    r"\b(?:lodges?|hotels?|guest\s?houses?|homestays?|resorts?|camps?|camping|campsites?|tents?|cabins?"
    r"|stay(?:s|ing)?|overnight|sleep(?:s|ing)?|restaurants?|caf[eé]s?|chefs?|kitchens?|guides?|drivers?"
    r"|crew|porters?)\b",
    re.IGNORECASE)
# A 4-digit number followed by one of these is a quantity, not a year.
_QUANTITY_AFTER_RE = re.compile(
    r"^[\s\-–—]*(?:%|percent\b|m\b|km\b|kms\b|metres?\b|meters?\b|ft\b|feet\b|kg\b|kilometres?\b"
    r"|kilometers?\b|miles?\b|people\b|guests?\b|travell?ers?\b|nights?\b|days?\b|hours?\b|hrs?\b"
    r"|minutes?\b|mins?\b|usd\b|dollars?\b|steps\b|rooms?\b|seats?\b|species\b"
    # review: a local currency after the number is a price ("1200 ngultrum"), not a year
    r"|ngultrum\b|nu\b|btn\b|kip\b|lak\b|dong\b|vnd\b|baht\b|thb\b|rupees?\b|inr\b|npr\b|yuan\b"
    r"|rmb\b|cny\b|yen\b|jpy\b|won\b|krw\b|ringgit\b|myr\b|rupiah\b|idr\b|riels?\b|khr\b|kyats?\b"
    r"|mmk\b|pesos?\b|php\b|eur\b|euros?\b|gbp\b|pounds?\b)",
    re.IGNORECASE)
# review: a bare 4-digit year needs a year cue right before it ("in 1954", "the 1954 battle",
# "since 1637", "(1637)") or must open the sentence; "holds 1500 statues" / "entry is 1200" do not.
_YEAR_CUE_BEFORE_RE = re.compile(
    r"(?:^|\b(?:in|since|from|by|until|till|circa|c\.|ca\.|around|the|of|between|and|to|before|after"
    r"|during|year|built|founded|rebuilt|restored|dating|dates|dated|back|early|late|mid)|[(\[])\s*$",
    re.IGNORECASE)
# ... and a plural noun right after it is a count ("to 1500 monks", "the 1500 statues").
_PLURAL_AFTER_RE = re.compile(
    r"^\s+(?!(?:was|has|is|its|this|as|his|thus|always|perhaps|whereas|besides)\b)[a-z]{2,}s\b")
# review: a walking / riding / route word near an elevation-unit number makes it a distance
# ("a short walk (1,500 m)", "the trail runs 1,200 m", "raft 1,500 m of rapids").
_DISTANCE_RE = re.compile(
    r"\b(?:walk\w*|hik\w*|trek\w*|trail\w*|stroll\w*|driv\w*|drove|rid(?:e|es|ing)|cycl\w*|bik\w*"
    r"|raft\w*|paddl\w*|kayak\w*|swim\w*|run|runs|running|path\w*|routes?|loops?|roads?|tracks?"
    r"|cover(?:s|ed|ing)?|through|along|away|distance|stretch\w*|sections?|length|wide|deep|far)\b",
    re.IGNORECASE)
_DISTANCE_WINDOW = 30   # chars before the number (after: _GAIN_WINDOW) a distance word must fall in
_CUE_WINDOW = 40        # chars either side of the number an elevation cue must fall in
# "1,500 m of forest / rapids / elevation" is a length or a gain, not the height of a place.
_UNIT_OF_RE = re.compile(r"^[\s\-–—]*(?:m|metres?|meters?|masl|ft|feet|foot)\s+of\b", re.IGNORECASE)
_CURRENCY_BEFORE_RE = re.compile(r"(?:[$€£¥₫]|\bUSD|\bUS\$|\bEUR|\bGBP)\s*$", re.IGNORECASE)


def _occurrences(token: str, sentence: str):
    for m in _NUM_RE.finditer(sentence):
        if m.group(0).replace(",", "") == token:
            yield m.start(), m.end()


def _past_year(n: int, year_now: int) -> bool:
    return YEAR_MIN <= n < year_now


def _occurrence_is_background(token: str, sentence: str, start: int, end: int, year_now: int) -> bool:
    before, after = sentence[:start], sentence[end:]
    if _CENTURY_AFTER_RE.match(after):                      # "17th-century"
        return True
    if "." in token:
        return False
    n = int(token)
    if _ERA_AFTER_RE.match(after) or _ERA_BEFORE_RE.search(before):   # "500 BC", "AD 1054"
        return after.lstrip().upper().startswith("B") or n < year_now
    if _ELEV_UNIT_AFTER_RE.match(after):                    # "3,120 m" — only as an elevation
        # a gain word next to the number (not anywhere: "at 5,106 m before the long descent" is a height)
        if (_GAIN_RE.search(before[-_GAIN_WINDOW:]) or _GAIN_RE.search(after[:_GAIN_WINDOW])
                or _GAIN_BEFORE_RE.search(before)):
            return False
        # a distance word next to the number, or "N m of …", makes it a length (review)
        if (_DISTANCE_RE.search(before[-_DISTANCE_WINDOW:]) or _DISTANCE_RE.search(after[:_GAIN_WINDOW])
                or _UNIT_OF_RE.match(after)):
            return False
        near = before[-_CUE_WINDOW:] + sentence[start:end] + after[:_CUE_WINDOW]
        return n >= ALTITUDE_MIN and bool(
            _ELEV_CUE_RE.search(near) or _AT_BEFORE_RE.search(before) or before.rstrip().endswith("("))
    if "," in sentence[start:end]:                          # "1,500 monks" is a count, never a year
        return False
    return (len(token) == 4 and _past_year(n, year_now)
            and bool(_YEAR_CUE_BEFORE_RE.search(before)) and not _PLURAL_AFTER_RE.match(after)
            and not _QUANTITY_AFTER_RE.match(after) and not _CURRENCY_BEFORE_RE.search(before))


def is_background_number(token: str, sentence: str, *, year_now: int | None = None) -> bool:
    """True when `token` (a novel number as `find_novel_numeric_claims(..., year_forms=True)`
    reports it: "1954", "17", "3120", "1630s", or a range end year "1638") is place background of
    an allowed kind in `sentence`. Every occurrence of the number in the sentence must qualify."""
    if not token or _FIRST_PERSON_RE.search(sentence or "") or _SERVICE_RE.search(sentence or ""):
        return False
    year_now = year_now or datetime.date.today().year
    if token.endswith("s") and token[:-1].isdigit():         # a decade: "1630s"
        return _past_year(int(token[:-1]), year_now)
    occurrences = list(_occurrences(token, sentence))
    if not occurrences:
        # the full end year of a range, reported for "1637–38" as "1638"
        if token.isdigit() and len(token) == 4:
            return any(str(_range_years(m)[1]) == token and _past_year(int(token), year_now)
                       for m in _YEAR_RANGE_RE.finditer(sentence))
        return False
    return all(_occurrence_is_background(token, sentence, s, e, year_now) for s, e in occurrences)
