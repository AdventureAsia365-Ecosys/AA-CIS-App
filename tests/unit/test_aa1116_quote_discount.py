"""AA-1116 (contract tenant.entitlements.v1 1.10) — the quote discount share per subscription line
(services/tenant_account/quote_discount.py), no database: the pure share mapping from the one quote rule, the refresh
on a fake connection (only changed rows are written, a non-live row gets 0), the hourly run (each tenant on its own,
skipped before CIS 443, a failure counted) and the tenant_billing_tick job that runs it fail-soft.
Invented plan names only (the fixtures of test_aa1116_pricing).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import asyncpg
import pytest

from services.jobs import tenant_billing_tick_job as job
from services.tenant_account import billing_terms as bt
from services.tenant_account import quote as qt
from services.tenant_account import quote_discount as qd
from tests.unit.test_aa1116_pricing import CRM, GROWTH, STARTER, TERMS, TODAY, TP

TID = "1116d000-0000-4000-8000-000000000001"
FOUNDING = {"founding_offer_from": "2027-03-01", "founding_offer_until": "2028-02-29"}
ZERO = (Decimal("0.00"), None)


def _quote(tenant=None, subs=(GROWTH,)):
    return qt.build({"billing_currency": "USD", **(tenant or {})}, list(subs), TERMS, today=TODAY)


# ── the pure share ──────────────────────────────────────────────────────────────────────────────────────────────

def test_combo_gives_every_line_its_share():
    shares = qd.line_shares(_quote(subs=(GROWTH, CRM, TP)), ["content", "crm", "tripplanner"])
    assert shares == {p: (Decimal("10.00"), "combo") for p in ("content", "crm", "tripplanner")}


def test_founding_gives_eligible_lines_only():
    shares = qd.line_shares(_quote(FOUNDING, subs=(STARTER, CRM)), ["content", "crm"])
    assert shares == {"content": ZERO, "crm": (Decimal("30.00"), "founding")}


def test_founding_wins_over_combo_without_stacking():
    shares = qd.line_shares(_quote(FOUNDING, subs=(GROWTH, CRM, TP)), ["content", "crm", "tripplanner"])
    assert {k for k, (_, kind) in shares.items() if kind != "founding"} == set()
    assert all(pct == Decimal("30.00") for pct, _ in shares.values())


def test_no_discount_and_non_live_rows_are_zero():
    assert qd.line_shares(_quote(subs=(GROWTH,)), ["content"]) == {"content": ZERO}
    canceled = {**TP, "status": "canceled"}
    shares = qd.line_shares(_quote(subs=(GROWTH, CRM, canceled)), ["content", "crm", "tripplanner"])
    assert shares == {"content": ZERO, "crm": ZERO, "tripplanner": ZERO}      # no combo without a live tripplanner


# ── refresh on a fake connection ────────────────────────────────────────────────────────────────────────────────

class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, subs, stored, tenant=None, *, fail_on=None):
        self.tenant = {"billing_currency": "USD", "is_pilot": False, **(tenant or {})}
        self.subs = subs
        self.stored = stored
        self.writes: list[tuple] = []
        self.fail_on = fail_on

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        assert sql == bt.TENANT_SQL
        if self.fail_on and args[0] in self.fail_on:
            raise self.fail_on[args[0]]
        return self.tenant

    async def fetch(self, sql, *args):
        if sql == bt.SUBS_SQL:
            return self.subs
        if sql == qd.STORED_SQL:
            return [{"product": p, "quote_discount_pct": pct, "quote_discount_kind": kind}
                    for p, (pct, kind) in self.stored.items()]
        if sql == qd.TENANTS_SQL:
            return [{"tenant_id": t} for t in ("t-a", "t-b")]
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        assert sql == qd.WRITE_SQL
        self.writes.append(args)


def test_refresh_writes_only_the_rows_that_change():
    conn = FakeConn([GROWTH, CRM, TP], {"content": (Decimal("10.00"), "combo"), "crm": (0, None),
                                        "tripplanner": (Decimal("0"), None)})
    out = asyncio.run(qd.refresh(conn, TID, today=TODAY, terms=TERMS))
    assert out == {"tenant_id": TID, "changed": ["crm", "tripplanner"]}
    assert conn.writes == [(TID, "crm", Decimal("10.00"), "combo"), (TID, "tripplanner", Decimal("10.00"), "combo")]
    conn.stored = {p: (Decimal("10.00"), "combo") for p in ("content", "crm", "tripplanner")}
    conn.writes = []
    assert asyncio.run(qd.refresh(conn, TID, today=TODAY, terms=TERMS))["changed"] == []
    assert conn.writes == []


def test_refresh_clears_a_share_that_ended():
    # the founding offer is over: the stored share goes back to 0
    conn = FakeConn([CRM], {"crm": (Decimal("30.00"), "founding")},
                    {"founding_offer_from": "2026-01-01", "founding_offer_until": "2026-12-31"})
    asyncio.run(qd.refresh(conn, TID, today=TODAY, terms=TERMS))
    assert conn.writes == [(TID, "crm", Decimal("0.00"), None)]


def test_refresh_of_an_unknown_tenant_writes_nothing():
    conn = FakeConn([], {})
    conn.tenant = None

    async def none(sql, *a):
        return None
    conn.fetchrow = none
    assert asyncio.run(qd.refresh(conn, TID, today=TODAY, terms=TERMS)) == {"tenant_id": TID, "changed": []}


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Acq()


@pytest.fixture
def terms(monkeypatch):
    monkeypatch.setattr(qd.ps, "snapshot", lambda keys: TERMS)


def test_refresh_all_counts_and_survives_one_tenant(terms):
    conn = FakeConn([GROWTH, CRM, TP], {p: (0, None) for p in ("content", "crm", "tripplanner")},
                    fail_on={"t-a": RuntimeError("bad row")})
    out = asyncio.run(qd.refresh_all(FakePool(conn), today=TODAY))
    assert out == {"outcome": "ok", "tenants": 1, "changed": 3, "failed": 1}


def test_refresh_all_is_skipped_before_443(terms):
    conn = FakeConn([GROWTH], {}, fail_on={"t-a": asyncpg.UndefinedColumnError("quote_discount_pct")})
    out = asyncio.run(qd.refresh_all(FakePool(conn), today=TODAY))
    assert out["outcome"] == "skipped"


def test_refresh_quietly_never_raises(terms):
    conn = FakeConn([GROWTH], {}, fail_on={TID: RuntimeError("down")})
    assert asyncio.run(qd.refresh_quietly(conn, TID)) is None


def test_the_tenants_query_skips_internal():
    assert "t.kind <> 'internal'" in qd.TENANTS_SQL


# ── the hourly job ──────────────────────────────────────────────────────────────────────────────────────────────

class Ctx:
    def __init__(self):
        self.pool = object()
        self.job_id = "1116d000-0000-4000-8000-0000000000aa"
        self.payload = {"trigger": "schedule"}
        self.result = None

    def set_result(self, r):
        self.result = r


def test_the_tick_job_refreshes_the_discounts(monkeypatch):
    monkeypatch.setattr(job.billing, "tick", AsyncMock(return_value={"outcome": "ok"}))
    monkeypatch.setattr(job.quote_discount, "refresh_all",
                        AsyncMock(return_value={"outcome": "ok", "tenants": 2, "changed": 1, "failed": 0}))
    ctx = Ctx()
    out = asyncio.run(job.run(ctx))
    assert out["discounts"]["changed"] == 1 and out["outcome"] == "ok" and ctx.result == out


def test_a_discount_failure_never_fails_the_billing_move(monkeypatch):
    monkeypatch.setattr(job.billing, "tick", AsyncMock(return_value={"outcome": "ok", "warned": 1}))
    monkeypatch.setattr(job.quote_discount, "refresh_all", AsyncMock(side_effect=RuntimeError("down")))
    out = asyncio.run(job.run(Ctx()))
    assert out["discounts"] == {"outcome": "failed"} and out["warned"] == 1


# ── the Tenant 360 billing writes refresh the tenant at once ────────────────────────────────────────────────────
from tests.unit.test_aa1028_tenant_provision import _SECRET  # noqa: E402  (the admin secret of the route tests)


@pytest.fixture
def client(monkeypatch):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=True), conn


@pytest.mark.parametrize("method,path,service,body", [
    ("put", "billing-details", "set_details", {"billing_currency": "VND"}),
    ("put", "subscriptions/crm/terms", "set_terms", {"billing_interval": "year"}),
])
def test_billing_writes_refresh_the_discount(client, monkeypatch, method, path, service, body):
    c, conn = client
    monkeypatch.setattr(bt, service, AsyncMock(return_value={"ok": True}))
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(qd, "refresh_quietly", refresh)
    headers = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "u-admin", "x-admin-role": "admin"}
    resp = getattr(c, method)(f"/admin/tenants/{TID}/{path}", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert refresh.await_args.args == (conn, TID)
