"""AA-1116 (option B pricing) — the services on real Postgres (CIS 435-437, 439 replayed by conftest).

Every test runs inside one outer transaction that is rolled back (the services' own transactions are savepoints):
  * plans: an addon product refused (S282 decision 14: no retail add-on), PATCH crm.core seats_min / VND price with a
    version bump, price_set_at set on a price change, the audit row; ai.budget_addon_usd_monthly accepted on a
    TripPlanner plan (the AI budget grows by it) and refused on a content plan;
  * provision: an attach below seats_min is raised to it (and starts the add-on's trial);
  * billing details: currency, bill-to, founding offer dates (until from the setting), the founding cap and an
    ineligible plan refused; the audit row holds no raw tax id / e-mail; subscription terms: interval, an agreed
    price under the Enterprise floor refused, a lock of a per-seat plan stores the seat price;
  * the quote of a tenant with three products (combo), a locked seat price after a seat increase, and the
    custom-domain entitlement check of add_domain (TripPlanner Pro only).
AA-1191: these tests write VND, so VND is enabled for the module (``enabled_vnd``); the last test checks the default
(USD only): a VND billing currency or plan price is refused on the real tables and nothing is written.
Invented names only.
"""
import os
import sys
import unittest.mock as um
import uuid
from contextlib import asynccontextmanager
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.tenant_account import billing_terms as bt  # noqa: E402
from services.tenant_account import currency  # noqa: E402
from services.tenant_account import plans as pl  # noqa: E402
from services.tenant_account import pricing_settings as ps  # noqa: E402
from services.tenant_account import provision as pv  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ACTOR = "admin:zz-1116"


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


@pytest.fixture(autouse=True)
def enabled_vnd(request, monkeypatch):
    """AA-1191: VND enabled (Platform › Settings › Enabled currencies) for the option B VND cases."""
    if "usd_only" not in request.node.name:
        monkeypatch.setattr(currency, "enabled", lambda: ("USD", "VND"))


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    await c.execute("ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS plan_id uuid")
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, tier="starter") -> str:
    tid = str(uuid.uuid4())
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week) "
                       "VALUES ($1::uuid, 'Zz Saffron Routes', $2, $3::plan_tier_enum, 3)",
                       tid, f"zz-saffron-{tid[:8]}", tier)
    return tid


async def _ai(conn, tid):
    v = await conn.fetchval("SELECT limit_value FROM shared.tenant_entitlement WHERE tenant_id = $1::uuid "
                            "AND feature_key = 'ai.budget_usd_monthly'", tid)
    return None if v is None else float(v)


@pytest.mark.asyncio
async def test_plans_option_b_fields(conn):
    with pytest.raises(pl.PlanError) as e:
        await pl.create_plan(conn, {"plan_key": "addon.zz_pack", "product": "addon", "name": "Zz pack",
                                    "price_usd_monthly": 19}, actor=ACTOR)
    assert e.value.code == "invalid_product"
    made = await pl.create_plan(conn, {"plan_key": "tripplanner.zz_trail", "product": "tripplanner",
                                       "name": "Zz trail", "price_usd_monthly": 49, "price_vnd_monthly": 1250000},
                                actor=ACTOR)
    assert (made["product"], made["price_unit"], made["price_vnd_monthly"], made["price_set_at"]) == (
        "tripplanner", "site", "1250000", date.today().isoformat())
    core = await pl.get_plan(conn, "crm.core")
    out = await pl.update_plan(conn, "crm.core", {"version": core["version"], "seats_min": 3,
                                                  "price_vnd_monthly": 490000}, actor=ACTOR)
    assert (out["seats_min"], out["price_vnd_monthly"], out["version"]) == (3, "490000", core["version"] + 1)
    assert out["price_set_at"] == date.today().isoformat()
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE action = 'plan.updated' "
                               "AND resource_id = 'crm.core' AND actor = $1", ACTOR) == 1
    hosted = await pl.get_plan(conn, "tripplanner.hosted")
    feats = hosted["features"] + [{"feature_key": "ai.budget_addon_usd_monthly", "limit_value": "10"}]
    await pl.update_plan(conn, "tripplanner.hosted", {"version": hosted["version"], "features": feats}, actor=ACTOR)
    tid = await _tenant(conn)                               # starter: 15 USD of AI
    await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, updated_by) "
                       "VALUES ($1::uuid, 'tripplanner', 'tripplanner.hosted', 'active', 'test')", tid)
    assert await _ai(conn, tid) == 25                       # + the Hosted increment
    growth = await pl.get_plan(conn, "content.growth")
    with pytest.raises(pl.PlanError) as e:
        await pl.update_plan(conn, "content.growth", {"version": growth["version"], "features": growth["features"] + [
            {"feature_key": "ai.budget_addon_usd_monthly", "limit_value": "5"}]}, actor=ACTOR)
    assert e.value.code == "ai_addon_not_on_content"


@pytest.mark.asyncio
async def test_attach_below_the_seat_minimum_is_raised(conn):
    await conn.execute("UPDATE shared.plan SET seats_min = 3 WHERE plan_key = 'crm.core'")
    tid = await _tenant(conn)
    req = pv.parse({"request_key": f"zz-att-{uuid.uuid4().hex[:10]}", "mode": "attach", "tenant_id": tid,
                    "add_ons": [{"product": "crm", "plan_key": "crm.core", "seats": 1}]})
    out = await pv.provision(_Pool(conn), req, job_id=str(uuid.uuid4()))
    assert "crm_seats_raised_to_3" in out["steps"]
    row = await conn.fetchrow("SELECT seats, status FROM shared.tenant_subscription WHERE tenant_id = $1::uuid "
                              "AND product = 'crm'", tid)
    assert (row["seats"], row["status"]) == (3, "trialing")


@pytest.mark.asyncio
async def test_billing_details_and_founding_offer(conn):
    tid = await _tenant(conn, "growth")
    out = await bt.set_details(conn, tid, {"billing_currency": "VND", "billing_country": "vn",
                                           "legal_entity_name": "Zz Saffron Routes Co",
                                           "founding_offer_from": "2027-03-01"}, actor=ACTOR)
    assert (out["billing_currency"], out["billing_country"], out["founding_offer_until"]) == (
        "VND", "VN", "2028-02-29")
    await bt.set_details(conn, tid, {"tax_id": "0312345678", "billing_email": "zz@saffron.example"}, actor=ACTOR)
    audit = await conn.fetch("SELECT details::text AS d FROM acp_shared.audit_log WHERE tenant_id = $1 "
                             "AND action = 'tenant.billing_details_set'", tid)
    assert len(audit) == 2
    assert all("0312345678" not in r["d"] and "zz@saffron.example" not in r["d"] and "Saffron Routes Co" not in r["d"]
               for r in audit)
    starter = await _tenant(conn, "starter")
    with pytest.raises(bt.TermsError) as e:
        await bt.set_details(conn, starter, {"founding_offer_from": "2027-03-01"}, actor=ACTOR)
    assert e.value.code == "founding_not_eligible"
    # the cap: with one founding place (the setting), a second agency is refused
    other = await _tenant(conn, "growth")
    real = ps.int_value
    with um.patch.object(ps, "int_value", lambda k: 1 if k == ps.FOUNDING_MAX_AGENCIES else real(k)):
        with pytest.raises(bt.TermsError) as e:
            await bt.set_details(conn, other, {"founding_offer_from": "2027-03-01"}, actor=ACTOR)
    assert e.value.code == "founding_offer_full"


@pytest.mark.asyncio
async def test_subscription_terms_floor_and_lock(conn):
    tid = await _tenant(conn, "growth")
    await conn.execute("UPDATE shared.plan SET price_floor_usd_monthly = 699 WHERE plan_key = 'content.growth'")
    with pytest.raises(bt.TermsError) as e:
        await bt.set_terms(conn, tid, "content", {"agreed_price_monthly": 500, "agreed_currency": "USD"},
                           actor=ACTOR)
    assert e.value.code == "below_price_floor"
    await conn.execute("UPDATE shared.plan SET price_floor_usd_monthly = NULL WHERE plan_key = 'content.growth'")
    out = await bt.set_terms(conn, tid, "content", {"lock": True, "billing_interval": "year"}, actor=ACTOR,
                             today=date(2027, 3, 1))
    price = await conn.fetchval("SELECT price_usd_monthly FROM shared.plan WHERE plan_key = 'content.growth'")
    assert (out["billing_interval"], out["agreed_price_monthly"], out["agreed_currency"], out["agreed_until"]) == (
        "year", str(price.quantize(price)), "USD", "2028-02-29")
    # a per-seat plan locks the seat price, not the line (3 seats x 19 would be 57)
    await conn.execute("UPDATE shared.plan SET price_usd_monthly = 19, seats_min = 3 WHERE plan_key = 'crm.core'")
    await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, seats, "
                       "updated_by) VALUES ($1::uuid, 'crm', 'crm.core', 'active', 3, 'test')", tid)
    out = await bt.set_terms(conn, tid, "crm", {"lock": True}, actor=ACTOR, today=date(2027, 3, 1))
    assert out["agreed_price_monthly"] == "19.00"
    await conn.execute("UPDATE shared.tenant_subscription SET seats = 10 WHERE tenant_id = $1::uuid "
                       "AND product = 'crm'", tid)
    q = await bt.tenant_quote(conn, tid, today=date(2027, 6, 1))
    crm = next(ln for ln in q["lines"] if ln["product"] == "crm")
    assert (crm["quantity"], crm["unit_price"], crm["amount"], crm["agreed"]) == (10, "19.00", "190.00", True)


@pytest.mark.asyncio
async def test_quote_combo_and_custom_domain_entitlement(conn):
    for key, price in (("content.growth", 349), ("crm.core", 19), ("tripplanner.hosted", 49)):
        await conn.execute("UPDATE shared.plan SET price_usd_monthly = $2 WHERE plan_key = $1", key, price)
    await conn.execute("UPDATE shared.plan SET seats_min = 3 WHERE plan_key = 'crm.core'")
    tid = await _tenant(conn, "growth")
    await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, seats, "
                       "updated_by) VALUES ($1::uuid, 'crm', 'crm.core', 'active', 1, 'test'), "
                       "($1::uuid, 'tripplanner', 'tripplanner.hosted', 'active', NULL, 'test')", tid)
    q = await bt.tenant_quote(conn, tid, today=date(2027, 6, 1))
    assert (q["subtotal"], q["discount_kind"], q["monthly_total"]) == ("455.00", "combo", "409.50")
    from services.tenant_account import tripplanner_domain as tpd
    assert await conn.fetchval(tpd.CUSTOM_DOMAIN_ENTITLED_SQL, tid) is False      # Hosted has no custom domain
    await conn.execute("UPDATE shared.tenant_subscription SET plan_key = 'tripplanner.pro' WHERE tenant_id = $1::uuid "
                       "AND product = 'tripplanner'", tid)
    assert await conn.fetchval(tpd.CUSTOM_DOMAIN_ENTITLED_SQL, tid) is True        # a Pro feature only (decision 14)


@pytest.mark.asyncio
async def test_usd_only_refuses_vnd_writes_and_bills_in_usd(conn):
    """AA-1191 default (USD only): VND refused on the billing currency and on a plan price; a tenant stored in VND is
    served and quoted in USD, its stored value untouched."""
    tid = await _tenant(conn, "growth")
    with pytest.raises(bt.TermsError) as e:
        await bt.set_details(conn, tid, {"billing_currency": "VND"}, actor=ACTOR)
    assert (e.value.code, e.value.status) == ("currency_not_enabled", 422)
    core = await pl.get_plan(conn, "crm.core")
    with pytest.raises(pl.PlanError) as e:
        await pl.update_plan(conn, "crm.core", {"version": core["version"], "price_vnd_monthly": 123000}, actor=ACTOR)
    assert e.value.code == "currency_not_enabled"
    await conn.execute("UPDATE shared.tenants SET billing_currency = 'VND' WHERE tenant_id = $1::uuid", tid)
    out = await bt.details(conn, tid)
    assert (out["billing_currency"], out["billing_currency_hidden"], out["enabled_currencies"]) == ("USD", True,
                                                                                                    ["USD"])
    quote = await bt.tenant_quote(conn, tid)
    assert quote["currency"] == "USD" and quote["reference"] is None
    stored = await conn.fetchval("SELECT billing_currency::text AS c FROM shared.tenants WHERE tenant_id = $1::uuid",
                                 tid)
    assert stored == "VND"
