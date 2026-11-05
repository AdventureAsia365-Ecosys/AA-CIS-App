"""AA-1037 (root ADR 0019 d9 "Screens now") — Tenant 360 › Billing › Booking invoices:
GET /admin/tenants/{tenant_id}/booking-billing (api/routers/admin_tenant360.py) over
services/tenant_account/billing.tenant_snapshot.

Mocked asyncpg (same pool / conn shape as test_aa766_tenant360.py). Covers: the answer shape (view row + CIS account +
live subscriptions, no invoice line or bill-to), the overdue amounts kept to ISO codes and finite numbers, no Booking
row -> booking null, the view unreadable -> available false with the CIS side still filled, a missing tenant -> 404,
and who may call it (the Billing tab's rule: admin, finance, customer_success; never support or a content role)."""
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import HTTPException

from api.core.staff_authz import POLICY, denial_reason, principal_from_role
from api.core.tenant_scope import TENANT_SCOPED
from api.routers import admin_tenant360 as t360
from services.tenant_account import billing

_SECRET = "test-admin-secret"
ROUTE = "/admin/tenants/{tenant_id}/booking-billing"
TENANT_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _SECRET)


def _request(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    request = MagicMock()
    request.app.state.pool = pool
    return request


VIEW_ROW = {"status": "warn", "oldest_overdue_days": 9, "overdue_invoices": 2,
            "overdue_amount": '{"USD": 120.50, "EUR": "80.00"}', "paid_through": date(2026, 11, 30),
            "warn_after_days": 7, "suspend_after_days": 30}


def _conn(*, tenant=True, view=VIEW_ROW, view_error=None):
    conn = AsyncMock()

    async def fetchrow(sql, *args):
        if "FROM shared.tenants WHERE tenant_id = $1" in sql and "plan_tier" in sql:   # _require_tenant
            return {"tenant_id": TENANT_ID, "name": "Juniper Lane Travel", "plan_tier": "starter"} if tenant else None
        if sql == billing.TENANT_ACCOUNT_SQL:
            return {"account_status": "past_due", "suspend_reason": None}
        if sql == billing.TENANT_STATUS_SQL:
            if view_error is not None:
                raise view_error("relation does not exist")
            return view
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(sql, *args):
        assert sql == billing.LIVE_SUBSCRIPTIONS_SQL, sql
        return [{"product": "content", "status": "active", "paid_through": date(2026, 11, 30)},
                {"product": "crm", "status": "trialing", "paid_through": None}]

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(side_effect=fetch)
    return conn


async def _get(conn):
    return await t360.get_tenant_booking_billing(TENANT_ID, _request(conn), x_admin_secret=_SECRET)


# ── the answer ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_answer_has_the_view_row_and_the_cis_side():
    out = await _get(_conn())
    assert out == {
        "tenant_id": str(TENANT_ID),
        "available": True,
        "booking": {"status": "warn", "oldest_overdue_days": 9, "overdue_invoices": 2,
                    "overdue_amount": {"EUR": "80.00", "USD": "120.50"}, "paid_through": "2026-11-30",
                    "warn_after_days": 7, "suspend_after_days": 30},
        "account": {"account_status": "past_due", "suspend_reason": None},
        "subscriptions": [{"product": "content", "status": "active", "paid_through": "2026-11-30"},
                          {"product": "crm", "status": "trialing", "paid_through": None}],
    }


@pytest.mark.asyncio
async def test_no_booking_row_gives_booking_null():
    out = await _get(_conn(view=None))
    assert out["available"] is True and out["booking"] is None
    assert out["account"]["account_status"] == "past_due"


@pytest.mark.parametrize("error", [asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError,
                                   asyncpg.InvalidSchemaNameError])
@pytest.mark.asyncio
async def test_view_unreadable_is_fail_soft(error):
    out = await _get(_conn(view_error=error))
    assert out["available"] is False and out["booking"] is None
    assert len(out["subscriptions"]) == 2, "the CIS side is still filled"


@pytest.mark.asyncio
async def test_another_database_error_is_not_swallowed():
    with pytest.raises(asyncpg.UndefinedColumnError):
        await _get(_conn(view_error=asyncpg.UndefinedColumnError))


@pytest.mark.asyncio
async def test_missing_tenant_is_404_before_any_billing_read():
    conn = _conn(tenant=False)
    with pytest.raises(HTTPException) as e:
        await _get(conn)
    assert e.value.status_code == 404
    assert conn.fetch.await_count == 0


@pytest.mark.asyncio
async def test_wrong_secret_is_refused():
    with pytest.raises(HTTPException) as e:
        await t360.get_tenant_booking_billing(TENANT_ID, _request(_conn()), x_admin_secret="nope")
    assert e.value.status_code in (401, 403)


def test_the_sql_reads_no_invoice_line_bill_to_or_person():
    sql = " ".join([billing.TENANT_STATUS_SQL, billing.TENANT_ACCOUNT_SQL, billing.LIVE_SUBSCRIPTIONS_SQL]).lower()
    assert "booking.v_tenant_billing_status" in sql
    for word in ("agency_invoice", "invoice_line", "bill_to", "email", "billing_ref", "recipient"):
        assert word not in sql, word


# ── overdue amounts ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, want", [
    ({"USD": 120}, {"USD": "120"}),
    ({"VND": "1500000", "USD": Decimal("12.30")}, {"USD": "12.30", "VND": "1500000"}),
    ('{"USD": 5.5}', {"USD": "5.5"}),
    ({"usd": 1, "US": 2, "<b>": 3, "USD": "NaN", "EUR": True, "GBP": "x", "THB": None}, {}),
    ("not json", {}),
    (None, {}),
    ("[1, 2]", {}),
])
def test_overdue_amounts(value, want):
    assert billing.overdue_amounts(value) == want


def test_booking_block_none():
    assert billing.booking_block(None) is None


# ── who may call it ─────────────────────────────────────────────────────────────────────────────

def test_policy_is_the_billing_tab_rule():
    assert POLICY[("GET", ROUTE)] == POLICY[("GET", "/admin/tenants/{tenant_id}/subscription")]


@pytest.mark.parametrize("role, allowed", [
    ("admin", True), ("finance", True), ("customer_success", True),
    ("support", False), ("content", False), ("content_lead", False), ("tenant_qa", False), ("ai_ops", False),
    ("viewer", False),
])
def test_roles(role, allowed):
    assert (denial_reason(principal_from_role(role), "GET", ROUTE) is None) is allowed


def test_route_is_tenant_scoped():
    """require_tenant_scope narrows customer_success to its book (the integration test proves the 403)."""
    route = next(r for r in t360.router.routes if getattr(r, "path", "") == ROUTE)
    assert all(d in route.dependencies for d in TENANT_SCOPED)
