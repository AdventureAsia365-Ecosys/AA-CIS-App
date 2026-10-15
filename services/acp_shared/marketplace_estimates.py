"""
services.acp_shared.marketplace_estimates — AA-330 Phần B: runway_months() +
parse_price(), commercial-estimate pure functions backing
api/routers/admin_marketplace.py.

Both functions here are DELIBERATELY separate from services/acp_planning/
(the real N4-N6 pipeline) — they model something the real pipeline does not
implement, not a duplicate of it. See runway_months()'s own docstring.
"""
from __future__ import annotations

import re
from typing import Optional

WEEKS_PER_MONTH = 4  # same convention as the N6 allocator's weeks=[1,2,3,4] (allocator deleted in AA-840)

def runway_months(atom_count: int, posts_per_week: float) -> Optional[int]:
    """AA-330 Phần B, commercial-decision D1 (Nghiep, confirmed before build) — floor(atom_count
    / (posts_per_week * 4)).

    This is a DELIBERATE, CONSERVATIVE COMMERCIAL SIMULATION for the tenant-facing upsell number
    ("12 tour -> 87 atom -> ~7 tháng @3 bài/tuần"), NOT a measurement of a real technical limit.
    It assumes every atom backs exactly one never-repeated post. That assumption does NOT hold in
    services.acp_planning.allocator: `_eligible_atoms()` only excludes an atom for `deleted=true`
    (manual curation) or same-month/same-trip/same-channel reuse (`used_this_month`, a local set
    reset on every `compute_slot_grid()` call, never persisted across months) -- an atom can be
    picked again next month with nothing stopping it. `cooldown_until` is read by the allocator
    but has zero write sites anywhere in this repo (grep-confirmed, AA-330 Phần B STEP 0); the
    `ATOM_COOLDOWN_WEEKS = 6` constant in acp_planning/constants.py is defined but never imported
    anywhere either. So atoms do not actually deplete today.

    DO NOT "fix" this formula to match the real allocator's reuse behavior, no matter the
    reasoning -- Nghiep's explicit instruction (AA-330 Phần B build task) is to keep the
    conservative, table-matching model as the number shown to tenants. `WEEKS_PER_MONTH = 4`
    (not 4.33) is not invented here either -- it is the same tiling `allocator.py`'s own
    `compute_slot_grid()` uses for one month's slot grid.

    Verified against the issue's own example table (AA-330 Phần B STEP 0, 08/08/2026):
    87 atoms @ 3 posts/week -> floor(87/12) = 7 (issue: ~7 tháng); 180 @ 5/week -> floor(180/20)
    = 9 (issue: ~9 tháng); 360 @ 7/week -> floor(360/28) = 12 (issue: "12+ tháng").

    Returns None (not 0) for posts_per_week <= 0 -- a zero/negative cadence has no meaningful
    "months until depletion" under this model."""
    if posts_per_week <= 0:
        return None
    return int(atom_count // (posts_per_week * WEEKS_PER_MONTH))


# ── parse_price() ────────────────────────────────────────────────────────────
#
# FX table below: fixed-at-build-time rates, NOT a live rate feed (AA-330 Phần B
# commercial-decision D3, confirmed before build). Looked up 2026-08-08 from
# exchange-rates.org (https://www.exchange-rates.org/current-rates/usd, page
# timestamped "2026-8-8 3:00 AM UTC"), cross-checked against the Federal
# Reserve's official H.10 release (https://www.federalreserve.gov/releases/
# h10/current/, published 2026-08-03, rates for week ending 2026-07-31) --
# both sources agreed within ~2% on every currency below. These rates WILL
# drift out of date -- there is no update mechanism in this codebase yet.
# Flagged, not built here (see docs/implementation-notes/AA-330.md).
FX_RATES_TO_USD = {
    "USD": 1.0,
    "GBP": 1.3492,   # 1 GBP = 1/0.7412 USD
    "EUR": 1.1561,   # 1 EUR = 1/0.8650 USD
    "INR": 0.010511,  # 1 INR = 1/95.138 USD
    "JPY": 0.006337,  # 1 JPY = 1/157.80 USD
    "KRW": 0.0007103,  # 1 KRW = 1/1407.89 USD
    "THB": 0.030395,  # 1 THB = 1/32.900 USD
    "TWD": 0.031007,  # 1 TWD = 1/32.251 USD
}

_NUM = r"[\d][\d,]*(?:\.\d+)?"

# Ordered strongest-first: an explicit currency code/word beats a bare symbol
# ($/£/€/¥/₩) -- resolves real supplier noise like "$23,300 TWD" (bare $
# prefix but an explicit TWD code right after the number) to TWD, not USD.
_STRONG_CURRENCY_RE = {
    "INR": re.compile(r"INR", re.IGNORECASE),
    "GBP": re.compile(r"GBP", re.IGNORECASE),
    "EUR": re.compile(r"EUR", re.IGNORECASE),
    "JPY": re.compile(r"JPY|Japanese\s+Yen|yen", re.IGNORECASE),
    "KRW": re.compile(r"KRW", re.IGNORECASE),
    "THB": re.compile(r"THB|baht", re.IGNORECASE),
    "TWD": re.compile(r"TWD", re.IGNORECASE),
    "USD": re.compile(r"USD|US\$", re.IGNORECASE),
}
# Symbol fallback, only consulted when no strong marker matched anywhere in
# the text. Bare "$" defaults to USD -- AA's own supplier data uses "$" as a
# USD shorthand far more often than any other symbol currency in this set.
_SYMBOL_CURRENCY_RE = {
    "GBP": re.compile(r"£"),
    "EUR": re.compile(r"€"),
    "JPY": re.compile(r"[¥￥]"),  # half-width U+00A5 and full-width U+FFE5 both seen live
    "KRW": re.compile(r"₩"),
    "USD": re.compile(r"\$"),
}

# A currency marker directly before or after a number, in either order --
# covers both "$1,200"/"US$1,200"/"INR 1,850" (prefix) and "1200 USD"/
# "10,000 yen"/"₩1,450,000KRW" (suffix, no space required). Gap is same-line
# whitespace ONLY ([ \t], not \n) -- an earlier version used \s* here, which
# let a stray "$" several lines away from a real price (e.g. "usd.$" in a
# table header) reach across the newline and grab an unrelated row-index
# number ("1") from a totally different line as if it were the price. No
# real sample needs more than 1 space between marker and number.
_PREFIX_TMPL = r"(?:{marker})[ \t]{{0,3}}({num})"
_SUFFIX_TMPL = r"({num})[ \t]{{0,3}}(?:{marker})"

# AA-330 Phần B live-verify follow-up (08/08/2026) found parse_price() returning $1.27 for a real,
# CATALOG-VISIBLE row (tour_id a68ebb28-9cc2-454b-8747-2f92c3815c83, "Tomioka Silk Mill", price_raw
# "Adult: 200 yen, elementary/jr. high school student: 100 yen..." -- a single-attraction walk-in
# admission ticket, not a bookable tour package). Re-ran parse_price() over the FULL live catalog
# (441 price_available=true tours reachable through GET /catalog, i.e. joined through
# acp_contract.v_trip_registry) sorted ascending: $1.27 was the ONLY value below $10 in that
# catalog-visible set -- every other price, including genuinely cheap short activities (a 3-hour
# Delhi cycle tour at $19.45, a 2-day budget India trek at $10.50), starts at $10.50.
#
# A second same-shape case was also found (tour_id 72ab6e91-063b-4604-8d37-19f96dcb5a63, "Gunma
# Museum of Natural History", parses to $1.33) but is NOT catalog-visible today -- confirmed its
# raw_tours.src_itineraries is NULL, which fails v_trip_registry's own pre-existing floor (migration
# 083, unrelated to AA-330), so it never reaches GET /catalog regardless of this fix. Still fixed
# here (same bug class, and it would surface the moment that unrelated NULL gets backfilled) and
# still regression-tested, but it was never an actual live-displayed wrong price the way Tomioka
# Silk Mill was.
#
# $5 sits in the middle of the empirically-confirmed gap ($1.27/$1.33 -> $10.50) and separately
# matches the threshold suggested when this bug was reported -- both point to the same number, not
# picked in isolation.
MIN_PLAUSIBLE_PRICE_USD = 5.0


_TIER_LINE_RE = re.compile(
    r"\d+\s*[-+]\s*\d*\s*Traveller\s*:\s*([^\n]+)", re.IGNORECASE)
_ADULT_LINE_RE = re.compile(r"Adults?\s*\([^)]*\)\s*:?\s*([^\n]+)", re.IGNORECASE)
_CHILD_LINE_RE = re.compile(r"Children\s*\([^)]*\)\s*:?\s*([^\n]+)", re.IGNORECASE)

# A number immediately followed by "/hour"/"/hr"/"per hour" is a per-unit-time
# activity rate ("1,000 yen/hour"), not a comparable tour package price --
# STEP 0 (AA-330 Phần B, 08/08/2026) flagged this shape as a real distortion
# risk (picking it up as "the price" would misrepresent the tour's actual
# price band, not just be off by a few percent). Deliberately narrower than
# "/day" or "per person"/"per pax"/"pp" -- those DO legitimately qualify a
# real total price throughout the good samples (e.g. "$350 per person").
_RATE_SUFFIX_RE = re.compile(r"\A\s*/\s*(hour|hr)\b|\A\s*per\s+hour\b", re.IGNORECASE)


def _find_amount(text: str) -> Optional[tuple[float, str]]:
    """First currency-marker + number match in `text` (strong markers checked
    across the whole text before any symbol fallback), or None. Deliberately
    a FIRST-match, not a scan-everything-and-pick-something -- this is what
    correctly ignores a trailing different-unit number on the same field
    ("From $630\\n$79 per day" -> $630, not the per-day rate; "5950 EUR ...
    Single room supplement +1.000 EUR" -> 5950, not the supplement). A match
    immediately followed by an hourly-rate suffix is skipped entirely (see
    _RATE_SUFFIX_RE) rather than accepted -- that currency is treated as
    having no valid match in this text."""
    for currency, marker_re in _STRONG_CURRENCY_RE.items():
        m = marker_re.search(text)
        if not m:
            continue
        prefix_re = re.compile(_PREFIX_TMPL.format(marker=marker_re.pattern, num=_NUM), re.IGNORECASE)
        suffix_re = re.compile(_SUFFIX_TMPL.format(marker=marker_re.pattern, num=_NUM), re.IGNORECASE)
        pm = prefix_re.search(text)
        sm = suffix_re.search(text)
        cand = None
        if pm and sm:
            cand = pm if pm.start() <= sm.start() else sm
        else:
            cand = pm or sm
        if cand and not _RATE_SUFFIX_RE.match(text[cand.end():cand.end() + 20]):
            return float(cand.group(1).replace(",", "")), currency
    for currency, marker_re in _SYMBOL_CURRENCY_RE.items():
        prefix_re = re.compile(_PREFIX_TMPL.format(marker=marker_re.pattern, num=_NUM))
        m = prefix_re.search(text)
        if m and not _RATE_SUFFIX_RE.match(text[m.end():m.end() + 20]):
            return float(m.group(1).replace(",", "")), currency
    return None


def _tiered_min(text: str) -> Optional[tuple[float, str]]:
    """AA-330 Phần B, commercial-decision D4 (Nghiep, confirmed before build): for a genuine
    "N-M Traveller : $X" group-size tier table, always take the LOWEST tier price ("giá từ").
    Deliberately does NOT include non-tier lines in the same field (e.g. "Single Occupation: $X",
    "Single Supplement: $X", "Extra guide for N pax: $X") -- those are per-person supplements
    ADDED on top of a tier price, not alternate lower total prices; a real sample
    ("2-5 Traveller: $492 / 6-9: $292 / 10+: $244 / Single Occupation: $54") would otherwise
    report $54 as "the price", which is not a bookable total. If tier lines disagree on currency
    (never observed live, but would be unreliable data) returns None per D4's own escape hatch
    ("nếu không thể tin cậy xác định mức thấp nhất... coi là không parse được")."""
    tier_matches = _TIER_LINE_RE.findall(text)
    if not tier_matches:
        return None
    amounts = []
    for segment in tier_matches:
        hit = _find_amount(segment)
        if hit:
            amounts.append(hit)
    if not amounts:
        return None
    currencies = {c for _, c in amounts}
    if len(currencies) > 1:
        return None  # mixed currency within one tier table -- unreliable, don't guess
    return min(amounts, key=lambda x: x[0])


def _adult_child_min(text: str) -> Optional[tuple[float, str]]:
    """AA-330 Phần B, D4: "Adults (...): X currency\\nChildren (...): Y currency" -- both are
    genuine bookable per-person totals (unlike the tier table's supplement-line trap), so take
    the lower of the two directly."""
    adult_m = _ADULT_LINE_RE.search(text)
    child_m = _CHILD_LINE_RE.search(text)
    if not adult_m and not child_m:
        return None
    amounts = []
    if adult_m:
        hit = _find_amount(adult_m.group(1))
        if hit:
            amounts.append(hit)
    if child_m:
        hit = _find_amount(child_m.group(1))
        if hit:
            amounts.append(hit)
    if not amounts:
        return None
    currencies = {c for _, c in amounts}
    if len(currencies) > 1:
        return None
    return min(amounts, key=lambda x: x[0])


def parse_price(price_raw: Optional[str]) -> Optional[float]:
    """AA-330 Phần B, commercial-decisions D2/D3/D4 (Nghiep, confirmed before build).

    Returns a USD amount (converted via the fixed FX_RATES_TO_USD table above), or None when the
    text can't be reliably reduced to one representative price -- NEVER a fabricated number. A
    None return means the tenant-facing catalog shows "Giá: liên hệ" (D2) for that tour instead of
    silently hiding it.

    Order of attempts: (1) a genuine group-size tier table ("N-M Traveller: $X" lines) -> lowest
    tier (D4); (2) an Adults/Children split -> lower of the two (D4); (3) otherwise, the single
    FIRST currency+number found anywhere in the text (covers plain "$1,200"/"US$2,590"/
    "INR 1,850 per person"/"3999.00 USD"/"From¥35,000" and safely ignores a trailing different-
    unit number like a "$X per day" rate or a "Single room supplement +$X" add-on on the same
    field, since only the first match is taken).

    Returns None for: empty/whitespace text; "From $" with nothing after it; non-numeric text
    ("On request"); bare numbers with no currency marker at all ("89", "565", "4348" -- STEP 0
    found these ambiguous/possibly data-entry errors, not safe to assume a currency for); any text
    where no currency+number pair matches (complex unlabeled seasonal/date-range tables, per-hour/
    per-day-only activity pricing); and a converted result below MIN_PLAUSIBLE_PRICE_USD (a
    genuinely-parsed but implausibly tiny amount -- a walk-in museum/site admission fee, not a
    comparable tour price; see that constant's own comment for the live-data evidence) -- all per
    D2/D4's "don't guess, mark unavailable" instruction."""
    if not price_raw or not price_raw.strip():
        return None

    hit = _tiered_min(price_raw) or _adult_child_min(price_raw) or _find_amount(price_raw)
    if not hit:
        return None
    amount, currency = hit
    if amount <= 0:
        return None
    rate = FX_RATES_TO_USD.get(currency)
    if rate is None:
        return None
    usd = round(amount * rate, 2)
    if usd < MIN_PLAUSIBLE_PRICE_USD:
        return None
    return usd


__all__ = [
    "runway_months", "parse_price", "FX_RATES_TO_USD", "WEEKS_PER_MONTH",
    "MIN_PLAUSIBLE_PRICE_USD",
]
