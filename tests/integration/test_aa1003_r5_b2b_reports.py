"""AA-1003 R5 against real Postgres: every SQL the FI-08, BK-03 and FI-07 readers run, as written, on the replayed
schema, then each renderer on what the collectors returned.

Everything runs on one connection inside an outer transaction rolled back at the end (a pool shim hands it to the
tenant provisioning saga). Tenants are provisioned with invented names; their AI calls sit in March 2023, a month no
other test writes. The Booking contract views (bk-017, bk-042) are not part of the CIS replay: when they are missing,
stand-in views with the contract's columns are created inside the transaction.
"""
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.reports import b2b_ops, money, registry  # noqa: E402
from services.tenant_account import provision as pv  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
UTC = timezone.utc
MAR1, MAR31 = date(2023, 3, 1), date(2023, 3, 31)
MON, SUN = date(2023, 3, 6), date(2023, 3, 12)
NOW = datetime(2023, 4, 3, 2, tzinfo=UTC)
STAGE = "zz_aa1003_r5"

FUNNEL_VIEW = ("SELECT * FROM (VALUES ('2023-03-07'::date, 'trip_case_created'::text, 'crm'::text, 2::bigint), "
               "('2023-03-08'::date, 'confirmed'::text, 'crm'::text, 1::bigint), "
               "('2023-03-09'::date, 'cancelled'::text, 'crm'::text, 1::bigint)) AS v(day, metric, source_system, n)")
CASES_VIEW = "SELECT * FROM (VALUES ('proposal_sent'::text, 2::bigint)) AS v(status, n)"


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
    await c.execute("ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS plan_id uuid")
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, tier="starter") -> str:
    body = {"request_key": f"zz-r5-{uuid.uuid4().hex[:12]}", "mode": "create", "name": "Wrenfold Journeys synthetic",
            "slug": f"zz1003r5-{uuid.uuid4().hex[:10]}", "plan_tier": tier, "posts_per_week": 3}
    out = await pv.provision(_Pool(conn), pv.parse(body), job_id=str(uuid.uuid4()))
    return out["tenant_id"]


async def _call(conn, tid, usd, *, day=12, shadow=None, status=None):
    await conn.execute(
        "INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, cost_usd, quality_signal, created_at, "
        "is_shadow, status, app) VALUES ($1::uuid, $2, 'writer', 'zz-model', $3, '{}'::jsonb, $4, $5, $6, 'cis')",
        tid, STAGE, usd, datetime(2023, 3, day, 5, tzinfo=UTC), shadow, status)


async def _ensure_view(conn, name: str, body: str) -> None:
    if await conn.fetchval("SELECT to_regclass($1)", name) is None:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS booking")
        await conn.execute(f"CREATE VIEW {name} AS {body}")


def _unread(facts: dict) -> dict:
    return {k: v for k, v in facts.items() if money.failed(v)}


@pytest.mark.asyncio
async def test_fi08_reads_subscriptions_entitlements_ledger_and_caps(conn):
    reg = registry.load()
    spender = await _tenant(conn)                                  # starter: 15 USD included, alert 80 %, stop 100 %
    quiet = await _tenant(conn)
    if await conn.fetchval("SELECT count(*) FROM shared.llm_call_daily WHERE day BETWEEN $1 AND $2", MAR1, MAR31):
        pytest.skip("rolled ledger days in March 2023 on this database")
    await _call(conn, spender, 10.0)
    await _call(conn, spender, 3.0, day=20, status="refused")
    await _call(conn, spender, 50.0, shadow=True)                  # shadow: AA's own A/B cost, never the tenant's
    await _call(conn, None, 2.5)                                   # platform work
    facts = await b2b_ops.collect_fi08(conn, MAR1, MAR31)
    assert _unread(facts) == {}, facts
    out = b2b_ops.render_fi08(reg, reg.report("FI-08"), MAR1, MAR31, facts, NOW)
    rows = {r["tenant_id"]: r for r in out.output["tenants"]["rows"]}
    assert {spender, quiet} <= set(rows)
    s = rows[spender]
    assert s["spend_usd"] == pytest.approx(13.0) and s["calls"] == 2 and s["refused_calls"] == 1
    assert s["cap"]["monthly_usd"] == pytest.approx(15.0) and s["cap"]["source"] == "plan"
    assert s["state"] == "alert" and s["used_pct"] == pytest.approx(86.7)
    assert s["subscriptions"] and s["subscriptions"][0]["product"] == "content"
    assert s["entitlements"]["total"] >= 1
    assert rows[quiet]["spend_usd"] == 0 and rows[quiet]["state"] == "ok"
    assert out.output["tenants"]["platform_usd"] >= 2.5
    k = {x["id"]: x for x in out.output["kpis"]}
    assert k["K-TEN-AICAP"]["status"] in ("amber", "red") and k["K-TEN-AICAP"]["value"] >= 1


@pytest.mark.asyncio
async def test_bk03_and_fi07_read_the_booking_contract_views(conn):
    reg = registry.load()
    tid = await _tenant(conn)
    await _ensure_view(conn, "booking.v_ops_funnel_daily", FUNNEL_VIEW)
    await _ensure_view(conn, "booking.v_ops_case_status", CASES_VIEW)
    await _ensure_view(
        conn, "booking.v_tenant_billing_status",
        f"SELECT '{tid}'::uuid AS tenant_id, 'warn'::text AS status, 12 AS oldest_overdue_days, 1 AS overdue_invoices, "
        "'{\"USD\": 240.50}'::jsonb AS overdue_amount, '2023-02-28'::date AS paid_through, 7 AS warn_after_days, "
        "30 AS suspend_after_days")
    bk = await b2b_ops.collect_bk03(conn, MON, SUN)
    assert _unread(bk) == {}, bk
    out = b2b_ops.render_bk03(reg, reg.report("BK-03"), MON, SUN, bk, NOW)
    assert out.output["report_id"] == "BK-03" and out.output["receivables"]["rows"]
    mine = [r for r in out.output["receivables"]["rows"] if r["tenant_id"] == tid]
    if mine:                                                       # the stand-in view (a real one has other rows)
        assert mine[0]["overdue_amount"] == {"USD": "240.50"} and mine[0]["bucket"] == "1-30"
        assert mine[0]["kind"] is not None
    fi = await b2b_ops.collect_fi07(conn, MON, SUN)
    assert _unread(fi) == {}, fi
    out = b2b_ops.render_fi07(reg, reg.report("FI-07"), MON, SUN, fi, NOW)
    assert out.output["report_id"] == "FI-07" and "Open pipeline now" in out.markdown
