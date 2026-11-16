"""AA-1116 (CIS 436) — the option B billing policies in the settings registry.

Every pricing policy value is a platform setting on Platform › Settings (areas "billing" and "tenant"; tech lead S279
and 16/11/2026: every price, threshold, limit and policy value is data an administrator edits, never a code literal).
Readers (the provision saga, the add-on request paths, shared/cost_guard.py, the quote service, the Plans and Tenant
360 routes) name a key through these constants and read the value in force with :func:`value` / :func:`avalue`
(``shared.settings``), never a copied number. The SQL readers (resolver 437, catalog views 438) read the same keys with
``shared.v_setting_billing_effective``. The keys equal the CIS 436 seed
(tests/integration/test_aa1116_migrations_435_439.py and the settings-lint reader check,
tests/unit/test_s279_settings_lint.py).
"""
from __future__ import annotations

from typing import Any

from shared import settings

# Trials (decision 3: every crm / tripplanner attach and approved add-on request starts trialing for the default
# length).
TRIAL_DAYS_DEFAULT = "cis.billing.trial_days_default"
TRIAL_DAYS_MAX = "cis.billing.trial_days_max"
# Decision 4: the share of each trialing subscription's own AI part (resolver 437 reads it in SQL).
TRIAL_AI_BUDGET_PCT = "cis.billing.trial_ai_budget_pct"
# Pilots (decision 8): a dated pilot is at most this many weeks; a pilot without an end date is always allowed.
PILOT_MAX_WEEKS = "cis.tenant.pilot_max_weeks"
PILOT_FREE = "cis.billing.pilot_free"
# The AI budget alert / stop of a tenant without its own AA-1016 row (and the starting values of a new row).
AI_ALERT_PCT_DEFAULT = "cis.billing.ai_alert_pct_default"
AI_APPROVAL_PCT_DEFAULT = "cis.billing.ai_approval_pct_default"
AI_HARD_STOP_DEFAULT = "cis.billing.ai_hard_stop_default"
# Commercial terms (the quote service; Booking drafts flag annual and discounts by hand, decision 7).
ANNUAL_PAID_MONTHS = "cis.billing.annual_paid_months"
FOUNDING_DISCOUNT_PCT = "cis.billing.founding_discount_pct"
FOUNDING_MONTHS = "cis.billing.founding_months"
FOUNDING_MAX_AGENCIES = "cis.billing.founding_max_agencies"
FOUNDING_ELIGIBLE_PLANS = "cis.billing.founding_eligible_plans"
COMBO_DISCOUNT_PCT = "cis.billing.combo_discount_pct"
COMBO_PRODUCTS = "cis.billing.combo_products"
FX_VND_PER_USD = "cis.billing.fx_vnd_per_usd"
VND_PRICE_REVIEW_MONTHS = "cis.billing.vnd_price_review_months"
PRICE_LOCK_MONTHS = "cis.billing.price_lock_months"
PRICE_REVIEW_MONTHS = "cis.billing.price_review_months"

ALL = (TRIAL_DAYS_DEFAULT, TRIAL_DAYS_MAX, TRIAL_AI_BUDGET_PCT, PILOT_MAX_WEEKS, PILOT_FREE, AI_ALERT_PCT_DEFAULT,
       AI_APPROVAL_PCT_DEFAULT, AI_HARD_STOP_DEFAULT, ANNUAL_PAID_MONTHS, FOUNDING_DISCOUNT_PCT, FOUNDING_MONTHS,
       FOUNDING_MAX_AGENCIES, FOUNDING_ELIGIBLE_PLANS, COMBO_DISCOUNT_PCT, COMBO_PRODUCTS, FX_VND_PER_USD,
       VND_PRICE_REVIEW_MONTHS, PRICE_LOCK_MONTHS, PRICE_REVIEW_MONTHS)

# The commercial keys the quote service and the Plans "Commercial terms" panel read, in display order.
COMMERCIAL = (ANNUAL_PAID_MONTHS, FOUNDING_DISCOUNT_PCT, FOUNDING_MONTHS, FOUNDING_MAX_AGENCIES,
              FOUNDING_ELIGIBLE_PLANS, COMBO_DISCOUNT_PCT, COMBO_PRODUCTS, FX_VND_PER_USD, VND_PRICE_REVIEW_MONTHS,
              PRICE_LOCK_MONTHS, PRICE_REVIEW_MONTHS, TRIAL_DAYS_DEFAULT, TRIAL_DAYS_MAX, TRIAL_AI_BUDGET_PCT,
              PILOT_FREE, PILOT_MAX_WEEKS)


def value(key: str) -> Any:
    """The platform value in force of ``key`` (sync; the registry's cache, else the compiled default)."""
    return settings.get(key)


async def avalue(key: str) -> Any:
    """The platform value in force of ``key`` (async)."""
    return await settings.aget(key)


def int_value(key: str) -> int:
    return int(settings.get(key))


async def aint_value(key: str) -> int:
    return int(await settings.aget(key))


def snapshot(keys: tuple[str, ...] = ALL) -> dict[str, Any]:
    """``{key: value in force}`` for ``keys`` (one read each; the quote service passes it around as a plain dict)."""
    return {k: settings.get(k) for k in keys}


def label(key: str) -> str:
    """The setting's plain label (Platform › Settings)."""
    return settings.definition(key).label
