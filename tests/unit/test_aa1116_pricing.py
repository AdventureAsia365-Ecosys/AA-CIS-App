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
         ps.FOUNDING_ELIGIBLE_PLANS: ["content.growth", "content.business", "crm.*", "tripplanner.*"],
         ps.COMBO_DISCOUNT_PCT: 10, ps.COMBO_PRODUCTS: ["content", "crm", "tripplanner"], ps.FX_VND_PER_USD: 26000,
         ps.PILOT_FREE: True}
GROWTH = {"product": "content", "plan_key": "content.growth", "plan_name": "Growth", "status": "active",
          "price_unit": "tenant", "price_usd_monthly": "349", "price_vnd_monthly": "8990000"}
CRM = {"product": "crm", "plan_key": "crm.core", "plan_name": "CRM Core", "status": "active", "price_unit": "seat",
       "price_usd_monthly": "19", "price_vnd_monthly": "490000", "seats": 1, "seats_included": 3, "seats_min": 3}
TP = {"product": "tripplanner", "plan_key": "tripplanner.hosted", "plan_name": "Hosted", "status": "active",
      "price_unit": "site", "price_usd_monthly": "49", "price_vnd_monthly": "1250000"}
STARTER = {**GROWTH, "plan_key": "content.starter", "plan_name": "Starter", "price_usd_monthly": "149"}


def _q(tenant=None, subs=(GROWTH,)):
    return qt.build({"billing_currency": "USD", **(tenant or {})}, list(subs), TERMS, today=TODAY).out()


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


def test_a_missing_price():
    out = _q(subs=({**TP, "price_usd_monthly": None},))
    assert out["lines"][0]["price_missing"] is True and out["monthly_total"] == "0.00"


def test_agreed_price_inside_and_after_the_lock():
    locked = {**GROWTH, "agreed_price_monthly": "300", "agreed_currency": "USD", "agreed_until": "2027-12-31"}
    assert _q(subs=(locked,))["monthly_total"] == "300.00"
    ended = {**locked, "agreed_until": "2027-01-31"}
    assert _q(subs=(ended,))["monthly_total"] == "349.00"
    other_currency = {**locked, "agreed_currency": "VND"}
    assert _q(subs=(other_currency,))["monthly_total"] == "349.00"


def test_locked_seat_price_follows_a_seat_increase():
    # the agreed price is per billed unit (per seat): 3 seats locked at 19, the agency grows to 10 seats -> 10 x 19
    locked = {**CRM, "agreed_price_monthly": "19", "agreed_currency": "USD", "agreed_until": "2027-12-31"}
    line = _q(subs=(locked,))["lines"][0]
    assert (line["quantity"], line["unit_price"], line["amount"], line["agreed"]) == (3, "19.00", "57.00", True)
    line = _q(subs=({**locked, "seats": 10},))["lines"][0]
    assert (line["quantity"], line["unit_price"], line["amount"]) == (10, "19.00", "190.00")
    # an agreed seat price under the list (17) applies to every billed seat, the minimum included
    line = _q(subs=({**locked, "agreed_price_monthly": "17", "seats": 1},))["lines"][0]
    assert (line["quantity"], line["amount"]) == (3, "51.00")


def test_lock_stores_the_unit_price_not_the_line(monkeypatch):
    monkeypatch.setattr(bt.ps, "int_value", lambda key: {ps.PRICE_LOCK_MONTHS: 12}[key])
    sub = {**CRM, "seats": 3, "agreed_price_monthly": None, "agreed_currency": None, "agreed_until": None}
    assert bt._agreed_terms(sub, {"lock": True}, "USD", TODAY) == (Decimal("19"), "USD", date(2028, 6, 14))
    tp = {**TP, "agreed_price_monthly": None, "agreed_currency": None, "agreed_until": None}
    assert bt._agreed_terms(tp, {"lock": True}, "VND", TODAY)[:2] == (Decimal("1250000"), "VND")


def test_floor_compares_the_line_the_agreed_price_gives():
    enterprise = {**GROWTH, "plan_key": "content.enterprise", "price_usd_monthly": None, "price_floor_usd_monthly": 699}
    with pytest.raises(bt.TermsError) as e:
        bt._check_floor(enterprise, Decimal("600"), "USD", TODAY)
    assert e.value.code == "below_price_floor"
    bt._check_floor(enterprise, Decimal("699"), "USD", TODAY)
    seat_plan = {**CRM, "seats": 1, "price_floor_usd_monthly": 50}
    bt._check_floor(seat_plan, Decimal("19"), "USD", TODAY)          # 3 billed seats x 19 = 57 >= 50
    with pytest.raises(bt.TermsError):
        bt._check_floor(seat_plan, Decimal("15"), "USD", TODAY)      # 3 x 15 = 45 < 50


def test_billing_details_audit_never_holds_personal_values():
    before = {"legal_entity_name": None, "tax_id": "0312345678", "billing_email": "old@zz-lotus.example",
              "billing_address": "1 Zz Road", "billing_country": "VN", "billing_currency": "USD",
              "founding_offer_from": None, "founding_offer_until": None}
    after = {**before, "legal_entity_name": "Zz Lotus Travel Co", "tax_id": "0399999999", "billing_email": None,
             "billing_currency": "VND"}
    changed = sorted(k for k in before if before[k] != after[k])
    out = bt.details_audit(changed, before, after)
    text = repr(out)
    for raw in ("0312345678", "0399999999", "old@zz-lotus.example", "Zz Lotus Travel Co"):
        assert raw not in text
    assert out["fields"] == ["billing_currency", "billing_email", "legal_entity_name", "tax_id"]
    assert out["before"] == {"billing_currency": "USD"} and out["after"] == {"billing_currency": "VND"}
    assert out["personal"] == {"billing_email": "cleared", "legal_entity_name": "set", "tax_id": "changed"}


def test_pilot_is_free_and_trials_are_listed():
    out = _q({"is_pilot": True}, subs=({**GROWTH, "status": "trialing"},))
    assert out["pilot_free"] is True and out["monthly_total"] == "0.00" and out["lines"][0]["trialing"] is True


def test_canceled_subscription_is_not_quoted():
    assert _q(subs=({**GROWTH, "status": "canceled"},))["lines"] == []


@pytest.mark.parametrize("plan_key,ok", [("crm.pro", True), ("content.growth", True), ("content.starter", False),
                                         ("tripplanner.pro", True), ("crmx.pro", False)])
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


def test_draft_option_b_fields():
    d = pl.draft({"name": "Pro", "price_usd_monthly": 99, "price_vnd_monthly": 2570000, "overage_usd_per_tour": "3.5",
                  "overage_usd_per_1k_calls": "0.6", "features": [{"feature_key": "tp.custom_domain"}]},
                 product="tripplanner", tier=None, catalogue=CATALOGUE)
    assert (d.price_unit, d.price_vnd_monthly, d.overage_usd_per_1k_calls) == ("site", Decimal("2570000"),
                                                                              Decimal("0.6000"))
    assert len(d.features) == 1


def test_no_retail_addon_product():
    # S282 decision 14: no addon.* plans (no custom-domain add-on, no AI pack), so no add-on product or unit price
    assert "addon" not in pl.PRODUCTS and "unit" not in pl.PRICE_UNITS
    with pytest.raises(pl.PlanError) as e:
        pl.draft({"name": "Zz", "price_unit": "unit"}, product="crm", tier=None, catalogue=CATALOGUE)
    assert e.value.code == "invalid_price_unit"
    with pytest.raises(pl.PlanError) as e:          # the custom domain stays a TripPlanner (Pro) feature
        pl.draft({"name": "Zz", "features": [{"feature_key": "tp.custom_domain"}]}, product="crm", tier=None,
                 catalogue=CATALOGUE)
    assert e.value.code == "feature_product_mismatch"


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
