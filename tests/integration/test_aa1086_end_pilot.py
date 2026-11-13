"""AA-1086 gap G2 on real Postgres: ending the Dev pilot as the runtime role (cis_app) frees the one pilot slot.

The pilot is made by the tenant_provision saga (``pilot_weeks``, Dev), then :func:`services.tenant_account.pilot.
end_pilot` runs with ``SET LOCAL ROLE cis_app``: the marker is cleared (``tenants_pilot_check`` holds), one
``account.status_changed`` event (status unchanged) and one ``tenant.pilot_ended`` audit row are written, a second
call writes nothing, and a new pilot is accepted again (``tenants_one_pilot_key``). The CI replay takes CIS 244's
grants off cis_app (tests/integration/conftest.py x1_244_undo), so the test grants the few privileges the runtime holds
on Dev (schema reference: shared.tenants, shared.tenant_account_event, acp_shared.audit_log) inside the outer
transaction, which is rolled back. Names are invented.
"""
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from test_aa1028_tenant_provision import _Pool, conn  # noqa: E402,F401 — fixture used by name
from services.tenant_account import pilot  # noqa: E402
from services.tenant_account import provision as pv  # noqa: E402

ACTOR = "admin:zz-aa1086-g2"

RUNTIME_GRANTS = """
    GRANT USAGE ON SCHEMA shared, acp_shared TO cis_app;
    GRANT SELECT, UPDATE ON shared.tenants TO cis_app;
    GRANT SELECT, INSERT ON shared.tenant_account_event TO cis_app;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA shared, acp_shared TO cis_app;
    GRANT EXECUTE ON FUNCTION shared.tenant_account_emit(text, uuid, jsonb) TO cis_app;
    GRANT SELECT, INSERT ON acp_shared.audit_log TO cis_app;
"""


async def _pilot(conn, weeks: int = 1) -> str:
    body = {"request_key": f"zz-1086-{uuid.uuid4().hex[:12]}", "mode": "create", "name": "Driftwood Marker Travel",
            "slug": f"zz1086-{uuid.uuid4().hex[:10]}", "plan_tier": "starter", "posts_per_week": 1,
            "kind": "agency", "pilot_weeks": weeks}
    out = await pv.provision(_Pool(conn), pv.parse(body), job_id=str(uuid.uuid4()))
    return out["tenant_id"]


async def _as_runtime(conn, coro_fn):
    """Run ``coro_fn(conn)`` as cis_app inside a savepoint, then back to the test role."""
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE cis_app")
        try:
            return await coro_fn(conn)
        finally:
            await conn.execute("RESET ROLE")


@pytest.mark.asyncio
async def test_end_pilot_as_cis_app_frees_the_slot(conn, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ENVIRONMENT", "dev")
    await conn.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE is_pilot")
    tid = await _pilot(conn)
    with pytest.raises(pv.ProvisionError) as e:
        await _pilot(conn)
    assert e.value.code == "pilot_taken"
    await conn.execute(RUNTIME_GRANTS)
    events_before = await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event WHERE tenant_id = $1::uuid",
                                        tid)

    out = await _as_runtime(conn, lambda c: pilot.end_pilot(c, tid, reason="rehearsal teardown", actor=ACTOR))
    assert out["changed"] is True and out["is_pilot"] is False and out["ended_early"] is True
    assert out["previous_pilot_ends_at"] is not None

    row = await conn.fetchrow("SELECT is_pilot AS is_pilot, pilot_ends_at AS ends, kind AS kind, "
                              "account_status AS status FROM shared.tenants WHERE tenant_id = $1::uuid", tid)
    assert (row["is_pilot"], row["ends"], row["kind"], row["status"]) == (False, None, "agency", "active")
    event = await conn.fetchrow(
        "SELECT type AS type, change AS change, actor AS actor FROM shared.tenant_account_event "
        "WHERE tenant_id = $1::uuid ORDER BY seq DESC LIMIT 1", tid)
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event WHERE tenant_id = $1::uuid",
                               tid) == events_before + 1
    assert event["type"] == "account.status_changed" and event["actor"] == ACTOR
    change = event["change"] if isinstance(event["change"], dict) else json.loads(event["change"])
    assert change == {"status": "active", "previous_status": "active", "suspend_reason": None}
    audit = await conn.fetch("SELECT actor AS actor, details->>'reason' AS reason FROM acp_shared.audit_log "
                             "WHERE tenant_id = $1 AND action = 'tenant.pilot_ended'", tid)
    assert [(a["actor"], a["reason"]) for a in audit] == [(ACTOR, "rehearsal teardown")]

    # a second call: 200-equivalent no-op, nothing written
    again = await _as_runtime(conn, lambda c: pilot.end_pilot(c, tid, reason="rehearsal teardown", actor=ACTOR))
    assert again["changed"] is False
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1 "
                               "AND action = 'tenant.pilot_ended'", tid) == 1
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event WHERE tenant_id = $1::uuid",
                               tid) == events_before + 1

    # the slot is free: a new pilot is accepted
    second = await _pilot(conn, weeks=2)
    assert await conn.fetchval("SELECT count(*) FROM shared.tenants WHERE is_pilot") == 1
    assert await conn.fetchval("SELECT is_pilot FROM shared.tenants WHERE tenant_id = $1::uuid", second) is True


@pytest.mark.asyncio
async def test_an_offboarded_pilot_still_frees_the_slot(conn, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ENVIRONMENT", "dev")
    await conn.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE is_pilot")
    tid = await _pilot(conn)
    await conn.execute("UPDATE shared.tenants SET account_status = 'offboarding', cancelled_at = now(), "
                       "is_active = false WHERE tenant_id = $1::uuid", tid)
    await conn.execute(RUNTIME_GRANTS)
    out = await _as_runtime(conn, lambda c: pilot.end_pilot(c, tid, reason="slot needed", actor=ACTOR))
    assert out["changed"] is True
    assert await conn.fetchval("SELECT count(*) FROM shared.tenants WHERE is_pilot") == 0
    change = await conn.fetchval("SELECT change->>'status' AS s FROM shared.tenant_account_event "
                                 "WHERE tenant_id = $1::uuid ORDER BY seq DESC LIMIT 1", tid)
    assert change == "offboarding"


@pytest.mark.asyncio
async def test_unknown_tenant(conn):  # noqa: F811
    await conn.execute(RUNTIME_GRANTS)
    with pytest.raises(pilot.PilotError) as e:
        await _as_runtime(conn, lambda c: pilot.end_pilot(c, str(uuid.uuid4()), reason="x", actor=ACTOR))
    assert e.value.code == "tenant_not_found"
