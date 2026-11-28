"""AA-1191 — USD only: the Enabled currencies setting (CIS 496) hides and refuses every currency that is not enabled.

The pure helpers (services/tenant_account/currency.py), the quote in the effective currency without a VND reference,
the billing details as served, the refused agreed currency and VND plan prices, the commercial terms without the VND
figures, and the admin routes answering 422 ``currency_not_enabled``. No DB; invented plan and tenant names only.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.tenant_account import billing_terms as bt
from services.tenant_account import currency
from services.tenant_account import plans as pl
from services.tenant_account import pricing_settings as ps
from services.tenant_account import quote as qt

TODAY = date(2027, 6, 15)
TERMS = {ps.ANNUAL_PAID_MONTHS: 10, ps.FOUNDING_DISCOUNT_PCT: 30, ps.FOUNDING_MONTHS: 12,
         ps.FOUNDING_ELIGIBLE_PLANS: [], ps.COMBO_DISCOUNT_PCT: 10, ps.COMBO_PRODUCTS: ["content", "crm"],
         ps.FX_VND_PER_USD: 26000, ps.PILOT_FREE: True}
STARTER = {"product": "content", "plan_key": "content.starter", "plan_name": "Starter", "status": "active",
           "price_unit": "tenant", "price_usd_monthly": "149", "price_vnd_monthly": "3790000"}
USD_ONLY = ("USD",)
BOTH = ("USD", "VND")


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, want", [
    (None, ("USD",)), ([], ("USD",)), (["USD"], ("USD",)), (["VND"], ("USD", "VND")),
    (["vnd", "USD", "VND"], ("USD", "VND")), (["USD", 5, ""], ("USD",)),
])
def test_normalise_keeps_usd_first_and_drops_duplicates(raw, want):
    assert currency.normalise(raw) == want


def test_the_default_is_usd_only():
    assert currency.enabled() == USD_ONLY


def test_a_build_without_the_compiled_key_serves_usd_only(monkeypatch):
    def _missing(key, tenant_id=None):
        raise KeyError(key)

    monkeypatch.setattr(currency.settings, "get", _missing)
    assert currency.enabled() == USD_ONLY


@pytest.mark.parametrize("stored, allowed, want", [
    ("VND", USD_ONLY, "USD"), ("VND", BOTH, "VND"), ("USD", USD_ONLY, "USD"), (None, USD_ONLY, "USD"),
    ("", BOTH, "USD"),
])
def test_effective_currency(stored, allowed, want):
    assert currency.effective(stored, allowed) == want


def test_problem_names_the_setting_and_accepts_empty_values():
    assert currency.problem("USD", USD_ONLY) is None
    assert currency.problem(None, USD_ONLY) is None
    assert currency.problem("VND", BOTH) is None
    why = currency.problem("VND", USD_ONLY, field="billing_currency")
    assert why.startswith("billing_currency: VND is not an enabled currency") and "Enabled currencies" in why


# ── the quote ───────────────────────────────────────────────────────────────────────────────────────────────────
def _q(tenant, allowed=None):
    terms = TERMS if allowed is None else {**TERMS, currency.ENABLED_CURRENCIES: list(allowed)}
    return qt.build(tenant, [STARTER], terms, today=TODAY).out()


def test_a_vnd_tenant_is_quoted_in_usd_while_vnd_is_disabled():
    out = _q({"billing_currency": "VND"}, USD_ONLY)
    assert out["currency"] == "USD" and out["monthly_total"] == "149.00"
    assert out["reference"] is None                           # never a VND equivalent


def test_a_usd_tenant_gets_no_vnd_reference_while_vnd_is_disabled():
    assert _q({"billing_currency": "USD"}, USD_ONLY)["reference"] is None
    assert _q({"billing_currency": "USD"}, BOTH)["reference"]["currency"] == "VND"


def test_vnd_comes_back_with_the_setting():
    out = _q({"billing_currency": "VND"}, BOTH)
    assert out["currency"] == "VND" and out["monthly_total"] == "3790000"


def test_an_agreed_vnd_price_does_not_apply_while_vnd_is_disabled():
    sub = {**STARTER, "agreed_price_monthly": "3000000", "agreed_currency": "VND", "agreed_until": None}
    out = qt.build({"billing_currency": "VND"}, [sub], {**TERMS, currency.ENABLED_CURRENCIES: ["USD"]},
                   today=TODAY).out()
    assert out["currency"] == "USD" and out["monthly_total"] == "149.00"


# ── billing details and terms ───────────────────────────────────────────────────────────────────────────────────
def test_billing_details_serve_the_effective_currency_and_the_hidden_flag():
    out = bt.with_effective_currency({"billing_currency": "VND", "tax_id": None}, USD_ONLY)
    assert out["billing_currency"] == "USD" and out["billing_currency_hidden"] is True
    assert out["enabled_currencies"] == ["USD"]
    assert "VND" not in str(out)
    out = bt.with_effective_currency({"billing_currency": "VND"}, BOTH)
    assert out["billing_currency"] == "VND" and out["billing_currency_hidden"] is False


def test_an_agreed_price_in_a_disabled_currency_is_refused():
    sub = {**STARTER, "agreed_price_monthly": None, "agreed_currency": None, "agreed_until": None}
    with pytest.raises(bt.TermsError) as e:
        bt._agreed_terms(sub, {"agreed_price_monthly": "100", "agreed_currency": "VND"}, "USD", TODAY, USD_ONLY)
    assert (e.value.code, e.value.status, e.value.field) == ("currency_not_enabled", 422, "agreed_currency")
    assert bt._agreed_terms(sub, {"agreed_price_monthly": "100", "agreed_currency": "USD"}, "USD", TODAY,
                            USD_ONLY)[:2] == (Decimal("100"), "USD")


class _Conn:
    """The calls set_details makes, for a tenant stored with ``billing_currency``."""

    def __init__(self, stored: str):
        self.row = {"tenant_id": "t", "name": "Zz Paper Lantern Tours", "kind": "agency", "is_pilot": False,
                    "legal_entity_name": None, "tax_id": None, "billing_email": None, "billing_address": None,
                    "billing_country": None, "billing_currency": stored, "founding_offer_from": None,
                    "founding_offer_until": None}
        self.executed = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Tx()

    async def execute(self, sql, *args):
        self.executed.append(sql)

    async def fetchval(self, sql, *args):
        return 1

    async def fetchrow(self, sql, *args):
        return dict(self.row)


@pytest.mark.asyncio
async def test_set_details_refuses_vnd_while_disabled():
    conn = _Conn("USD")
    with pytest.raises(bt.TermsError) as e:
        await bt.set_details(conn, "t", {"billing_currency": "VND"}, actor="admin:zz")
    assert (e.value.code, e.value.status, e.value.field) == ("currency_not_enabled", 422, "billing_currency")
    assert not any("UPDATE shared.tenants" in s for s in conn.executed)


@pytest.mark.asyncio
async def test_set_details_keeps_a_stored_vnd_that_the_form_sends_back(monkeypatch):
    """A tenant stored in VND: resending VND (no change) is not a new VND write; USD converts it."""
    monkeypatch.setattr(bt, "_audit", AsyncMock())
    out = await bt.set_details(_Conn("VND"), "t", {"billing_currency": "VND"}, actor="admin:zz")
    assert out["changed"] == [] and out["billing_currency"] == "USD" and out["billing_currency_hidden"] is True
    conn = _Conn("VND")
    await bt.set_details(conn, "t", {"billing_currency": "USD"}, actor="admin:zz")
    assert any("UPDATE shared.tenants" in s for s in conn.executed)


# ── plans and commercial terms ──────────────────────────────────────────────────────────────────────────────────
def _draft(**fields):
    return pl.draft({"name": "Zz Plan", "price_usd_monthly": 99, **fields}, product="tripplanner", tier=None,
                    catalogue={})


def test_a_new_vnd_price_is_refused_while_vnd_is_disabled():
    with pytest.raises(pl.PlanError) as e:
        pl.refuse_disabled_vnd(None, _draft(price_vnd_monthly=2570000), USD_ONLY)
    assert (e.value.code, e.value.status) == ("currency_not_enabled", 422)
    pl.refuse_disabled_vnd(None, _draft(price_vnd_monthly=2570000), BOTH)
    pl.refuse_disabled_vnd(None, _draft(), USD_ONLY)


def test_a_stored_vnd_price_may_stay_or_be_cleared_but_not_changed():
    before = _draft(price_vnd_monthly=2570000)
    pl.refuse_disabled_vnd(before, _draft(price_vnd_monthly=2570000, name="Zz Plan 2"), USD_ONLY)
    pl.refuse_disabled_vnd(before, _draft(price_vnd_monthly=None), USD_ONLY)
    with pytest.raises(pl.PlanError):
        pl.refuse_disabled_vnd(before, _draft(price_vnd_monthly=2600000), USD_ONLY)
    with pytest.raises(pl.PlanError):
        pl.refuse_disabled_vnd(before, _draft(price_vnd_monthly=2570000, price_extra_seat_vnd=90000), USD_ONLY)


def test_reviews_leave_the_vnd_figures_out_while_vnd_is_disabled():
    plans = [{"plan_key": "content.growth", "name": "Zz Growth", "price_usd_monthly": "349.00",
              "price_vnd_monthly": "9500000", "price_set_at": "2027-01-10"}]
    terms = {ps.FX_VND_PER_USD: 26000, ps.PRICE_REVIEW_MONTHS: 12, ps.VND_PRICE_REVIEW_MONTHS: 3}
    (r,) = pl.reviews(plans, terms, today=TODAY, vnd=False)
    assert (r["vnd_review_due"], r["vnd_review_overdue"], r["vnd_drift_pct"]) == (None, False, None)
    assert r["price_review_due"] == "2028-01-10"


def _admin_app(monkeypatch):
    from api.routers import admin_plans

    monkeypatch.setenv("ADMIN_SECRET", "zz-secret")
    monkeypatch.setattr(admin_plans, "verify_admin_secret", lambda s: None)
    app = FastAPI()
    app.include_router(admin_plans.router)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.pool = pool
    app.dependency_overrides = {}
    from api.core.staff_authz import staff_policy
    app.dependency_overrides[staff_policy] = lambda: None
    return app, admin_plans


def test_commercial_terms_hide_the_vnd_terms_while_vnd_is_disabled(monkeypatch):
    app, admin_plans = _admin_app(monkeypatch)
    monkeypatch.setattr(admin_plans.pl, "list_plans", AsyncMock(return_value=[]))
    c = TestClient(app)
    body = c.get("/admin/plans/commercial-terms").json()
    keys = {t["key"] for t in body["terms"]}
    assert ps.FX_VND_PER_USD not in keys and ps.VND_PRICE_REVIEW_MONTHS not in keys
    assert ps.ANNUAL_PAID_MONTHS in keys and body["enabled_currencies"] == ["USD"]
    monkeypatch.setattr(admin_plans.cur_, "enabled", lambda: BOTH)
    keys = {t["key"] for t in c.get("/admin/plans/commercial-terms").json()["terms"]}
    assert ps.FX_VND_PER_USD in keys


def test_a_refused_vnd_plan_price_is_a_422_with_the_code(monkeypatch):
    app, admin_plans = _admin_app(monkeypatch)
    why = currency.problem("VND", USD_ONLY, field="price_vnd_monthly")
    monkeypatch.setattr(admin_plans.pl, "update_plan",
                        AsyncMock(side_effect=pl.PlanError(currency.CODE, why)))
    monkeypatch.setattr(admin_plans, "_require_tenant_admin", lambda role: None)
    resp = TestClient(app).patch("/admin/plans/content.growth", json={"version": 2, "price_vnd_monthly": 1})
    assert resp.status_code == 422 and resp.json()["detail"]["detail"] == "currency_not_enabled"
    assert resp.headers["X-Error-Code"] == "currency_not_enabled"
