"""AA-1116 (S282 gaps G5 / G11) on real Postgres — a new tenant with a crm add-on gets its CRM agency and its first
owner invited in the same staff request (services/tenant_account/crm_onboarding.py, the provisioning routes in
api/routers/admin_tenant_provision.py), against a fake crm.ops.v1 relay (no network).

  * create with crm: the saga's transaction run by the route under the job's id, then run again by the job, gives one
    tenant (the job's run is ``resumed``); the onboarding reads the live crm subscription, creates the agency, invites
    the owner, writes ONE account.status_changed event (status unchanged, the CRM pull's nudge) and ONE
    tenant.crm_onboarded audit row whose details never hold the address;
  * a second onboarding sends nothing new and writes nothing;
  * a tenant whose crm add-on is canceled is refused before any CRM call.

Each test runs on one connection inside an outer transaction that is rolled back. Invented names only.
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
from services.tenant_account import crm_onboarding as co  # noqa: E402
from services.tenant_account import provision as pv  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
OWNER = "owner@quillmoor-trails.example.test"
ACTOR = "admin:zz-aa1116-crm"


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
    await c.execute("ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS plan_id uuid")  # as test_aa1028_*
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


class FakeCrm:
    def __init__(self, tid, *, exists=False, invite=(202, {"status": "pending"})):
        self.tid, self.exists, self.invite = tid, exists, invite
        self.calls = []

    async def __call__(self, method, path, authorization, *, json=None, params=None):
        self.calls.append((method, path))
        if method == "GET":
            return (200, {"tenant_id": self.tid}) if self.exists else (404, {"detail": "not_found"})
        if path == "/tenants":
            self.exists = True
            return 201, {"tenant_id": self.tid}
        return self.invite


def _create(**kw):
    body = {"request_key": f"zz-1116-{uuid.uuid4().hex[:12]}", "mode": "create", "name": "Quillmoor Trails synthetic",
            "slug": f"zz1116-{uuid.uuid4().hex[:10]}", "plan_tier": "starter", "posts_per_week": 3,
            "add_ons": [{"product": "crm", "plan_key": "crm.core", "seats": 3}]}
    body.update(kw)
    return pv.parse(body)


async def _counts(conn, tid):
    events = await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_account_event WHERE tenant_id = $1::uuid "
                                 "AND type = 'account.status_changed'", tid)
    audits = await conn.fetch("SELECT details FROM acp_shared.audit_log WHERE tenant_id = $1 "
                              "AND action = 'tenant.crm_onboarded'", tid)
    return int(events), [json.loads(a["details"]) if isinstance(a["details"], str) else a["details"] for a in audits]


@pytest.mark.asyncio
async def test_create_with_crm_onboards_once(conn):
    pool = _Pool(conn)
    req, job_id = _create(), str(uuid.uuid4())
    first = await pv.provision(pool, req, job_id=job_id)            # the route, in the staff request
    again = await pv.provision(pool, req, job_id=job_id)            # the job, later
    assert again["resumed"] is True and again["tenant_id"] == first["tenant_id"]
    tid = first["tenant_id"]
    assert "crm_subscribed" in first["steps"]
    events_before, _ = await _counts(conn, tid)

    crm = FakeCrm(tid)
    out = await co.onboard(pool, tid, owner_email=OWNER, authorization="Bearer staff", actor=ACTOR,
                           ticket="AA-1116", request_key=req.request_key, call=crm)
    assert out["status"] == "done" and out["steps"] == ["crm_agency_created", "crm_owner_invited"]
    events, audits = await _counts(conn, tid)
    assert events == events_before + 1
    assert len(audits) == 1 and audits[0]["owner_invite"] == "sent" and OWNER not in json.dumps(audits[0])
    nudge = await conn.fetchrow("SELECT change, actor FROM shared.tenant_account_event WHERE tenant_id = $1::uuid "
                                "AND type = 'account.status_changed' ORDER BY seq DESC LIMIT 1", tid)
    change = json.loads(nudge["change"]) if isinstance(nudge["change"], str) else nudge["change"]
    assert change["status"] == change["previous_status"] and nudge["actor"] == ACTOR

    crm.invite = (409, {"detail": "owner_invite_pending"})
    out = await co.onboard(pool, tid, owner_email=OWNER, authorization="Bearer staff", actor=ACTOR, call=crm)
    assert out["steps"] == ["crm_agency_exists", "crm_owner_invite_pending"]
    assert await _counts(conn, tid) == (events, audits)


@pytest.mark.asyncio
async def test_a_canceled_crm_add_on_is_refused_before_any_crm_call(conn):
    pool = _Pool(conn)
    first = await pv.provision(pool, _create(), job_id=str(uuid.uuid4()))
    tid = first["tenant_id"]
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'canceled', canceled_at = now(), "
                       "updated_by = 'test:aa1116' WHERE tenant_id = $1::uuid AND product = 'crm'", tid)
    crm = FakeCrm(tid)
    with pytest.raises(co.OnboardingError) as e:
        await co.onboard(pool, tid, owner_email=OWNER, authorization="Bearer staff", actor=ACTOR, call=crm)
    assert e.value.code == "no_crm_subscription" and crm.calls == []
