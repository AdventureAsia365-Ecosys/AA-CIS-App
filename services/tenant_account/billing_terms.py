"""AA-1116 (option B) — a tenant's billing details, the commercial terms of its subscriptions and its quote.

Tenant 360 › Billing details (admin, ``tenant.write``; audited ``tenant.billing_details_set``): the bill-to fields CIS
holds (legal entity, tax id, billing e-mail / address / country; until now no page edited them), the billing currency
(USD | VND, CIS 435) and the founding-agency offer (from / until; at most ``cis.billing.founding_max_agencies``
agencies, only for a tenant with a live subscription on an eligible plan).
Tenant 360 › Subscription terms (audited ``tenant.subscription_terms_set``): billing interval (month | year), an agreed
monthly price in a currency with an optional end (a contract price, or ``lock`` = the list price now, locked for
``cis.billing.price_lock_months``), refused under the plan's ``price_floor_usd_monthly`` (the Enterprise floor).
The quote (:mod:`services.tenant_account.quote`) reads the same rows.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.tenant_account import pricing_settings as ps
from services.tenant_account import quote as qt

CURRENCIES = ("USD", "VND")
INTERVALS = ("month", "year")
# technical-constant: the CIS 354 / 435 CHECK lengths of the bill-to fields (the database refuses longer)
TEXT_LIMITS = {"legal_entity_name": 300, "tax_id": 64, "billing_email": 254, "billing_address": 1000}
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
AGREED_MAX = Decimal("999999999999.99")         # numeric(14,2)
DETAIL_FIELDS = ("legal_entity_name", "tax_id", "billing_email", "billing_address", "billing_country",
                 "billing_currency", "founding_offer_from", "founding_offer_until")


class TermsError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422, field: Optional[str] = None):
        super().__init__(code, message, status, field)
        self.code, self.message, self.status, self.field = code, message, status, field


TENANT_SQL = """
    SELECT tenant_id::text AS tenant_id, name::text AS name, kind, is_pilot, legal_entity_name, tax_id, billing_email,
           billing_address, billing_country, billing_currency::text AS billing_currency, founding_offer_from,
           founding_offer_until
      FROM shared.tenants WHERE tenant_id = $1::uuid
"""
SUBS_SQL = """
    SELECT s.product, s.plan_key, s.status, s.seats, s.billing_interval, s.agreed_price_monthly,
           s.agreed_currency::text AS agreed_currency, s.agreed_until, s.trial_ends_at, p.name AS plan_name,
           p.price_unit, p.price_usd_monthly, p.price_vnd_monthly, p.seats_included, p.seats_min,
           p.price_extra_seat_usd, p.price_extra_seat_vnd, p.price_floor_usd_monthly
      FROM shared.tenant_subscription s
      JOIN shared.plan p ON p.plan_key = s.plan_key
     WHERE s.tenant_id = $1::uuid
     ORDER BY CASE s.product WHEN 'content' THEN 0 WHEN 'crm' THEN 1 ELSE 2 END
"""
ADDONS_SQL = """
    SELECT a.plan_key, p.name AS plan_name, a.quantity, a.status, p.price_usd_monthly, p.price_vnd_monthly,
           (a.status IN ('trialing', 'active') AND a.starts_at <= now()
            AND (a.ends_at IS NULL OR a.ends_at > now())) AS live
      FROM shared.tenant_addon a JOIN shared.plan p ON p.plan_key = a.plan_key
     WHERE a.tenant_id = $1::uuid AND a.status <> 'canceled'
"""
FOUNDING_COUNT_SQL = """
    SELECT count(*) FROM shared.tenants WHERE founding_offer_from IS NOT NULL AND tenant_id <> $1::uuid
"""
LOCK_TENANT_SQL = "SELECT 1 FROM shared.tenants WHERE tenant_id = $1::uuid FOR UPDATE"
FOUNDING_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('cis.billing.founding_offer', 0))"
UPDATE_DETAILS_SQL = """
    UPDATE shared.tenants
       SET legal_entity_name = $2, tax_id = $3, billing_email = $4, billing_address = $5, billing_country = $6,
           billing_currency = $7, founding_offer_from = $8, founding_offer_until = $9, updated_at = now()
     WHERE tenant_id = $1::uuid
"""
SUB_LOCK_SQL = """
    SELECT s.product, s.plan_key, s.status, s.billing_interval, s.agreed_price_monthly,
           s.agreed_currency::text AS agreed_currency, s.agreed_until, p.price_floor_usd_monthly,
           p.price_usd_monthly, p.price_vnd_monthly, p.price_unit, p.seats_included, p.seats_min, s.seats,
           p.price_extra_seat_usd, p.price_extra_seat_vnd
      FROM shared.tenant_subscription s JOIN shared.plan p ON p.plan_key = s.plan_key
     WHERE s.tenant_id = $1::uuid AND s.product = $2 FOR UPDATE OF s
"""
UPDATE_TERMS_SQL = """
    UPDATE shared.tenant_subscription
       SET billing_interval = $3, agreed_price_monthly = $4, agreed_currency = $5, agreed_until = $6, updated_by = $7
     WHERE tenant_id = $1::uuid AND product = $2
"""


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date) else (str(v) if isinstance(v, Decimal) else v)


def _date(v: Any, what: str) -> Optional[date]:
    if v is None or isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise TermsError("invalid_date", f"{what} is a date (YYYY-MM-DD)", field=what) from None


def _months_after(start: date, months: int) -> date:
    """The last day of ``months`` months that start on ``start`` (12 from 1 March: the last day of February)."""
    y, m = divmod(start.month - 1 + months, 12)
    year, month = start.year + y, m + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day) - timedelta(days=1)


async def details(conn, tenant_id: str) -> Optional[dict]:
    row = await conn.fetchrow(TENANT_SQL, tenant_id)
    return None if row is None else {k: _iso(v) for k, v in dict(row).items()}


async def tenant_quote(conn, tenant_id: str, *, today: Optional[date] = None) -> Optional[dict]:
    """The tenant's quote (see :mod:`quote`) with its subscriptions' terms; None when there is no such tenant."""
    tenant = await conn.fetchrow(TENANT_SQL, tenant_id)
    if tenant is None:
        return None
    subs = [dict(r) for r in await conn.fetch(SUBS_SQL, tenant_id)]
    addons = [dict(r) for r in await conn.fetch(ADDONS_SQL, tenant_id)]
    terms = ps.snapshot(ps.COMMERCIAL)
    q = qt.build(dict(tenant), subs, addons, terms, today=today or date.today()).out()
    q["subscriptions"] = [{"product": s["product"], "plan_key": s["plan_key"], "status": s["status"],
                           "billing_interval": s["billing_interval"],
                           "agreed_price_monthly": _iso(s["agreed_price_monthly"]),
                           "agreed_currency": s["agreed_currency"], "agreed_until": _iso(s["agreed_until"]),
                           "price_floor_usd_monthly": _iso(s["price_floor_usd_monthly"])} for s in subs]
    return q


def _clean_text(name: str, v: Any) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        raise TermsError("invalid_text", f"{name} is text", field=name)
    v = v.strip()
    if not v:
        return None
    if len(v) > TEXT_LIMITS[name]:
        raise TermsError("too_long", f"{name}: at most {TEXT_LIMITS[name]} characters", field=name)
    if name == "billing_email" and not re.match(r"^[^@\s]+@[^@\s]+$", v):
        raise TermsError("invalid_email", "billing_email is an e-mail address", field=name)
    return v


async def _audit(conn, tenant_id: str, actor: str, action: str, details_: dict) -> None:
    await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=action, resource_type="tenant",
                          resource_id=tenant_id, details=json.loads(json.dumps(details_, default=str)))


async def set_details(conn, tenant_id: str, payload: dict, *, actor: str) -> dict:
    """Replace the fields ``payload`` names (others keep their value). Founding: ``founding_offer_from`` set and
    ``founding_offer_until`` left out = from + cis.billing.founding_months; ``founding_offer_from`` null clears both."""
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor[:64])
        if await conn.fetchval(LOCK_TENANT_SQL, tenant_id) is None:
            raise TermsError("tenant_not_found", "no tenant with this id", 404)
        before = dict(await conn.fetchrow(TENANT_SQL, tenant_id))
        after = {k: before[k] for k in DETAIL_FIELDS}
        for k in TEXT_LIMITS:
            if k in payload:
                after[k] = _clean_text(k, payload[k])
        if "billing_country" in payload:
            c = payload["billing_country"]
            if c is not None and (not isinstance(c, str) or not COUNTRY_RE.match(c.strip().upper())):
                raise TermsError("invalid_country", "billing_country is a two-letter ISO code", field="billing_country")
            after["billing_country"] = None if c is None else c.strip().upper()
        if "billing_currency" in payload:
            if payload["billing_currency"] not in CURRENCIES:
                raise TermsError("invalid_currency", "billing_currency: USD or VND", field="billing_currency")
            after["billing_currency"] = payload["billing_currency"]
        if "founding_offer_from" in payload or "founding_offer_until" in payload:
            await _founding(conn, tenant_id, payload, after)
        await conn.execute(UPDATE_DETAILS_SQL, tenant_id, *(after[k] for k in DETAIL_FIELDS))
        changed = sorted(k for k in DETAIL_FIELDS if before[k] != after[k])
        if changed:
            await _audit(conn, tenant_id, actor, AdminTenantAuditAction.BILLING_DETAILS_SET,
                         {"fields": changed, "before": {k: before[k] for k in changed},
                          "after": {k: after[k] for k in changed}})
    return {**(await details(conn, tenant_id)), "changed": changed}


async def _founding(conn, tenant_id: str, payload: dict, after: dict) -> None:
    start = _date(payload.get("founding_offer_from", after["founding_offer_from"]), "founding_offer_from")
    if start is None:
        after["founding_offer_from"] = after["founding_offer_until"] = None
        return
    end = _date(payload.get("founding_offer_until"), "founding_offer_until") or \
        _months_after(start, ps.int_value(ps.FOUNDING_MONTHS))
    if end < start:
        raise TermsError("invalid_date", "founding_offer_until is on or after founding_offer_from",
                         field="founding_offer_until")
    if after["founding_offer_from"] is None:          # a new founding agency: the cap and the eligible plans
        await conn.execute(FOUNDING_LOCK_SQL)
        if int(await conn.fetchval(FOUNDING_COUNT_SQL, tenant_id)) >= ps.int_value(ps.FOUNDING_MAX_AGENCIES):
            raise TermsError("founding_offer_full", "every founding-agency place is taken", 409)
        patterns = ps.value(ps.FOUNDING_ELIGIBLE_PLANS) or []
        live = [r for r in await conn.fetch(SUBS_SQL, tenant_id) if r["status"] in qt.LIVE]
        if not any(qt.eligible(r["plan_key"], patterns) for r in live):
            raise TermsError("founding_not_eligible", "the tenant holds no plan the founding offer covers", 409)
    after["founding_offer_from"], after["founding_offer_until"] = start, end


def _agreed_price(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise TermsError("invalid_price", "agreed_price_monthly is a number", field="agreed_price_monthly") from None
    if isinstance(v, bool) or not d.is_finite() or d < 0 or d > AGREED_MAX or d != d.quantize(Decimal("0.01")):
        raise TermsError("invalid_price", "agreed_price_monthly: 0 or more, at most 2 decimals",
                         field="agreed_price_monthly")
    return d


async def set_terms(conn, tenant_id: str, product: str, payload: dict, *, actor: str,
                    today: Optional[date] = None) -> dict:
    """Billing interval / agreed price / price lock of one subscription. ``lock: true`` = the list price in the tenant's
    currency now, until today + cis.billing.price_lock_months."""
    today = today or date.today()
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor[:64])
        sub = await conn.fetchrow(SUB_LOCK_SQL, tenant_id, product)
        if sub is None:
            raise TermsError("subscription_not_found", f"the tenant has no {product} subscription", 404)
        currency = await conn.fetchval("SELECT billing_currency::text FROM shared.tenants WHERE tenant_id = $1::uuid",
                                       tenant_id)
        interval = payload.get("billing_interval", sub["billing_interval"])
        if interval not in INTERVALS:
            raise TermsError("invalid_interval", "billing_interval: month or year", field="billing_interval")
        price, cur, until = sub["agreed_price_monthly"], sub["agreed_currency"], sub["agreed_until"]
        if payload.get("lock"):
            line = qt.subscription_line({**dict(sub), "agreed_price_monthly": None}, currency, today)
            if line.price_missing:
                raise TermsError("price_missing", f"the plan has no {currency} price to lock", 409)
            months = ps.int_value(ps.PRICE_LOCK_MONTHS)
            price, cur, until = line.amount, currency, (_months_after(today, months) if months else None)
        elif "agreed_price_monthly" in payload:
            price = _agreed_price(payload["agreed_price_monthly"])
            cur = payload.get("agreed_currency", cur or currency) if price is not None else None
            until = _date(payload.get("agreed_until"), "agreed_until") if price is not None else None
        elif "agreed_until" in payload and price is not None:
            until = _date(payload["agreed_until"], "agreed_until")
        if price is not None and cur not in CURRENCIES:
            raise TermsError("invalid_currency", "agreed_currency: USD or VND", field="agreed_currency")
        floor = sub["price_floor_usd_monthly"]
        if price is not None and floor is not None:
            fx = Decimal(str(ps.value(ps.FX_VND_PER_USD)))
            in_usd = price if cur == "USD" else price / fx
            if in_usd < floor:
                raise TermsError("below_price_floor", f"the agreed price is under the plan's floor of {floor} USD "
                                 "a month", field="agreed_price_monthly")
        await conn.execute(UPDATE_TERMS_SQL, tenant_id, product, interval, price, cur, until, actor[:64])
        before = {"billing_interval": sub["billing_interval"], "agreed_price_monthly": sub["agreed_price_monthly"],
                  "agreed_currency": sub["agreed_currency"], "agreed_until": sub["agreed_until"]}
        after = {"billing_interval": interval, "agreed_price_monthly": price, "agreed_currency": cur,
                 "agreed_until": until}
        if before != after:
            await _audit(conn, tenant_id, actor, AdminTenantAuditAction.SUBSCRIPTION_TERMS_SET,
                         {"product": product, "plan_key": sub["plan_key"], "before": before, "after": after})
    return {"product": product, **{k: _iso(v) for k, v in after.items()}}
