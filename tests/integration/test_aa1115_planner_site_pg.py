"""AA-1115 (root ADR 0019 d10) — the tripplanner add-on's ``planner`` (services/tenant_account/planner_site.py) on real
Postgres.

One outer transaction per test, rolled back. The add-on goes live the way staff turn it on (a request, an approval of
``tripplanner.hosted``, the attach saga), then ``planner_site.read`` answers the hosted URL from the tenant slug, the
launcher flag from the resolved entitlements (tp.hosted + tp.launcher, CIS 355) and only the ACTIVE custom planner
hosts (CIS 428), sorted. Invented names only.
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
from services.tenant_account import addon_requests as ar  # noqa: E402
from services.tenant_account import planner_site as ps  # noqa: E402
from services.tenant_account import provision as pv  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
OWNER = "membership:6f1d2c3b-0000-4000-8000-00000000a115"
STAFF = "admin:7e2a9c11-0000-4000-8000-00000000a115"


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


async def _tenant(conn) -> tuple[str, str]:
    slug = f"zz1115-{uuid.uuid4().hex[:10]}"
    req = pv.parse({"request_key": f"zz-a1115-{uuid.uuid4().hex[:12]}", "mode": "create",
                    "name": "Quillmere Voyages", "slug": slug, "plan_tier": "starter", "posts_per_week": 3,
                    "kind": "demo"})
    out = await pv.provision(_Pool(conn), req, job_id=str(uuid.uuid4()))
    return out["tenant_id"], slug


async def _turn_on_the_planner(conn, tid):
    row, _ = await ar.create_request(conn, tid, "tripplanner", None, actor=OWNER)
    _, job_id = await ar.approve(conn, tid, row["request_id"], plan_key="tripplanner.hosted", seats=None, note=None,
                                 actor=STAFF, staff_id=None)
    job = await conn.fetchrow("SELECT payload FROM shared.job WHERE id = $1::uuid", job_id)
    payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
    await pv.provision(_Pool(conn), pv.parse(payload), job_id=job_id)


async def _host(conn, tid, domain, status):
    await conn.execute("INSERT INTO shared.tenant_tripplanner_domain (tenant_id, domain, status, updated_by) "
                       "VALUES ($1::uuid, $2, $3, 'test:aa1115')", tid, domain, status)


@pytest.mark.asyncio
async def test_no_hosted_planner_before_the_add_on_is_on(conn):
    tid, _ = await _tenant(conn)
    assert await ps.read(conn, tid, in_service=True) is None
    assert await ps.read(conn, str(uuid.uuid4()), in_service=True) is None     # an unknown tenant


@pytest.mark.asyncio
async def test_an_active_planner_answers_its_url_launcher_and_active_hosts(conn, monkeypatch):
    monkeypatch.delenv(ps.ORIGIN_ENV, raising=False)
    tid, slug = await _tenant(conn)
    await _turn_on_the_planner(conn, tid)
    other, _ = await _tenant(conn)
    await _host(conn, tid, "trips.quillmere.example", "active")
    await _host(conn, tid, "book.quillmere.example", "active")
    await _host(conn, tid, "new.quillmere.example", "validating")
    await _host(conn, other, "trips.ottervale.example", "active")              # another tenant's host
    view = await ar.tenant_view(conn, tid)
    assert [(a["product"], a["state"]) for a in view["add_ons"]][1] == ("tripplanner", "active")
    site = await ps.read(conn, tid, in_service=view["in_service"])
    assert site == {"url": f"{ps.DEFAULT_ORIGIN}/{slug}/plan",
                    "domains": ["book.quillmere.example", "trips.quillmere.example"], "launcher": True}
    # out of service: TripPlanner answers 404 for the slug, so no address is offered
    assert await ps.read(conn, tid, in_service=False) is None
