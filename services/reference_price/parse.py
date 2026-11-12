"""services.reference_price.parse — AA-1091 item 5: the free-text source price of a tour (raw_tours.price_raw) as a
structured REFERENCE price (amount, currency, basis) plus the doubts a person should look at.

Root ADR 0005 d7: "an existing free-text price may be parsed once into a reference price with a review flag". The
result is never a selling price. Pure functions, no I/O; the store (services/reference_price/store.py) saves a parse with
origin 'parsed' and ``needs_review`` = bool(reasons), and staff confirm or correct it on the admin Reference prices page.

The amount rule is the one AA-Booking's ``parse_reference`` uses (backend/app/catalog/trip_plan_sync.py, the first
amount of the text with its currency), so both sides read "$1,722/person" the same way; this parser also finds the
currency next to the number, the basis (per person / per group / from) and the reasons for a review:

  no_text             blank text
  no_amount           no number at all ("On request")
  no_currency         a number but no currency next to it or anywhere ("1450")
  several_amounts     more than one different price in the text (adult / child, supplements, seasons)
  several_currencies  prices in more than one currency
  tiered              the price depends on the group size ("2-3 pax: ...", "per person for 4 travellers")
  basis_assumed       no basis word: per person is assumed
  basis_unclear       both a per-person and a per-group word
  amount_small        an amount below 5 in its currency unit: probably not a tour price (a ticket, a fee)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

BASES: tuple[str, ...] = ("per_person", "per_group", "from")   # CIS 401 tour_reference_price_basis_check
MAX_AMOUNT = Decimal("9999999999.99")                         # numeric(12,2)
MIN_PLAUSIBLE = Decimal("5")
RAW_MAX = 2000                                                # CIS 401 price_raw_seen length

# ISO 4217 codes seen in AA supplier sheets (and the region's), upper case. A three-letter word that is not here is not
# a currency ("PAX", "DAY").
CODES = frozenset({
    "USD", "EUR", "GBP", "AUD", "NZD", "CAD", "SGD", "CHF", "JPY", "CNY", "HKD", "TWD", "KRW", "INR", "NPR", "BTN",
    "LKR", "MVR", "THB", "VND", "LAK", "KHR", "MMK", "MYR", "IDR", "PHP", "BND", "MNT", "AED",
})
# Symbols and words, longest first (US$ before $). A bare "$" is USD (AA's supplier sheets use it for USD).
SYMBOLS: tuple[tuple[str, str], ...] = (
    ("US$", "USD"), ("NZ$", "NZD"), ("A$", "AUD"), ("S$", "SGD"), ("C$", "CAD"), ("HK$", "HKD"), ("NT$", "TWD"),
    ("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("฿", "THB"), ("₫", "VND"), ("₹", "INR"), ("₩", "KRW"), ("¥", "JPY"),
    ("￥", "JPY"), ("₭", "LAK"), ("៛", "KHR"),
)
WORDS: tuple[tuple[str, str], ...] = (
    ("dollars", "USD"), ("dollar", "USD"), ("euros", "EUR"), ("euro", "EUR"), ("pounds", "GBP"), ("yen", "JPY"),
    ("baht", "THB"), ("dong", "VND"), ("rupees", "INR"), ("rupee", "INR"), ("ringgit", "MYR"), ("kip", "LAK"),
)

_NUM = r"\d[\d.,]*\d|\d"
_ALPHA_ONLY = r"(?<![A-Za-z]){}(?![A-Za-z])"       # "USD1450" and "1450USD" have no \b between code and digits
_MARK = "|".join([re.escape(s) for s, _ in SYMBOLS] + [_ALPHA_ONLY.format(w) for w, _ in WORDS]
                 + [_ALPHA_ONLY.format("[A-Za-z]{3}")])
_PREFIX_RE = re.compile(rf"({_MARK})[ \t]{{0,3}}({_NUM})", re.IGNORECASE)
_SUFFIX_RE = re.compile(rf"({_NUM})[ \t]{{0,3}}({_MARK})", re.IGNORECASE)
_NUMBER_RE = re.compile(_NUM)

_FROM_RE = re.compile(r"^\W*(from|starting(\s+(from|at))?|start\s+from|as\s+low\s+as|price\s+from)\b|\bfrom\s*[$€£¥]",
                      re.IGNORECASE)
_GROUP_RE = re.compile(r"\bper\s+(group|vehicle|car|van|boat|jeep|room|party|booking|tour)\b|/\s*(group|vehicle|car|"
                       r"boat|tour)\b|\b(private\s+group|whole\s+group|for\s+the\s+group|group\s+price)\b",
                       re.IGNORECASE)
_PERSON_RE = re.compile(r"\bper\s+(person|pax|adult|traveller|traveler|head|guest|participant|people)\b|/\s*(person|"
                        r"pax|adult|traveller|traveler|guest|pp)\b|\bp\.?\s?p\.?(?=\W|$)|\bpps\b|\beach\b|"
                        r"\bper\s+tourist\b", re.IGNORECASE)
_TIER_RE = re.compile(r"\b\d+\s*(?:-|–|to)\s*\d+\s*(pax|people|persons|travell?ers|guests|participants)\b|"
                      r"\b(group\s+of|for)\s+\d+\s*(pax|people|persons|travell?ers|guests)\b|"
                      r"\b\d+\s*\+?\s*(pax|people|persons|travell?ers|guests)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Parsed:
    """One parse. ``amount`` / ``currency`` / ``basis`` are all set or all None."""
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    basis: Optional[str] = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def found(self) -> bool:
        return self.amount is not None

    @property
    def needs_review(self) -> bool:
        return bool(self.reasons)

    def as_dict(self) -> dict:
        return {"amount": None if self.amount is None else str(self.amount), "currency": self.currency,
                "basis": self.basis, "reasons": list(self.reasons), "needs_review": self.needs_review}


def number(token: str) -> Optional[Decimal]:
    """A price token as a Decimal: "1,722" / "1.722" (thousands) / "1,722.50" / "1.722,50" / "495". None when it is not
    a number. Same reading as AA-Booking ``trip_plan_sync._number``."""
    token = token.strip()
    if "," in token and "." in token:
        decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        token = token.replace(thousands, "").replace(decimal_sep, ".")
    elif "," in token:
        parts = token.split(",")
        token = token.replace(",", "") if all(len(p) == 3 for p in parts[1:]) else token.replace(",", ".")
    elif token.count(".") > 1 or (token.count(".") == 1 and len(token.split(".")[1]) == 3):
        token = token.replace(".", "")
    try:
        value = Decimal(token)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def currency_of(marker: str) -> Optional[str]:
    """The ISO code a currency marker names (a code, a symbol or a word), or None."""
    m = marker.strip()
    if m.upper() in CODES:
        return m.upper()
    for sym, code in SYMBOLS:
        if m.upper() == sym.upper():
            return code
    for word, code in WORDS:
        if m.lower() == word:
            return code
    return None


def _pairs(text: str) -> list[tuple[int, Decimal, str]]:
    """(position, amount, currency) of every number with a currency marker right before or after it."""
    out: dict[int, tuple[int, Decimal, str]] = {}
    for rx, mark_i, num_i in ((_PREFIX_RE, 1, 2), (_SUFFIX_RE, 2, 1)):
        for m in rx.finditer(text):
            cur = currency_of(m.group(mark_i))
            amount = number(m.group(num_i))
            if cur is None or amount is None:
                continue
            pos = m.start(num_i)
            out.setdefault(pos, (pos, amount, cur))
    return sorted(out.values())


def basis_of(text: str) -> tuple[str, tuple[str, ...]]:
    """(basis, reasons) from the words of the text: "from" first, then per group, then per person; none = per person,
    assumed."""
    group = bool(_GROUP_RE.search(text))
    person = bool(_PERSON_RE.search(text))
    if _FROM_RE.search(text):
        return "from", (("basis_unclear",) if group and not person else ())
    if group and person:
        return "per_group", ("basis_unclear",)
    if group:
        return "per_group", ()
    if person:
        return "per_person", ()
    return "per_person", ("basis_assumed",)


def clean_text(raw: Optional[str]) -> Optional[str]:
    """The source text with whitespace runs collapsed, or None when blank."""
    text = " ".join((raw or "").split())
    return text or None


def parse(raw: Optional[str]) -> Parsed:
    """The reference price a free-text source price names, with the reasons a person should review it. Never invents a
    currency: a number with no currency anywhere is ``no_currency`` (nothing found)."""
    text = clean_text(raw)
    if text is None:
        return Parsed(reasons=("no_text",))
    pairs = _pairs(text)
    if not pairs:
        if not _NUMBER_RE.search(text):
            return Parsed(reasons=("no_amount",))
        # a lone currency code elsewhere in the text ("Price: 1,450 (all prices in USD)")
        codes = [c for c in (currency_of(w) for w in re.findall(_ALPHA_ONLY.format("[A-Za-z]{3}"), text)) if c]
        amount = number(_NUMBER_RE.search(text).group(0))
        if len(set(codes)) != 1 or amount is None:
            return Parsed(reasons=("no_currency",))
        pairs = [(0, amount, codes[0])]
    _, amount, currency = pairs[0]
    if amount <= 0 or amount > MAX_AMOUNT:
        return Parsed(reasons=("no_amount",))
    reasons: list[str] = []
    if len({c for _, _, c in pairs}) > 1:
        reasons.append("several_currencies")
    if len({a for _, a, _ in pairs}) > 1:
        reasons.append("several_amounts")
    if _TIER_RE.search(text):
        reasons.append("tiered")
    basis, basis_reasons = basis_of(text)
    reasons.extend(basis_reasons)
    if amount < MIN_PLAUSIBLE:
        reasons.append("amount_small")
    return Parsed(amount=amount.quantize(Decimal("0.01")), currency=currency, basis=basis, reasons=tuple(reasons))
