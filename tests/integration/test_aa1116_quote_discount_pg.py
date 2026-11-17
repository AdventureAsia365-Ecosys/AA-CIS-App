"""AA-1116 (contract tenant.entitlements.v1 1.10) on real Postgres — services/tenant_account/quote_discount.py writes
the quote's share of each subscription line into shared.tenant_subscription.quote_discount_pct / _kind (CIS 443):

  * a tenant with content + crm + tripplanner gets the combo share on the three lines; a second refresh writes nothing;
  * the founding offer (eligible lines only) wins over the combo and replaces it; a canceled add-on goes back to 0;
  * no account event is written (the update trigger ignores the columns), so the CRM / Booking pulls see nothing;
  * refresh_all skips kind internal and refreshes each other tenant.

The commercial terms are passed in (the fixed TERMS of test_aa1116_pricing), so the test does not depend on the
seeded registry values. Each test runs on one connection inside an outer transaction that is rolled back.
Invented names only.
"""
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.tenant_account import quote_discount as qd  # noqa: E402
from tests.unit.test_aa1116_pricing import TERMS  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TODAY = date(2027, 6, 15)


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, *, kind="demo", products=("crm", "tripplanner")):
    tid = str(uuid.uuid4())
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                       "is_active, kind) VALUES ($1::uuid, 'Fernwick Journeys synthetic', $2, "
                       "'growth'::plan_tier_enum, 3, 60, true, $3)", tid, f"zz1116q-{tid[:12]}", kind)
    plans = {"crm": ("crm.core", 3), "tripplanner": ("tripplanner.hosted", None)}
    for p in products:
        await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, seats, "
                           "updated_by) VALUES ($1::uuid, $2, $3, 'active', $4, 'test:aa1116')", tid, p, *plans[p])
    await conn.execute("UPDATE shared.plan SET price_usd_monthly = CASE plan_key WHEN 'content.growth' THEN 349 "
                       "WHEN 'crm.core' THEN 19 WHEN 'tripplanner.hosted' THEN 49 END "
                       "WHERE plan_key IN ('content.growth', 'crm.core', 'tripplanner.hosted')")
    return tid


async def _shares(conn, tid):
    rows = await conn.fetch("SELECT product, quote_discount_pct AS pct, quote_discount_kind AS kind "
                            "FROM shared.tenant_subscription WHERE tenant_id = $1::uuid", tid)
    return {r["product"]: (r["pct"], r["kind"]) for r in rows}


async def _events(conn, tid):
    return await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_account_event WHERE tenant_id = $1::uuid", tid)


@pytest.mark.asyncio
async def test_combo_then_founding_then_cancel(conn):
    tid = await _tenant(conn)
    events = await _events(conn, tid)
    out = await qd.refresh(conn, tid, today=TODAY, terms=TERMS)
    assert sorted(out["changed"]) == ["content", "crm", "tripplanner"]
    assert await _shares(conn, tid) == {p: (Decimal("10.00"), "combo") for p in ("content", "crm", "tripplanner")}
    assert (await qd.refresh(conn, tid, today=TODAY, terms=TERMS))["changed"] == []
    assert await _events(conn, tid) == events

    await conn.execute("UPDATE shared.tenants SET founding_offer_from = '2027-03-01', founding_offer_until = "
                       "'2028-02-29' WHERE tenant_id = $1::uuid", tid)
    await qd.refresh(conn, tid, today=TODAY, terms=TERMS)
    assert await _shares(conn, tid) == {p: (Decimal("30.00"), "founding") for p in ("content", "crm", "tripplanner")}

    await conn.execute("UPDATE shared.tenants SET founding_offer_from = NULL, founding_offer_until = NULL "
                       "WHERE tenant_id = $1::uuid", tid)
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'canceled', canceled_at = now(), "
                       "updated_by = 'test:aa1116' WHERE tenant_id = $1::uuid AND product = 'tripplanner'", tid)
    await qd.refresh(conn, tid, today=TODAY, terms=TERMS)
    zero = (Decimal("0.00"), None)
    assert await _shares(conn, tid) == {"content": zero, "crm": zero, "tripplanner": zero}


@pytest.mark.asyncio
async def test_refresh_all_skips_internal(conn, monkeypatch):
    monkeypatch.setattr(qd.ps, "snapshot", lambda keys: TERMS)
    agency = await _tenant(conn)
    internal = await _tenant(conn, kind="internal")
    out = await qd.refresh_all(_Pool(conn), today=TODAY)
    assert out["outcome"] == "ok" and out["failed"] == 0 and out["changed"] >= 3
    assert (await _shares(conn, agency))["crm"] == (Decimal("10.00"), "combo")
    assert (await _shares(conn, internal))["crm"] == (Decimal("0.00"), None)
