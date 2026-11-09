"""AA-1037 R4 slice 1 on real Postgres: the hourly tenant_billing_tick reads AA-Booking's billing status and moves the
tenant account through the CIS 354 account path (root ADR 0019 d9).

``booking.v_tenant_billing_status`` (bk-042) is stood in by a table of its exact columns inside the test's outer
transaction (rolled back with everything else; a pool shim hands the one connection out, so the tick's own
transactions are savepoints). Covers: no view -> skipped; warn -> active becomes past_due with one bell row a day;
past_due -> suspended for billing; paid again -> back to active with a newer paid_through; a suspension for another
reason stays; on Dev a real (agency) tenant is never moved. Names are invented.
"""
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.tenant_account import billing  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
# Copied from AA-Booking migrations/042_agency_billing_guards_status_view.sql (the view's columns and types).
BK042_COLUMNS = ("tenant_id uuid, status text, oldest_overdue_days integer, overdue_invoices integer, "
                 "overdue_amount jsonb, paid_through date, warn_after_days integer, suspend_after_days integer")


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
        await c.execute("CREATE SCHEMA IF NOT EXISTS booking")
        await c.execute("DROP TABLE IF EXISTS booking.v_tenant_billing_status")
        await c.execute(f"CREATE TABLE booking.v_tenant_billing_status ({BK042_COLUMNS})")
        await c.execute("DELETE FROM booking.v_tenant_billing_status")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, name, *, kind="demo", status="active", reason=None) -> str:
    tid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active, kind)"
        " VALUES ($1::uuid, $2, $3, 'starter'::plan_tier_enum, 3, 60, true, $4)",
        tid, name, f"zz1037-{tid[:12]}", kind)
    await conn.execute("UPDATE shared.tenants SET account_status = $2, suspend_reason = $3 WHERE tenant_id = $1::uuid",
                       tid, status, reason)
    return tid


async def _status(conn, tid, status, days=0, paid_through=None):
    await conn.execute(
        "INSERT INTO booking.v_tenant_billing_status VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, 1, 30)",
        tid, status, days, 1 if days else 0, '{"USD": 120.00}' if days else "{}", paid_through)


async def _account(conn, tid):
    return tuple(await conn.fetchrow("SELECT account_status, suspend_reason FROM shared.tenants "
                                     "WHERE tenant_id = $1::uuid", tid))


async def _bells(conn, tid, event):
    return await conn.fetchval("SELECT count(*) FROM shared.notifications WHERE tenant_id = $1::uuid "
                               "AND event_type = $2", tid, event)


@pytest.mark.asyncio
async def test_without_the_view_the_tick_skips(conn):
    await conn.execute("DROP TABLE booking.v_tenant_billing_status")
    out = await billing.tick(_Pool(conn), job_id="j0")          # the outer transaction is rolled back after
    assert out["outcome"] == "skipped"


@pytest.mark.asyncio
async def test_warn_suspend_and_restore(conn, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    warned = await _tenant(conn, "Zephyr Trails synthetic")
    late = await _tenant(conn, "Larkspur Journeys synthetic")
    paid = await _tenant(conn, "Quillfeather Tours synthetic", status="suspended", reason="billing")
    held = await _tenant(conn, "Marigold Coast synthetic", status="suspended", reason="compliance")
    await _status(conn, warned, "warn", 5)
    await _status(conn, late, "past_due", 31)
    await _status(conn, paid, "ok", 0, date(2099, 1, 31))
    await _status(conn, held, "ok")

    out = await billing.tick(_Pool(conn), job_id="j1")
    assert (out["warned"], out["suspended"], out["restored"], out["failed"]) == (1, 1, 1, 0)
    assert await _account(conn, warned) == ("past_due", None)
    assert await _account(conn, late) == ("suspended", "billing")
    assert await _account(conn, paid) == ("active", None)
    assert await _account(conn, held) == ("suspended", "compliance")
    assert await conn.fetchval("SELECT paid_through FROM shared.tenant_subscription WHERE tenant_id = $1::uuid "
                               "AND product = 'content'", paid) == date(2099, 1, 31)
    assert await _bells(conn, warned, "tenant.billing.overdue") == 1
    assert await _bells(conn, late, "tenant.billing.suspended") == 1
    roles = await conn.fetchval("SELECT target_roles FROM shared.notifications WHERE tenant_id = $1::uuid", late)
    assert set(roles) == {"tenant", "admin", "finance"}
    actor = await conn.fetchval("SELECT actor FROM shared.tenant_account_event WHERE tenant_id = $1::uuid "
                                "AND type = 'account.status_changed' ORDER BY seq DESC LIMIT 1", late)
    assert actor == "job:tenant_billing_tick:j1"

    again = await billing.tick(_Pool(conn), job_id="j2")          # the same status: nothing moves, one bell a day
    assert (again["warned"], again["suspended"], again["restored"]) == (0, 0, 0)
    assert await _bells(conn, warned, "tenant.billing.overdue") == 1


@pytest.mark.asyncio
async def test_dev_moves_synthetic_tenants_only(conn, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    real = await _tenant(conn, "Saffron Bay agency", kind="agency")
    synthetic = await _tenant(conn, "Driftwood Isles synthetic")
    await _status(conn, real, "past_due", 40)
    await _status(conn, synthetic, "past_due", 40)
    out = await billing.tick(_Pool(conn), job_id="j3")
    assert out["skipped_real"] == 1 and out["suspended"] == 1
    assert await _account(conn, real) == ("active", None)
    assert await _account(conn, synthetic) == ("suspended", "billing")
