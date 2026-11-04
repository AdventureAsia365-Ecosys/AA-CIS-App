"""AA-1036 R3 (root ADR 0019 d7) — add-on requests (services/tenant_account/addon_requests.py) on real Postgres.

One outer transaction per test, rolled back (the service's own transactions become savepoints). Covers the whole flow
the agency app and Tenant 360 drive:

  * the owner asks for the CRM: one pending row, an audit row, one staff bell row (admin + customer_success) that the
    admin bell scope shows and the tenant bell does not; asking again answers the open request (no second row, no
    second bell row);
  * the tenant's add-on list: requested / available, the last decision with its reason after a decline;
  * a decline needs a reason and is set once;
  * an approval checks the plan, enqueues ONE tenant_provision attach job (request key addon-request:<id>) in the same
    transaction and records it; running that saga attaches the plan, the add-on reads active, and a new request is
    refused (already_subscribed);
  * an account that is not in service cannot ask.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers.admin import notification_scope  # noqa: E402
from services.tenant_account import addon_requests as ar  # noqa: E402
from services.tenant_account import provision as pv  # noqa: E402
from services.tenant_account.plans import PlanError  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
OWNER = "membership:6f1d2c3b-0000-4000-8000-00000000a036"
STAFF = "admin:7e2a9c11-0000-4000-8000-00000000a036"


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


async def _tenant(conn) -> str:
    req = pv.parse({"request_key": f"zz-a1036-{uuid.uuid4().hex[:12]}", "mode": "create",
                    "name": "Marigold Ferry Tours", "slug": f"zz1036-{uuid.uuid4().hex[:10]}",
                    "plan_tier": "starter", "posts_per_week": 3, "kind": "demo"})
    out = await pv.provision(_Pool(conn), req, job_id=str(uuid.uuid4()))
    return out["tenant_id"]


async def _bell(conn, tenant_id, scope="TRUE"):
    return await conn.fetch(f"SELECT event_type, target_roles, payload FROM shared.notifications "
                            f"WHERE tenant_id = $1::uuid AND {scope}", tenant_id)


@pytest.mark.asyncio
async def test_a_request_is_stored_audited_and_rings_the_staff_bell_once(conn):
    tid = await _tenant(conn)
    row, created = await ar.create_request(conn, tid, "crm", "  We would like the CRM for our sales desk.  ",
                                           actor=OWNER)
    assert created is True and row["status"] == "pending" and row["product"] == "crm"
    assert row["note"] == "We would like the CRM for our sales desk."
    assert "requested_by" not in row and "decided_by" not in row     # the tenant never sees an actor
    again, created_again = await ar.create_request(conn, tid, "crm", None, actor=OWNER)
    assert created_again is False and again["request_id"] == row["request_id"]
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_addon_request WHERE tenant_id = $1::uuid",
                               tid) == 1
    stored = await conn.fetchrow("SELECT requested_by FROM shared.tenant_addon_request WHERE request_id = $1::uuid",
                                 row["request_id"])
    assert stored["requested_by"] == OWNER
    audit = await conn.fetch("SELECT action, actor, resource_id FROM acp_shared.audit_log "
                             "WHERE tenant_id = $1 AND action = 'tenant.addon_requested'", tid)
    assert [(a["actor"], a["resource_id"]) for a in audit] == [(OWNER, row["request_id"])]
    bell = await _bell(conn, tid)
    assert len(bell) == 1 and bell[0]["event_type"] == "tenant.addon.requested"
    assert sorted(bell[0]["target_roles"]) == ["admin", "customer_success"]
    payload = json.loads(bell[0]["payload"]) if isinstance(bell[0]["payload"], str) else bell[0]["payload"]
    assert payload["product"] == "crm" and "Marigold Ferry Tours" in payload["message"]
    assert "CRM add-on" in payload["message"]
    # The admin bell shows it; the tenant bell (rows targeted at 'tenant' only) does not.
    assert len(await _bell(conn, tid, notification_scope("admin"))) == 1
    assert len(await _bell(conn, tid, "target_roles @> ARRAY['tenant']::text[]")) == 0


@pytest.mark.asyncio
async def test_the_tenant_view_and_a_decline(conn):
    tid = await _tenant(conn)
    view = await ar.tenant_view(conn, tid)
    assert view["in_service"] is True
    assert [(a["product"], a["state"]) for a in view["add_ons"]] == [("crm", "available"), ("tripplanner", "available")]
    row, _ = await ar.create_request(conn, tid, "tripplanner", None, actor=OWNER)
    view = await ar.tenant_view(conn, tid)
    assert [(a["product"], a["state"]) for a in view["add_ons"]] == [("crm", "available"), ("tripplanner", "requested")]
    assert view["add_ons"][1]["request"]["request_id"] == row["request_id"]
    with pytest.raises(PlanError) as e:
        await ar.decline(conn, tid, row["request_id"], reason="   ", actor=STAFF)
    assert e.value.code == "reason_required"
    out = await ar.decline(conn, tid, row["request_id"], reason="Trip planner opens after the pilot.", actor=STAFF)
    assert out["status"] == "declined" and out["decided_by"] == STAFF
    with pytest.raises(PlanError) as e:
        await ar.decline(conn, tid, row["request_id"], reason="again", actor=STAFF)
    assert e.value.code == "already_decided" and e.value.status == 409
    tp = (await ar.tenant_view(conn, tid))["add_ons"][1]
    assert tp["state"] == "available" and tp["request"] is None
    assert tp["last_decision"]["status"] == "declined"
    assert tp["last_decision"]["decision_note"] == "Trip planner opens after the pilot."
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1 "
                               "AND action = 'tenant.addon_request_declined'", tid) == 1
    staff = await ar.staff_list(conn, tid)
    assert [r["status"] for r in staff] == ["declined"] and staff[0]["decided_by"] == STAFF


@pytest.mark.asyncio
async def test_an_approval_attaches_the_plan_through_the_saga(conn):
    tid = await _tenant(conn)
    row, _ = await ar.create_request(conn, tid, "crm", None, actor=OWNER)
    with pytest.raises(PlanError) as e:
        await ar.approve(conn, tid, row["request_id"], plan_key="tripplanner.hosted", seats=None, note=None,
                         actor=STAFF, staff_id=None)
    assert e.value.code == "unknown_plan"                      # a plan of another product
    with pytest.raises(PlanError) as e:
        await ar.approve(conn, tid, str(uuid.uuid4()), plan_key="crm.core", seats=3, note=None, actor=STAFF,
                         staff_id=None)
    assert e.value.code == "request_not_found" and e.value.status == 404
    out, job_id = await ar.approve(conn, tid, row["request_id"], plan_key="crm.core", seats=3,
                                   note="Welcome aboard", actor=STAFF, staff_id=None)
    assert out["status"] == "approved" and out["plan_key"] == "crm.core" and out["provision_job_id"] == job_id
    job = await conn.fetchrow("SELECT kind, status, payload, idempotency_key FROM shared.job WHERE id = $1::uuid",
                              job_id)
    assert job["kind"] == "tenant_provision" and job["status"] == "queued"
    assert job["idempotency_key"] == f"tenant_provision:addon-request:{row['request_id']}"
    payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
    assert payload["mode"] == "attach" and payload["tenant_id"] == tid
    assert payload["add_ons"] == [{"product": "crm", "plan_key": "crm.core", "seats": 3}]
    with pytest.raises(PlanError) as e:
        await ar.approve(conn, tid, row["request_id"], plan_key="crm.core", seats=3, note=None, actor=STAFF,
                         staff_id=None)
    assert e.value.code == "already_decided"
    # The worker runs the saga: the add-on is live, the tenant sees it active and cannot ask again.
    await pv.provision(_Pool(conn), pv.parse(payload), job_id=job_id)
    crm = (await ar.tenant_view(conn, tid))["add_ons"][0]
    assert crm["state"] == "active" and crm["subscription_status"] == "active"
    assert crm["last_decision"]["status"] == "approved" and crm["last_decision"]["decision_note"] == "Welcome aboard"
    with pytest.raises(PlanError) as e:
        await ar.create_request(conn, tid, "crm", None, actor=OWNER)
    assert e.value.code == "already_subscribed" and e.value.status == 409
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1 "
                               "AND action = 'tenant.addon_request_approved'", tid) == 1


@pytest.mark.asyncio
async def test_an_account_out_of_service_cannot_ask(conn):
    tid = await _tenant(conn)
    await conn.execute("UPDATE shared.tenants SET account_status = 'suspended', suspend_reason = 'billing' "
                       "WHERE tenant_id = $1::uuid", tid)
    with pytest.raises(PlanError) as e:
        await ar.create_request(conn, tid, "crm", None, actor=OWNER)
    assert e.value.code == "tenant_not_in_service" and e.value.status == 409
    assert (await ar.tenant_view(conn, tid))["in_service"] is False


async def _request_row(conn, request_id):
    return dict(await conn.fetchrow(
        "SELECT tenant_id::text AS tenant_id, status, decided_at, decided_by, decision_note, plan_key, "
        "provision_job_id FROM shared.tenant_addon_request WHERE request_id = $1::uuid", request_id))


@pytest.mark.asyncio
async def test_a_request_of_another_tenant_is_not_found_and_stays_as_it_is(conn):
    """Review AA-1036: tenant B's request id under /admin/tenants/{A}/… answers 404 request_not_found; B's row, the
    job queue and the audit trail are untouched."""
    tid_a, tid_b = await _tenant(conn), await _tenant(conn)
    row_b, _ = await ar.create_request(conn, tid_b, "crm", "B asks", actor=OWNER)
    before = await _request_row(conn, row_b["request_id"])
    jobs = await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'tenant_provision'")
    for decide in (
        lambda: ar.approve(conn, tid_a, row_b["request_id"], plan_key="crm.core", seats=None, note=None,
                           actor=STAFF, staff_id=None),
        lambda: ar.decline(conn, tid_a, row_b["request_id"], reason="not yours", actor=STAFF),
        lambda: ar.staff_one(conn, tid_a, row_b["request_id"]),
    ):
        with pytest.raises(PlanError) as e:
            await decide()
        assert e.value.code == "request_not_found" and e.value.status == 404
    after = await _request_row(conn, row_b["request_id"])
    assert after == before and after["tenant_id"] == tid_b and after["status"] == "pending"
    assert await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'tenant_provision'") == jobs
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = ANY($1::text[]) "
                               "AND action IN ('tenant.addon_request_approved', 'tenant.addon_request_declined')",
                               [tid_a, tid_b]) == 0
    assert await ar.staff_list(conn, tid_a) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "account_status = 'suspended', suspend_reason = 'billing'",
    "is_active = false",
    "cancelled_at = now(), account_status = 'offboarding'",
])
async def test_an_approval_for_an_account_out_of_service_is_refused_before_anything_is_written(conn, change):
    """Review AA-1036: approve() refuses 409 tenant_not_in_service (status, is_active or cancelled_at) and neither
    marks the request approved nor enqueues the saga."""
    tid = await _tenant(conn)
    row, _ = await ar.create_request(conn, tid, "crm", None, actor=OWNER)
    await conn.execute(f"UPDATE shared.tenants SET {change} WHERE tenant_id = $1::uuid", tid)
    t = await conn.fetchrow(ar.TENANT_SQL, tid)
    assert ar.in_service(t) is False
    jobs = await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'tenant_provision'")
    with pytest.raises(PlanError) as e:
        await ar.approve(conn, tid, row["request_id"], plan_key="crm.core", seats=None, note=None, actor=STAFF,
                         staff_id=None)
    assert e.value.code == "tenant_not_in_service" and e.value.status == 409
    stored = await _request_row(conn, row["request_id"])
    assert stored["status"] == "pending" and stored["provision_job_id"] is None
    assert await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'tenant_provision'") == jobs
    assert (await ar.tenant_view(conn, tid))["in_service"] is False
    # a decline still works (it attaches nothing)
    assert (await ar.decline(conn, tid, row["request_id"], reason="Account paused", actor=STAFF))["status"] == \
        "declined"


@pytest.mark.asyncio
async def test_an_approval_whose_setup_failed_says_so(conn):
    """Review AA-1036: the request reads 'approved, setup failed' from its job, for staff and for the agency."""
    tid = await _tenant(conn)
    row, _ = await ar.create_request(conn, tid, "tripplanner", None, actor=OWNER)
    out, job_id = await ar.approve(conn, tid, row["request_id"], plan_key="tripplanner.hosted", seats=None,
                                   note=None, actor=STAFF, staff_id=None)
    assert out["setup"] == "in_progress"
    one, status, _ = await ar.staff_one(conn, tid, row["request_id"])
    assert status == "queued" and one["setup"] == "in_progress" and one["provision_job_id"] == job_id
    await conn.execute("UPDATE shared.job SET status = 'failed', error = 'unknown_plan: no live plan' "
                       "WHERE id = $1::uuid", job_id)
    staff = (await ar.staff_list(conn, tid))[0]
    assert staff["status"] == "approved" and staff["setup"] == "failed"
    one, status, error = await ar.staff_one(conn, tid, row["request_id"])
    assert (status, one["setup"]) == ("failed", "failed") and error.startswith("unknown_plan")
    tp = (await ar.tenant_view(conn, tid))["add_ons"][1]
    assert tp["state"] == "available" and tp["last_decision"]["setup"] == "failed"
