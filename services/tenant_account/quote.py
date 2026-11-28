"""AA-1116 (option B) — the monthly software quote of a tenant, from data only. Pure: no DB, no clock.

Every number comes from its caller: the plan rows (Plans & add-ons: USD and VND list prices, unit, included seats, seat
minimum, extra-seat prices), the subscriptions (seats, interval, agreed price / currency / until), the tenant (billing
currency, founding-offer dates, pilot) and the commercial settings in force
(:mod:`services.tenant_account.pricing_settings`, Platform › Settings). Booking drafts the invoice; this quote is what
staff see on Tenant 360 and the agency sees on its Billing tab, so both read one rule.

Rules (S282 decisions):
  * a line is priced in the tenant's billing currency (USD, or VND from the plan's VND list); a plan with no price in
    that currency is a 0 line flagged ``price_missing``;
  * per-seat plans bill max(seats, seats_min) seats (decision 2); other plans bill the plan price plus the seats above
    ``seats_included`` at the extra-seat price;
  * seat overage on a per-seat plan (AA-1116, S284): the seats the plan includes (max(``seats_included``,
    ``seats_min``)) at the seat price, the seats above them (``extra_seats``) at the plan's extra-seat price in the
    billing currency, else at the seat price (so a plan with no extra-seat price bills exactly as before); with an
    agreed price in force every seat is at the agreed (locked) seat price. Booking drafts the same split as a
    ``subscription`` and an ``extra_seats`` line;
  * an agreed price in the billing currency replaces the list unit price while ``agreed_until`` is empty or not past.
    It is per billed unit, like the list price (CIS 435 column comment, contract tenant.entitlements.v1 1.9): on a
    per-seat plan the line is agreed x max(seats, seats_min), so a seat added during a price lock is billed at the
    locked seat price; on any other plan it replaces the plan fee and seats above ``seats_included`` stay at the
    extra-seat list price;
  * there is no retail add-on line (S282 decision 14: no custom-domain add-on, no AI pack);
  * founding offer: ``founding_discount_pct`` on the lines whose plan is eligible while today is inside the offer;
    combo: ``combo_discount_pct`` on every line when the tenant holds every product of ``combo_products``; they never
    stack, the larger discount applies (decision 6);
  * the annual figure is ``annual_paid_months`` x the monthly total (12 months for the price of 10 in option B);
  * a pilot is free while ``pilot_free`` is on (the totals are 0, the lines still show the list);
  * ``fx_vnd_per_usd`` gives a reference equivalent only (never used to invoice).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Optional

from services.tenant_account import currency as cur_
from services.tenant_account import pricing_settings as ps

LIVE = ("trialing", "active", "past_due")
PLACES = {"USD": Decimal("0.01"), "VND": Decimal("1")}


def _d(v: Any) -> Optional[Decimal]:
    return None if v is None else Decimal(str(v))


def _money(v: Decimal, currency: str) -> Decimal:
    return v.quantize(PLACES[currency], rounding=ROUND_HALF_UP)


def eligible(plan_key: str, patterns: Iterable[str]) -> bool:
    """``plan_key`` matches a pattern exactly or by product (``crm.*``)."""
    for p in patterns or ():
        if p == plan_key or (p.endswith(".*") and plan_key.split(".")[0] == p[:-2]):
            return True
    return False


@dataclass
class Line:
    kind: str                      # subscription
    product: str
    plan_key: str
    name: str
    quantity: int
    unit_price: Optional[Decimal]
    amount: Decimal
    agreed: bool = False
    price_missing: bool = False
    billing_interval: str = "month"
    trialing: bool = False
    discount: Decimal = Decimal(0)
    extra_seats: int = 0                      # AA-1116 S284: seats billed above the plan's included seats
    extra_seat_price: Optional[Decimal] = None

    def out(self, currency: str) -> dict:
        return {"kind": self.kind, "product": self.product, "plan_key": self.plan_key, "name": self.name,
                "quantity": self.quantity, "extra_seats": self.extra_seats,
                "extra_seat_price": None if self.extra_seat_price is None
                else str(_money(self.extra_seat_price, currency)),
                "unit_price": None if self.unit_price is None else str(_money(self.unit_price, currency)),
                "amount": str(_money(self.amount, currency)), "discount": str(_money(self.discount, currency)),
                "agreed": self.agreed, "price_missing": self.price_missing,
                "billing_interval": self.billing_interval, "trialing": self.trialing}


@dataclass
class Quote:
    currency: str
    lines: list[Line] = field(default_factory=list)
    discount_kind: Optional[str] = None          # founding | combo | None
    discount_pct: Decimal = Decimal(0)
    pilot_free: bool = False
    terms: dict = field(default_factory=dict)

    @property
    def subtotal(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal(0))

    @property
    def discount(self) -> Decimal:
        return sum((ln.discount for ln in self.lines), Decimal(0))

    @property
    def monthly_total(self) -> Decimal:
        return Decimal(0) if self.pilot_free else self.subtotal - self.discount

    def out(self) -> dict:
        c = self.currency
        months = int(self.terms.get(ps.ANNUAL_PAID_MONTHS) or 12)
        fx = _d(self.terms.get(ps.FX_VND_PER_USD))
        total = self.monthly_total
        ref_currency = "USD" if c == "VND" else "VND"
        if fx and fx > 0 and _is_enabled(ref_currency, self.terms):
            ref = total / fx if c == "VND" else total * fx
            reference = {"currency": ref_currency, "monthly_total": str(_money(ref, ref_currency)),
                         "fx_vnd_per_usd": str(fx)}
        else:
            reference = None
        return {"currency": c, "lines": [ln.out(c) for ln in self.lines],
                "subtotal": str(_money(self.subtotal, c)), "discount_kind": self.discount_kind,
                "discount_pct": str(self.discount_pct), "discount": str(_money(self.discount, c)),
                "pilot_free": self.pilot_free, "monthly_total": str(_money(total, c)),
                "annual_paid_months": months, "annual_total": str(_money(total * months, c)),
                "reference": reference}


def _unit_price(row: dict, currency: str, *, extra_seat: bool = False) -> Optional[Decimal]:
    if currency == "VND":
        return _d(row.get("price_extra_seat_vnd" if extra_seat else "price_vnd_monthly"))
    return _d(row.get("price_extra_seat_usd" if extra_seat else "price_usd_monthly"))


def _agreed(sub: dict, currency: str, today: date) -> Optional[Decimal]:
    price, cur, until = _d(sub.get("agreed_price_monthly")), sub.get("agreed_currency"), sub.get("agreed_until")
    if price is None or cur != currency:
        return None
    if until is not None:
        until = until if isinstance(until, date) else date.fromisoformat(str(until))
        if until < today:
            return None
    return price


def seat_split(seats: Any, seats_included: Any, seats_min: Any) -> tuple[int, int]:
    """(seats billed at the seat price, extra seats) of a per-seat plan. The billed total is max(seats, seats_min, 1)
    (no seats = the included ones); the plan includes max(seats_included, seats_min, 1) of them; the rest are extra.
    A plan with no ``seats_included`` has no extra seats (every seat at the seat price). Pure; Booking's
    ``app/agency_billing/drafts.py`` keeps the same rule."""
    total = max(int(seats or seats_included or 1), int(seats_min or 1), 1)
    if seats_included is None:
        return total, 0
    base = min(total, max(int(seats_included), int(seats_min or 1), 1))
    return base, total - base


def subscription_line(sub: dict, currency: str, today: date) -> Line:
    """One subscription's line. An agreed price in force replaces the list UNIT price (per seat on a per-seat plan)."""
    agreed = _agreed(sub, currency, today)
    unit = agreed if agreed is not None else _unit_price(sub, currency)
    extra_seats, extra_price = 0, None
    if sub.get("price_unit") == "seat":
        base, extra_seats = seat_split(sub.get("seats"), sub.get("seats_included"), sub.get("seats_min"))
        qty = base + extra_seats
        if extra_seats:
            # the locked seat price covers seats added during the lock; else the plan's extra-seat price, else the seat
            listed = _unit_price(sub, currency, extra_seat=True)
            extra_price = agreed if agreed is not None else (listed if listed is not None else unit)
        amount = (unit or Decimal(0)) * base + (extra_price or Decimal(0)) * extra_seats
    else:
        qty = 1
        amount = unit or Decimal(0)
        included, seats = sub.get("seats_included"), sub.get("seats")
        extra = _unit_price(sub, currency, extra_seat=True)
        if extra is not None and included is not None and seats is not None and seats > included:
            extra_seats, extra_price = seats - included, extra
            amount += extra * extra_seats
    return Line("subscription", sub["product"], sub["plan_key"], sub.get("plan_name") or sub["plan_key"], qty, unit,
                amount, agreed=agreed is not None, price_missing=unit is None,
                billing_interval=sub.get("billing_interval") or "month", trialing=sub.get("status") == "trialing",
                extra_seats=extra_seats, extra_seat_price=extra_price)


def _in_offer(tenant: dict, today: date) -> bool:
    start, end = tenant.get("founding_offer_from"), tenant.get("founding_offer_until")
    if start is None or end is None:
        return False
    start = start if isinstance(start, date) else date.fromisoformat(str(start))
    end = end if isinstance(end, date) else date.fromisoformat(str(end))
    return start <= today <= end


def _is_enabled(code: str, terms: dict) -> bool:
    """AA-1191: ``code`` is an enabled currency (``terms`` carries cis.billing.enabled_currencies; a caller that does
    not pass it, a unit test, keeps every currency)."""
    allowed = terms.get(cur_.ENABLED_CURRENCIES)
    return allowed is None or code in cur_.normalise(allowed)


def build(tenant: dict, subscriptions: list[dict], terms: dict, *, today: date) -> Quote:
    """The quote of the live subscriptions. ``terms`` = :func:`pricing_settings.snapshot` (plus, AA-1191, the enabled
    currencies: a billing currency that is not enabled quotes in USD, and the reference equivalent is shown only in an
    enabled currency)."""
    currency = tenant.get("billing_currency") or "USD"
    if not _is_enabled(currency, terms):
        currency = cur_.BASE
    q = Quote(currency=currency, terms=terms)
    live_subs = [s for s in subscriptions if s.get("status") in LIVE]
    q.lines = [subscription_line(s, currency, today) for s in live_subs]
    q.pilot_free = bool(tenant.get("is_pilot")) and bool(terms.get(ps.PILOT_FREE))
    # discounts: founding on eligible lines, combo on every line when every combo product is live; the larger wins
    founding_pct = _d(terms.get(ps.FOUNDING_DISCOUNT_PCT)) or Decimal(0)
    combo_pct = _d(terms.get(ps.COMBO_DISCOUNT_PCT)) or Decimal(0)
    patterns = terms.get(ps.FOUNDING_ELIGIBLE_PLANS) or []
    founding = Decimal(0)
    if _in_offer(tenant, today) and founding_pct > 0:
        founding = sum((ln.amount * founding_pct / 100 for ln in q.lines if eligible(ln.plan_key, patterns)),
                       Decimal(0))
    held = {s["product"] for s in live_subs}
    combo_products = set(terms.get(ps.COMBO_PRODUCTS) or [])
    combo = q.subtotal * combo_pct / 100 if combo_products and combo_products <= held and combo_pct > 0 \
        else Decimal(0)
    if founding <= 0 and combo <= 0:
        return q
    if combo > founding:
        q.discount_kind, q.discount_pct = "combo", combo_pct
        for ln in q.lines:
            ln.discount = ln.amount * combo_pct / 100
    else:
        q.discount_kind, q.discount_pct = "founding", founding_pct
        for ln in q.lines:
            if eligible(ln.plan_key, patterns):
                ln.discount = ln.amount * founding_pct / 100
    return q
