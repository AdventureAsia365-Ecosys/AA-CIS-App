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


# ── AA-1036 review: an approval never changes a live add-on (tenant.book.write is not a billing write) ─────────────

CS = "admin:5c3e8d20-0000-4000-8000-00000000c036"          # the tenant's account manager (customer_success)


async def _sub(conn, tid, product="crm"):
    r = await conn.fetchrow("SELECT plan_key, status, seats FROM shared.tenant_subscription "
                            "WHERE tenant_id = $1::uuid AND product = $2", tid, product)
    return tuple(r) if r is not None else None


async def _attach(conn, tid, plan_key="crm.pro", seats=6, only_new=False):
    """Tenant 360 "Add an add-on" (an admin's attach saga) run by the worker; returns (result, job id)."""
    job_id = str(uuid.uuid4())
    body = {"request_key": f"zz-att-{uuid.uuid4().hex[:10]}", "mode": "attach", "tenant_id": tid,
            "add_ons": [{"product": "crm", "plan_key": plan_key, "seats": seats}]}
    if only_new:
        body["only_new"] = True
    return await pv.provision(_Pool(conn), pv.parse(body), job_id=job_id), job_id


@pytest.mark.asyncio
async def test_an_attach_closes_the_pending_request_and_the_account_manager_cannot_approve_it(conn):
    """A pending CRM request, then an admin attaches the CRM (crm.pro, 6 seats): the saga closes the request
    (approved, by the saga, with the live plan, audited); the account manager's approve with another plan and seats
    answers 409 and the live plan and seats stay as they are."""
    tid = await _tenant(conn)
    row, _ = await ar.create_request(conn, tid, "crm", None, actor=OWNER)
    out, job_id = await _attach(conn, tid)
    assert out["steps"] == ["crm_subscribed", "crm_request_closed"]
    stored = await _request_row(conn, row["request_id"])
    assert stored["status"] == "approved" and stored["plan_key"] == "crm.pro"
    assert stored["decided_by"] == pv.actor_for(job_id) and str(stored["provision_job_id"]) == job_id
    assert stored["decision_note"] == pv.REQUEST_CLOSED_NOTE
    audit = await conn.fetch("SELECT actor, resource_id, details FROM acp_shared.audit_log WHERE tenant_id = $1 "
                             "AND action = 'tenant.addon_request_approved'", tid)
    assert [(a["actor"], a["resource_id"]) for a in audit] == [(pv.actor_for(job_id), row["request_id"])]
    jobs = await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'tenant_provision'")
    with pytest.raises(PlanError) as e:
        await ar.approve(conn, tid, row["request_id"], plan_key="crm.core", seats=500, note=None, actor=CS,
                         staff_id=None)
    assert e.value.status == 409 and e.value.code == "already_decided"
    assert await _sub(conn, tid) == ("crm.pro", "active", 6)
    assert await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'tenant_provision'") == jobs
    crm = (await ar.tenant_view(conn, tid))["add_ons"][0]
    assert crm["state"] == "active" and crm["request"] is None and crm["last_decision"]["status"] == "approved"
    # a second attach of the same plan closes nothing more (no pending request left)
    again, _ = await _attach(conn, tid)
    assert again["steps"] == ["crm_unchanged"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,trial", [
    ("active", None), ("past_due", None), ("trialing", "now() + interval '5 days'"),
])
async def test_approving_a_request_whose_add_on_is_live_by_now_is_refused(conn, status, trial):
    """The add-on went live through a path that closes no request (here: written directly): approve answers 409
    already_subscribed before anything is written; the request stays pending (staff decline it)."""
    tid = await _tenant(conn)
    row, _ = await ar.create_request(conn, tid, "crm", None, actor=OWNER)
    await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, seats, "
                       "updated_by, trial_ends_at) "
                       f"VALUES ($1::uuid, 'crm', 'crm.pro', $2, 6, 'zz-test', {trial or 'NULL'})", tid, status)
    jobs = await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'tenant_provision'")
    with pytest.raises(PlanError) as e:
        await ar.approve(conn, tid, row["request_id"], plan_key="crm.core", seats=500, note=None, actor=CS,
                         staff_id=None)
    assert e.value.status == 409 and e.value.code == "already_subscribed"
    assert await _sub(conn, tid) == ("crm.pro", status, 6)
    stored = await _request_row(conn, row["request_id"])
    assert stored["status"] == "pending" and stored["provision_job_id"] is None
    assert await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'tenant_provision'") == jobs
    assert (await ar.decline(conn, tid, row["request_id"], reason="Already on", actor=CS))["status"] == "declined"


@pytest.mark.asyncio
async def test_an_approval_whose_add_on_went_live_before_its_job_ran_changes_nothing(conn):
    """Approved while the CRM is off (crm.core, 2 seats); an admin attaches crm.pro with 6 seats before the worker
    runs the approval's job: that job (only_new) leaves the live subscription as it is."""
    tid = await _tenant(conn)
    row, _ = await ar.create_request(conn, tid, "crm", None, actor=OWNER)
    _, job_id = await ar.approve(conn, tid, row["request_id"], plan_key="crm.core", seats=2, note=None, actor=CS,
                                 staff_id=None)
    payload = await conn.fetchval("SELECT payload FROM shared.job WHERE id = $1::uuid", job_id)
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["only_new"] is True
    admin, _ = await _attach(conn, tid)
    assert admin["steps"] == ["crm_subscribed"]                 # the request was approved already: nothing to close
    out = await pv.provision(_Pool(conn), pv.parse(payload), job_id=job_id)
    assert out["steps"] == ["crm_already_on"]
    assert await _sub(conn, tid) == ("crm.pro", "active", 6)
    # only_new still turns on an add-on that is not live (canceled; a trial that has ended)
    for change in ("status = 'canceled', canceled_at = now(), trial_ends_at = NULL",
                   "status = 'trialing', trial_ends_at = now() - interval '1 day', canceled_at = NULL"):
        await conn.execute(f"UPDATE shared.tenant_subscription SET {change} "
                           "WHERE tenant_id = $1::uuid AND product = 'crm'", tid)
        out, _ = await _attach(conn, tid, plan_key="crm.core", seats=2, only_new=True)
        assert out["steps"] == ["crm_subscribed"], change
        assert await _sub(conn, tid) == ("crm.core", "active", 2)


# ── tenant.addons.v1: the CRM relays its owner's request (M2M, scope cis.tenant/addons) ──────────────────────────

CRM_MEMBER = "2d3e4f5a-0000-4000-8000-00000000a036"


class _TimeoutPool(_Pool):
    """The route acquires with a timeout, as on asyncpg's Pool."""

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self._conn


def _m2m_app(conn):
    from fastapi import FastAPI

    from api.core import m2m_auth
    from api.routers.tenant_addons import router

    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    app.state.pool = _TimeoutPool(conn)
    return app


@asynccontextmanager
async def _m2m_client(conn, monkeypatch):
    import httpx

    from api.core import m2m_auth
    from tests.unit.aa976_m2m import install_m2m

    monkeypatch.delenv(m2m_auth.ENV_M2M_ISSUER, raising=False)
    monkeypatch.delenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, raising=False)
    issuer, _ = install_m2m()
    headers = {"Authorization": f"Bearer {issuer.token(scope=m2m_auth.SCOPE_TENANT_ADDONS)}"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_m2m_app(conn)), base_url="http://cis.test",
                                     headers=headers) as client:
            yield client
    finally:
        m2m_auth.set_m2m_verifier(None)


@pytest.mark.asyncio
async def test_the_crm_relays_its_owners_request_into_the_same_store(conn, monkeypatch):
    """An agency with no Content studio membership: the CRM asks for the Trip planner for its CRM owner. One row in
    shared.tenant_addon_request (requested_by crm-member:<id>), the CIS audit row and the staff bell row; asking again
    answers the open request; the studio's own list shows the same request."""
    tid = await _tenant(conn)
    async with _m2m_client(conn, monkeypatch) as client:
        body = {"product": "tripplanner", "note": "For our river season.", "crm_member_id": CRM_MEMBER}
        r = await client.post(f"/tenant/v1/tenants/{tid}/add-on-requests", json=body)
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["created"] is True and created["request"]["status"] == "pending"
        assert "requested_by" not in created["request"]
        again = await client.post(f"/tenant/v1/tenants/{tid}/add-on-requests", json={**body, "note": None})
        assert again.status_code == 200 and again.json()["created"] is False
        assert again.json()["request"]["request_id"] == created["request"]["request_id"]
        listed = await client.get(f"/tenant/v1/tenants/{tid}/add-ons")
    assert listed.status_code == 200
    planner = {a["product"]: a for a in listed.json()["add_ons"]}["tripplanner"]
    assert planner["state"] == "requested" and planner["request"]["note"] == "For our river season."
    actor = f"crm-member:{CRM_MEMBER}"
    rows = await conn.fetch("SELECT requested_by FROM shared.tenant_addon_request WHERE tenant_id = $1::uuid", tid)
    assert [r["requested_by"] for r in rows] == [actor]
    audit = await conn.fetch("SELECT actor FROM acp_shared.audit_log WHERE tenant_id = $1 "
                             "AND action = 'tenant.addon_requested'", tid)
    assert [a["actor"] for a in audit] == [actor]
    assert len(await _bell(conn, tid)) == 1
    studio = await ar.tenant_view(conn, tid)       # what the Content studio's Add-ons page reads
    assert {a["product"]: a["state"] for a in studio["add_ons"]}["tripplanner"] == "requested"


@pytest.mark.asyncio
async def test_the_relay_keeps_the_studio_rules(conn, monkeypatch):
    tid = await _tenant(conn)
    unknown = str(uuid.uuid4())
    async with _m2m_client(conn, monkeypatch) as client:
        r = await client.post(f"/tenant/v1/tenants/{unknown}/add-on-requests",
                              json={"product": "crm", "crm_member_id": CRM_MEMBER})
        assert r.status_code == 404 and r.json()["reason"] == "tenant_not_found"
        r = await client.get(f"/tenant/v1/tenants/{unknown}/add-ons")
        assert r.status_code == 404
        await conn.execute("UPDATE shared.tenants SET account_status = 'suspended', suspend_reason = 'billing' "
                           "WHERE tenant_id = $1::uuid", tid)
        r = await client.post(f"/tenant/v1/tenants/{tid}/add-on-requests",
                              json={"product": "crm", "crm_member_id": CRM_MEMBER})
        assert r.status_code == 409 and r.json()["reason"] == "tenant_not_in_service"
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_addon_request WHERE tenant_id = $1::uuid", tid) == 0
