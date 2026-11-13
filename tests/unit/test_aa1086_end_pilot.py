"""AA-1086 gap G2 (root ADR 0019 d16): ``POST /admin/tenants/{tenant_id}/pilot/end`` and
services/tenant_account/pilot.py without a database.

Administrator only (POLICY tenant.write, re-checked in the route); a reason is required; ending a pilot clears the
marker, writes one account event (status unchanged) and one audit row in one transaction; a tenant that is not the
pilot is a no-op 200 that writes nothing. The SQL runs as cis_app on Postgres in
tests/integration/test_aa1086_end_pilot.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.core.staff_authz import POLICY, TENANT_WRITE, denial_reason, principal_from_role
from api.routers.admin_tenant_subscription import EndPilotRequest, end_tenant_pilot
from services.acp_shared.audit_log import AdminTenantAuditAction
from services.tenant_account import pilot

SECRET = "test-admin-secret"
TID = "3c2b1a09-8f7e-4d6c-9b5a-4f3e2d1c0b0a"
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
ROUTE = ("POST", "/admin/tenants/{tenant_id}/pilot/end")


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _conn(row):
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Txn())
    conn.fetchrow = AsyncMock(return_value=row)
    return conn


def _request(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    request = MagicMock()
    request.app.state.pool.acquire = MagicMock(return_value=ctx)
    return request


def _row(is_pilot=True, ends_at=NOW + timedelta(days=10), status="active"):
    return {"tenant_id": TID, "name": "Tidewater Pilot Travel", "is_pilot": is_pilot,
            "pilot_ends_at": ends_at if is_pilot else None, "account_status": status, "suspend_reason": None}


def _sqls(conn):
    return [c.args[0] for c in conn.execute.call_args_list]


# ── policy ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_route_is_tenant_write():
    assert POLICY[ROUTE] == TENANT_WRITE


@pytest.mark.parametrize("role", ["viewer", "content", "content_lead", "customer_success", "support", "finance",
                                  "ai_ops", "tenant_qa"])
def test_only_the_administrator_passes_the_policy(role):
    assert denial_reason(principal_from_role(role), "POST", ROUTE[1]) is not None
    assert denial_reason(principal_from_role("admin"), "POST", ROUTE[1]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "customer_success", "content", None])
async def test_the_route_refuses_every_other_role(role):
    conn = _conn(_row())
    with pytest.raises(HTTPException) as e:
        await end_tenant_pilot(TID, EndPilotRequest(reason="pilot over"), _request(conn),
                               x_admin_secret=SECRET, x_admin_user_id="u-1", x_admin_role=role)
    assert e.value.status_code == 403
    conn.fetchrow.assert_not_awaited()
    conn.execute.assert_not_awaited()


# ── the service ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ending_a_pilot_clears_the_marker_emits_and_audits():
    conn = _conn(_row())
    out = await pilot.end_pilot(conn, TID, reason="  rehearsal finished  ", actor="admin:u-9", now=NOW)
    assert out == {"tenant_id": TID, "is_pilot": False, "changed": True,
                   "previous_pilot_ends_at": (NOW + timedelta(days=10)).isoformat(), "ended_early": True}
    sqls = _sqls(conn)
    assert sqls[0] == pilot.END_SQL and sqls[1] == pilot.ACTOR_SQL and sqls[2] == pilot.EMIT_SQL
    emit = conn.execute.call_args_list[2].args
    assert emit[1:] == (TID, "active", None)          # the status does not change: status = previous_status
    assert conn.execute.call_args_list[1].args[1] == "admin:u-9"
    audit = [c.args for c in conn.execute.call_args_list if "acp_shared.audit_log" in c.args[0]]
    assert len(audit) == 1
    _sql, tid, actor, action, rtype, _rid, details = audit[0]
    assert (tid, actor, action, rtype) == (TID, "admin:u-9", AdminTenantAuditAction.PILOT_ENDED, "tenant")
    assert action == "tenant.pilot_ended"
    d = json.loads(details)
    assert d["reason"] == "rehearsal finished" and d["ended_early"] is True and d["account_status"] == "active"


@pytest.mark.asyncio
async def test_a_pilot_past_its_end_is_not_early():
    conn = _conn(_row(ends_at=NOW - timedelta(days=1), status="offboarding"))
    out = await pilot.end_pilot(conn, TID, reason="slot needed", actor="admin:u-9", now=NOW)
    assert out["changed"] is True and out["ended_early"] is False
    assert conn.execute.call_args_list[2].args[2] == "offboarding"   # any status: an offboarded pilot frees the slot


@pytest.mark.asyncio
async def test_a_tenant_that_is_not_the_pilot_is_a_no_op():
    conn = _conn(_row(is_pilot=False))
    out = await pilot.end_pilot(conn, TID, reason="again", actor="admin:u-9", now=NOW)
    assert out == {"tenant_id": TID, "is_pilot": False, "changed": False, "previous_pilot_ends_at": None,
                   "ended_early": None}
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_tenant_is_not_found():
    with pytest.raises(pilot.PilotError) as e:
        await pilot.end_pilot(_conn(None), TID, reason="x", actor="admin:u-9")
    assert e.value.code == "tenant_not_found"


@pytest.mark.parametrize("reason,code", [("", "reason_required"), ("   ", "reason_required"), (None, "reason_required"),
                                         ("x" * 501, "reason_too_long")])
def test_the_reason_is_required_and_bounded(reason, code):
    with pytest.raises(pilot.PilotError) as e:
        pilot.clean_reason(reason)
    assert e.value.code == code
    assert pilot.clean_reason("x" * 500) == "x" * 500


# ── the route ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_route_ends_the_pilot_then_a_second_call_is_a_no_op():
    conn = _conn(_row())
    first = await end_tenant_pilot(TID, EndPilotRequest(reason="rehearsal teardown"), _request(conn),
                                   x_admin_secret=SECRET, x_admin_user_id="u-1", x_admin_role="admin")
    assert first.changed is True and first.is_pilot is False
    conn.fetchrow = AsyncMock(return_value=_row(is_pilot=False))
    conn.execute.reset_mock()
    second = await end_tenant_pilot(TID, EndPilotRequest(reason="rehearsal teardown"), _request(conn),
                                    x_admin_secret=SECRET, x_admin_user_id="u-1", x_admin_role="admin")
    assert second.changed is False and second.is_pilot is False
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("row,reason,status,code", [
    (None, "teardown", 404, "tenant_not_found"),
    (_row(), "   ", 422, "reason_required"),
])
async def test_the_route_maps_refusals(row, reason, status, code):
    with pytest.raises(HTTPException) as e:
        await end_tenant_pilot(TID, EndPilotRequest(reason=reason), _request(_conn(row)),
                               x_admin_secret=SECRET, x_admin_user_id="u-1", x_admin_role="admin")
    assert e.value.status_code == status and e.value.detail["detail"] == code


@pytest.mark.asyncio
async def test_the_route_refuses_a_bad_id_and_a_bad_secret():
    with pytest.raises(HTTPException) as e:
        await end_tenant_pilot("not-a-uuid", EndPilotRequest(reason="x"), _request(_conn(_row())),
                               x_admin_secret=SECRET, x_admin_user_id="u-1", x_admin_role="admin")
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await end_tenant_pilot(TID, EndPilotRequest(reason="x"), _request(_conn(_row())),
                               x_admin_secret="wrong", x_admin_user_id="u-1", x_admin_role="admin")
    assert e.value.status_code == 403
