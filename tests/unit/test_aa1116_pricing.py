"""AA-1116 (option B pricing) — the pure parts: the quote, the plan draft's new fields, the review badges, the billed
seats and the month arithmetic of the founding offer / price lock. No DB. Invented plan names only."""
from datetime import date
from decimal import Decimal

import pytest

from services.tenant_account import billing_terms as bt
from services.tenant_account import plans as pl
from services.tenant_account import pricing_settings as ps
from services.tenant_account import provision as pv
from services.tenant_account import quote as qt

TODAY = date(2027, 6, 15)
TERMS = {ps.ANNUAL_PAID_MONTHS: 10, ps.FOUNDING_DISCOUNT_PCT: 30, ps.FOUNDING_MONTHS: 12,
         ps.FOUNDING_ELIGIBLE_PLANS: ["content.growth", "content.business", "crm.*", "tripplanner.*", "addon.*"],
         ps.COMBO_DISCOUNT_PCT: 10, ps.COMBO_PRODUCTS: ["content", "crm", "tripplanner"], ps.FX_VND_PER_USD: 26000,
         ps.PILOT_FREE: True}
GROWTH = {"product": "content", "plan_key": "content.growth", "plan_name": "Growth", "status": "active",
          "price_unit": "tenant", "price_usd_monthly": "349", "price_vnd_monthly": "8990000"}
CRM = {"product": "crm", "plan_key": "crm.core", "plan_name": "CRM Core", "status": "active", "price_unit": "seat",
       "price_usd_monthly": "19", "price_vnd_monthly": "490000", "seats": 1, "seats_included": 3, "seats_min": 3}
TP = {"product": "tripplanner", "plan_key": "tripplanner.hosted", "plan_name": "Hosted", "status": "active",
      "price_unit": "site", "price_usd_monthly": "49", "price_vnd_monthly": "1250000"}
STARTER = {**GROWTH, "plan_key": "content.starter", "plan_name": "Starter", "price_usd_monthly": "149"}
PACK = {"plan_key": "addon.ai_pack", "plan_name": "AI pack", "quantity": 2, "status": "active",
        "price_usd_monthly": "19", "price_vnd_monthly": "490000", "live": True}


def _q(tenant=None, subs=(GROWTH,), addons=()):
    return qt.build({"billing_currency": "USD", **(tenant or {})}, list(subs), list(addons), TERMS, today=TODAY).out()


def test_combo_on_three_products():
    out = _q(subs=(GROWTH, CRM, TP))
    assert out["subtotal"] == "455.00"                       # 349 + 3 x 19 (seat minimum) + 49
    assert (out["discount_kind"], out["monthly_total"]) == ("combo", "409.50")
    assert out["annual_total"] == "4095.00"                  # 10 of 12 months


def test_founding_growth_and_starter_excluded():
    founding = {"founding_offer_from": "2027-03-01", "founding_offer_until": "2028-02-29"}
    assert _q(founding)["monthly_total"] == "244.30"
    assert _q(founding, subs=(STARTER,))["monthly_total"] == "149.00"
    assert _q({"founding_offer_from": "2026-01-01", "founding_offer_until": "2026-12-31"})["monthly_total"] == "349.00"


def test_founding_and_combo_do_not_stack():
    founding = {"founding_offer_from": "2027-03-01", "founding_offer_until": "2028-02-29"}
    out = _q(founding, subs=(GROWTH, CRM, TP))
    assert out["discount_kind"] == "founding" and out["monthly_total"] == "318.50"     # 455 x 0.7 > 455 x 0.9


def test_vnd_tenant_uses_the_vnd_list_and_a_usd_reference():
    out = _q({"billing_currency": "VND"}, subs=(STARTER,))
    assert out["currency"] == "VND" and out["monthly_total"] == "8990000"
    assert out["reference"]["currency"] == "USD" and out["reference"]["monthly_total"] == "345.77"


def test_ai_pack_times_quantity_and_a_missing_price():
    out = _q(addons=(PACK,))
    assert out["lines"][1]["amount"] == "38.00"
    out = _q(subs=({**TP, "price_usd_monthly": None},))
    assert out["lines"][0]["price_missing"] is True and out["monthly_total"] == "0.00"


def test_agreed_price_inside_and_after_the_lock():
    locked = {**GROWTH, "agreed_price_monthly": "300", "agreed_currency": "USD", "agreed_until": "2027-12-31"}
    assert _q(subs=(locked,))["monthly_total"] == "300.00"
    ended = {**locked, "agreed_until": "2027-01-31"}
    assert _q(subs=(ended,))["monthly_total"] == "349.00"
    other_currency = {**locked, "agreed_currency": "VND"}
    assert _q(subs=(other_currency,))["monthly_total"] == "349.00"


def test_pilot_is_free_and_trials_are_listed():
    out = _q({"is_pilot": True}, subs=({**GROWTH, "status": "trialing"},))
    assert out["pilot_free"] is True and out["monthly_total"] == "0.00" and out["lines"][0]["trialing"] is True


def test_canceled_subscription_is_not_quoted():
    assert _q(subs=({**GROWTH, "status": "canceled"},))["lines"] == []


@pytest.mark.parametrize("plan_key,ok", [("crm.pro", True), ("content.growth", True), ("content.starter", False),
                                         ("addon.custom_domain", True), ("crmx.pro", False)])
def test_eligible_patterns(plan_key, ok):
    assert qt.eligible(plan_key, TERMS[ps.FOUNDING_ELIGIBLE_PLANS]) is ok


@pytest.mark.parametrize("seats,seats_min,want", [(1, 3, 3), (None, 3, 3), (5, 3, 5), (2, None, 2), (None, None, None)])
def test_billed_seats(seats, seats_min, want):
    assert pv.billed_seats(seats, seats_min) == want


@pytest.mark.parametrize("start,months,want", [(date(2027, 3, 1), 12, date(2028, 2, 29)),
                                               (date(2027, 1, 31), 1, date(2027, 2, 27)),
                                               (date(2027, 6, 15), 12, date(2028, 6, 14))])
def test_months_after(start, months, want):
    assert bt._months_after(start, months) == want


CATALOGUE = {"ai.budget_usd_monthly": {"product": "platform", "value_kind": "limit", "unit": "usd_month"},
             "ai.budget_addon_usd_monthly": {"product": "platform", "value_kind": "limit", "unit": "usd_month"},
             "tp.custom_domain": {"product": "tripplanner", "value_kind": "flag", "unit": None},
             "crm.custom_domain": {"product": "crm", "value_kind": "flag", "unit": None}}


def test_draft_option_b_fields_and_addon_features():
    d = pl.draft({"name": "Domain", "price_usd_monthly": 15, "price_vnd_monthly": 390000, "overage_usd_per_tour": "3.5",
                  "overage_usd_per_1k_calls": "0.6", "features": [{"feature_key": "tp.custom_domain"},
                                                                  {"feature_key": "crm.custom_domain"}]},
                 product="addon", tier=None, catalogue=CATALOGUE)
    assert (d.price_unit, d.price_vnd_monthly, d.overage_usd_per_1k_calls) == ("unit", Decimal("390000"),
                                                                              Decimal("0.6000"))
    assert len(d.features) == 2


def test_ai_addon_key_on_tripplanner_but_not_on_content():
    d = pl.draft({"name": "Hosted", "features": [{"feature_key": "ai.budget_addon_usd_monthly", "limit_value": 10}]},
                 product="tripplanner", tier=None, catalogue=CATALOGUE)
    assert d.features[0].limit_value == Decimal("10.00")
    with pytest.raises(pl.PlanError) as e:
        pl.draft({"name": "Growth", "features": [{"feature_key": "ai.budget_addon_usd_monthly", "limit_value": 1}]},
                 product="content", tier=None, catalogue=CATALOGUE)
    assert e.value.code == "ai_addon_not_on_content"
    with pytest.raises(pl.PlanError) as e:
        pl.draft({"name": "Hosted", "features": [{"feature_key": "ai.budget_usd_monthly", "limit_value": 1}]},
                 product="tripplanner", tier=None, catalogue=CATALOGUE)
    assert e.value.code == "platform_key_content_only"


@pytest.mark.parametrize("field,value,code", [("seats_min", 0, "invalid_seats_min"), ("seats_min", 501,
                                                                                       "invalid_seats_min"),
                                              ("price_vnd_monthly", "1.5", "invalid_price"),
                                              ("overage_usd_per_tour", "0.12345", "invalid_number")])
def test_draft_refuses(field, value, code):
    with pytest.raises(pl.PlanError) as e:
        pl.draft({"name": "Core", field: value}, product="crm", tier=None, catalogue=CATALOGUE)
    assert e.value.code == code


def test_price_change_is_seen():
    a = pl.draft({"name": "Core", "price_usd_monthly": 19}, product="crm", tier=None, catalogue=CATALOGUE)
    b = pl.draft({"name": "Core", "price_usd_monthly": 19, "seats_min": 3}, product="crm", tier=None,
                 catalogue=CATALOGUE)
    c = pl.draft({"name": "Core", "price_usd_monthly": 19, "price_vnd_monthly": 490000}, product="crm", tier=None,
                 catalogue=CATALOGUE)
    assert a.prices() == b.prices() and a.prices() != c.prices()


def test_reviews():
    plans = [{"plan_key": "content.growth", "name": "Growth", "price_usd_monthly": "349.00",
              "price_vnd_monthly": "9500000", "price_set_at": "2027-01-10"},
             {"plan_key": "content.enterprise", "name": "Enterprise", "price_usd_monthly": None,
              "price_vnd_monthly": None, "price_set_at": None}]
    terms = {ps.FX_VND_PER_USD: 26000, ps.PRICE_REVIEW_MONTHS: 12, ps.VND_PRICE_REVIEW_MONTHS: 3}
    (r,) = pl.reviews(plans, terms, today=TODAY)
    assert (r["price_review_due"], r["price_review_overdue"], r["vnd_review_due"], r["vnd_review_overdue"]) == (
        "2028-01-10", False, "2027-04-10", True)
    assert r["vnd_drift_pct"] == "4.7"                        # 9,500,000 vs 349 x 26,000 = 9,074,000


def test_every_pricing_key_is_compiled():
    from shared import settings
    keys = {d["key"] for d in settings.load_definitions()}
    assert set(ps.ALL) <= keys and set(ps.COMMERCIAL) <= set(ps.ALL)
