"""AA-1114 on real Postgres — the TripPlanner custom-domain follow-up.

- The AA-subject gate (services/acp_shared/subject_gate.py) reads the tenant's OWN verified TripPlanner hosts from
  shared.tenant_tripplanner_domain (CIS 428): a booking link to its `active` host passes; its `pending` host and
  another tenant's `active` host are held (one open subject_gate flag).
- add_domain (services/tenant_account/tripplanner_domain.py) refuses a tenant whose TripPlanner plan does not carry
  tp.custom_domain (tripplanner.hosted, CIS 355 seed) with 409 custom_domain_not_included, and nothing is stored.

Each test runs in one rolled-back transaction (the seed helpers of test_aa986_l2_tour_lifecycle and the gate module).
Invented names and hosts only.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)
from test_aa1039_gate_and_fanout import _gate_piece, _open_gate_flags, _piece, _request  # noqa: E402

from services.acp_shared import subject_gate as sg  # noqa: E402
from services.tenant_account import tp_edge  # noqa: E402
from services.tenant_account import tripplanner_domain as tpd  # noqa: E402

ACTOR = "admin:zz-aa1114-domain"
OWN = "trip.quillbrook.example.invalid"
OWN_PENDING = "go.quillbrook.example.invalid"
OTHER = "trip.marrowfen.example.invalid"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(l2.DSN)
    tr = c.transaction()
    await tr.start()
    try:
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(l2.TTV_LIVE_STANDIN_DDL)
        # CIS 124 (pgvector) does not replay on the CI postgres:15 image (as in test_aa1039_gate_and_fanout)
        await c.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel text, "
                        "ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid")
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(tp_edge.DIST_ENV, "EDISTTEST1114")
    monkeypatch.delenv("TRIP_PAGE_BASE_URL", raising=False)
    monkeypatch.delenv("TRIPPLANNER_SITE_HOSTS", raising=False)
    monkeypatch.delenv("TRIPPLANNER_REVALIDATE_URL", raising=False)


async def _domain(conn, tenant, host, status):
    await conn.execute("INSERT INTO shared.tenant_tripplanner_domain (tenant_id, domain, status, updated_by) "
                       "VALUES ($1::uuid, $2, $3, 'test:aa1114')", tenant, host, status)


@pytest.mark.asyncio
async def test_gate_allows_the_tenants_own_active_host_only(conn):
    t = await l2._tour(conn, "aa1114-gate")
    tenant = await l2._tenant(conn, "aa1114-own")
    other = await l2._tenant(conn, "aa1114-other")
    await _domain(conn, tenant, OWN, "active")
    await _domain(conn, tenant, OWN_PENDING, "pending")
    await _domain(conn, other, OTHER, "active")

    assert await sg.tenant_tripplanner_hosts(conn, tenant) == {OWN}
    assert await sg.tenant_tripplanner_hosts(conn, other) == {OTHER}

    req = await _request(conn, tenant, trip=t.tour, goal="conversion")
    good = f"Three slow days by the lake. [Book your trip](https://{OWN}/plan) today."
    piece = await _piece(conn, tenant, req, good)
    res = await sg.run(conn, piece=_gate_piece(tenant, req, piece, good), stage="write", actor="test")
    assert res.ok, res.detail
    assert await _open_gate_flags(conn, piece) == []

    for host in (OWN_PENDING, OTHER, f"evil.{OWN}"):
        bad = f"Three slow days by the lake. [Book your trip](https://{host}/plan) today."
        res = await sg.run(conn, piece=_gate_piece(tenant, req, piece, bad), stage="publish", actor="test")
        assert not res.ok and res.codes == [sg.OFF_CATALOG_LINK], host
        assert len(await _open_gate_flags(conn, piece)) == 1


@pytest.mark.asyncio
async def test_a_verified_www_host_never_allows_the_apex(conn):
    """Review fix: the stored www. host is kept as verified, so the agency's apex (its own shop) stays held."""
    t = await l2._tour(conn, "aa1114-www")
    tenant = await l2._tenant(conn, "aa1114-www")
    await _domain(conn, tenant, "www.saltmere.example.invalid", "active")
    assert await sg.tenant_tripplanner_hosts(conn, tenant) == {"www.saltmere.example.invalid"}

    req = await _request(conn, tenant, trip=t.tour, goal="conversion")
    good = "Two quiet days on the coast. [Book your trip](https://www.saltmere.example.invalid/plan) today."
    piece = await _piece(conn, tenant, req, good)
    res = await sg.run(conn, piece=_gate_piece(tenant, req, piece, good), stage="write", actor="test")
    assert res.ok, res.detail

    bad = "Two quiet days on the coast. [Book your trip](https://saltmere.example.invalid/book) today."
    res = await sg.run(conn, piece=_gate_piece(tenant, req, piece, bad), stage="publish", actor="test")
    assert not res.ok and res.codes == [sg.OFF_CATALOG_LINK]


async def _tp_tenant(conn, plan_key):
    tid = str(uuid.uuid4())
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                       "is_active, kind) VALUES ($1::uuid, 'Quillbrook Journeys synthetic', $2, "
                       "'starter'::plan_tier_enum, 3, 60, true, 'agency')", tid, f"zz1114-{tid[:12]}")
    await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, updated_by) "
                       "VALUES ($1::uuid, 'tripplanner', $2, 'active', 'test:aa1114')", tid, plan_key)
    return tid


@pytest.mark.asyncio
async def test_add_domain_needs_the_custom_domain_feature(conn):
    hosted = await _tp_tenant(conn, "tripplanner.hosted")
    with pytest.raises(tpd.DomainError) as e:
        await tpd.add_domain(conn, hosted, domain="trip.saltreed.example.invalid", actor=ACTOR)
    assert (e.value.code, e.value.status) == ("custom_domain_not_included", 409)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_tripplanner_domain "
                               "WHERE tenant_id = $1::uuid", hosted) == 0

    pro = await _tp_tenant(conn, "tripplanner.pro")
    out = await tpd.add_domain(conn, pro, domain="trip.saltreed.example.invalid", actor=ACTOR)
    assert out["status"] == "pending"
