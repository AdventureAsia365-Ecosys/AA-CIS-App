"""AA-1160 — CIS records the tenants the CRM holds an agency for (services/tenant_account/crm_agency.py, CIS 475).

  * ``record``: one throttled upsert, a known source only; ``record_quietly`` never raises (no pool, a failing write);
  * the tenant.entitlements.v1 snapshot read by the CRM client records ``crm_pull``; a 404 records nothing; a failing
    marker write never fails the read;
  * the agency app's Trip planner module (``module_view``) says crm only with the add-on AND a CRM agency the CRM proved
    within cis.tenant.crm_agency_seen_hours, else the agency's own route (S285 live gap: crm.core without an agency
    showed "Now: CRM" and its leads were skipped; a deleted agency must age out, not stay crm forever);
  * Tenant 360's lead route answers ``crm_agency`` under the same window.
Invented names only.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core import m2m_auth
from api.routers.tenant_account import router
from services.tenant_account import crm_agency
from services.tenant_account import tripplanner_site as tp
from tests.unit import test_aa1028_tenant_account_contract as t1028
from tests.unit.aa976_m2m import install_m2m

TENANT = t1028.TENANT


class MarkerConn:
    def __init__(self, status: str = "INSERT 0 1", fail: Optional[BaseException] = None) -> None:
        self.status, self.fail = status, fail
        self.executed: list[tuple] = []

    async def execute(self, sql: str, *args: Any) -> str:
        if self.fail:
            raise self.fail
        self.executed.append((sql, args))
        return self.status


class Acquire:
    """asyncpg's PoolAcquireContext shape: awaitable (``await pool.acquire()``) and an async context manager."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def __await__(self):
        async def _c():
            return self.conn
        return _c().__await__()

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class Pool:
    """The snapshot read's pool (reader: ``await acquire`` / ``release``) and the marker's (``async with acquire``)."""

    def __init__(self, reader_conn: Any, marker_conn: MarkerConn) -> None:
        self.reader_conn, self.marker_conn, self.calls = reader_conn, marker_conn, 0

    def acquire(self, timeout: Optional[float] = None):
        self.calls += 1
        return Acquire(self.reader_conn if self.calls == 1 else self.marker_conn)

    async def release(self, conn: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(m2m_auth.ENV_M2M_ISSUER, raising=False)
    monkeypatch.delenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, raising=False)
    yield
    m2m_auth.set_m2m_verifier(None)


def _client(pool) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    app.state.pool = pool
    return TestClient(app)


def _read(client, issuer, tenant=TENANT):
    token = issuer.token(scope=m2m_auth.SCOPE_TENANT_READ)
    return client.get(f"/tenant/v1/tenants/{tenant}/account", headers={"Authorization": f"Bearer {token}"})


# ── record ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_record_upserts_one_throttled_row():
    conn = MarkerConn()
    assert asyncio.run(crm_agency.record(conn, TENANT, "crm_pull")) is True
    assert conn.executed == [(crm_agency.UPSERT_SQL, (TENANT, "crm_pull", float(crm_agency.REFRESH_AFTER_S)))]
    # The throttle is in the statement: a fresh sighting of the same source updates nothing ("INSERT 0 0").
    assert "last_seen_at < now() - make_interval(secs => $3)" in crm_agency.UPSERT_SQL
    assert asyncio.run(crm_agency.record(MarkerConn("INSERT 0 0"), TENANT, "crm_pull")) is False


def test_record_refuses_an_unknown_source():
    with pytest.raises(ValueError):
        asyncio.run(crm_agency.record(MarkerConn(), TENANT, "crm_guess"))


def test_record_quietly_never_raises():
    assert asyncio.run(crm_agency.record_quietly(None, TENANT, "crm_pull")) is False
    pool = Pool(None, MarkerConn(fail=RuntimeError("gone")))
    pool.calls = 1                                                           # the next acquire is the marker's
    with structlog.testing.capture_logs() as logs:
        assert asyncio.run(crm_agency.record_quietly(pool, TENANT, "crm_pull")) is False
    assert [e["event"] for e in logs] == ["crm_agency_marker_failed"] and logs[0]["error"] == "RuntimeError"


# ── the snapshot read ───────────────────────────────────────────────────────────────────────────────────────────

def test_the_crms_snapshot_read_records_the_agency():
    issuer, _ = install_m2m()
    marker = MarkerConn()
    r = _read(_client(Pool(t1028.FakeConn(), marker)), issuer)
    assert r.status_code == 200
    assert marker.executed == [(crm_agency.UPSERT_SQL, (TENANT, "crm_pull", float(crm_agency.REFRESH_AFTER_S)))]


def test_an_unknown_tenant_records_nothing():
    issuer, _ = install_m2m()
    marker = MarkerConn()
    r = _read(_client(Pool(t1028.FakeConn(), marker)), issuer, t1028.UNKNOWN_TENANT)
    assert r.status_code == 404 and marker.executed == []


def test_a_failing_marker_never_fails_the_read():
    issuer, _ = install_m2m()
    r = _read(_client(Pool(t1028.FakeConn(), MarkerConn(fail=RuntimeError("gone")))), issuer)
    assert r.status_code == 200 and r.json()["in_service"] is True


# ── the agency app's module and Tenant 360 ──────────────────────────────────────────────────────────────────────

class ModuleConn:
    def __init__(self, *, crm_core: bool, crm_agency_: bool, channel: str = "email") -> None:
        self.crm_core, self.agency, self.channel = crm_core, crm_agency_, channel
        self.args: dict[str, tuple] = {}

    async def fetchrow(self, sql: str, *args: Any):
        if sql == tp.MODULE_SQL:
            self.args["module"] = args
            return {"in_service": True, "crm_core": self.crm_core, "crm_agency": self.agency,
                    "ai_sessions_monthly": None, "domains": []}
        if sql == tp.ROUTE_SQL:
            return {"lead_channel": self.channel, "lead_webhook_url": None,
                    "lead_email": "desk@fernhollow.example.com" if self.channel == "email" else None,
                    "updated_at": None, "updated_by": "test"}
        if sql == tp.APPEARANCE_SQL:
            return {"theme": "{}", "locale": "vi"}
        raise AssertionError(sql)

    async def fetchval(self, sql: str, *args: Any):
        if sql == crm_agency.EXISTS_SQL:
            self.args["exists"] = args
            return self.agency
        return "active" if sql == tp.TENANT_SQL else False


@pytest.mark.parametrize("crm_core,agency,channel,effective", [
    (True, True, "email", "crm"),
    (True, False, "email", "email"),       # S285: the add-on without a CRM agency takes the agency's own route
    (True, False, "none", "none"),
    (False, True, "email", "email"),       # an agency left from an ended add-on: the route, never crm
])
def test_module_effective_channel_needs_a_crm_agency(crm_core, agency, channel, effective):
    view = asyncio.run(tp.module_view(ModuleConn(crm_core=crm_core, crm_agency_=agency, channel=channel), TENANT))
    assert view["crm_core"] is crm_core and view["lead_route"]["effective_channel"] == effective


@pytest.mark.parametrize("agency", [True, False])
def test_tenant360_lead_route_answers_crm_agency(agency):
    out = asyncio.run(tp.lead_route(ModuleConn(crm_core=True, crm_agency_=agency), TENANT))
    assert out["crm_agency"] is agency and out["lead_channel"] == "email"


def test_a_sighting_counts_only_inside_the_setting_window(monkeypatch):
    """A deleted or offboarded CRM agency is never read again: its row ages out after cis.tenant.crm_agency_seen_hours
    (an admin-editable setting, default 3), in both readers."""
    assert crm_agency.seen_hours() == 3                                     # the compiled default (CIS 475 seed)
    assert "a.last_seen_at > now() - make_interval(hours => $2)" in tp.MODULE_SQL
    assert "a.last_seen_at > now() - make_interval(hours => $2)" in crm_agency.EXISTS_SQL
    monkeypatch.setattr(crm_agency.settings, "get",
                        lambda key, tenant_id=None: 7 if key == "cis.tenant.crm_agency_seen_hours" else None)
    conn = ModuleConn(crm_core=True, crm_agency_=False)
    asyncio.run(tp.module_view(conn, TENANT))
    asyncio.run(tp.lead_route(conn, TENANT))
    assert conn.args["module"] == (TENANT, 7) and conn.args["exists"] == (TENANT, 7)
