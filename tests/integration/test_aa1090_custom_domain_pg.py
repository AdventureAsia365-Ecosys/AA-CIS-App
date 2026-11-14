"""AA-1090 TripPlanner R8 on real Postgres — the Tenant 360 custom-domain editor
(services/tenant_account/tripplanner_domain.py) and the tripplanner_custom_domain job
(services/jobs/tripplanner_custom_domain_job.py)
over shared.tenant_tripplanner_domain (CIS 427, replayed by conftest), with fake ACM / CloudFront clients (the unit
module's fakes: no AWS).

Each test runs on one asyncpg connection inside an outer transaction that is rolled back. Names are invented.

  * add: the row is pending, its job is queued once (idempotency key) and the change is audited; the per-tenant limit
    (the setting's default) and a host another tenant holds are refused; a bad host never reaches the table;
  * the job walks the real row from pending to active (every UPDATE satisfies the 427 CHECKs: the ARN, the CNAME
    target, the validation record), and a removal deletes the row once the alias is detached;
  * a validation past the timeout fails the host and clears its certificate; retry puts it back to pending.
"""
import os
import sys
import uuid
from datetime import timedelta
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.jobs import tripplanner_custom_domain_job as job  # noqa: E402  (registers the kind)
from services.tenant_account import tp_edge  # noqa: E402
from services.tenant_account import tripplanner_domain as tpd  # noqa: E402
from shared.jobs.registry import Reschedule  # noqa: E402
from tests.unit.test_aa1090_custom_domain import BASE, DIST, TARGET, FakeAcm, FakeCf  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ACTOR = "admin:zz-aa1090-domain"


@pytest.fixture(autouse=True)
def edge_env(monkeypatch):
    monkeypatch.setenv(tp_edge.DIST_ENV, DIST)
    monkeypatch.setenv(tp_edge.BASE_CERT_ENV, BASE)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, *, tripplanner=True) -> str:
    tid = str(uuid.uuid4())
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                       "is_active, kind) VALUES ($1::uuid, 'Larchfield Expeditions synthetic', $2, "
                       "'starter'::plan_tier_enum, 3, 60, true, 'agency')", tid, f"zz1090-{tid[:12]}")
    if tripplanner:
        await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, updated_by) "
                           "VALUES ($1::uuid, 'tripplanner', 'tripplanner.hosted', 'active', 'test:aa1090')", tid)
    return tid


def _ctx(conn, domain_id):
    return SimpleNamespace(pool=conn, payload={"domain_id": domain_id}, job_id=str(uuid.uuid4()),
                           job=SimpleNamespace(progress={}), set_result=lambda r: None)


async def _status(conn, domain_id):
    return await conn.fetchval("SELECT status FROM shared.tenant_tripplanner_domain WHERE domain_id = $1::uuid",
                               domain_id)


@pytest.mark.asyncio
async def test_add_queues_once_and_audits(conn):
    tid = await _tenant(conn)
    out = await tpd.add_domain(conn, tid, domain="Trip.Larchfield.Example.Invalid", actor=ACTOR)
    assert out["status"] == "pending" and out["domain"] == "trip.larchfield.example.invalid"
    jobs = await conn.fetch("SELECT kind, payload->>'domain_id' AS d, idempotency_key FROM shared.job "
                            "WHERE idempotency_key = $1", f"tripplanner_custom_domain:{out['domain_id']}:add")
    assert len(jobs) == 1 and jobs[0]["kind"] == "tripplanner_custom_domain" and jobs[0]["d"] == out["domain_id"]
    audit = await conn.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE tenant_id = $1 AND action = $2",
                                tid, tpd.AdminTenantAuditAction.TRIPPLANNER_DOMAIN_ADDED)
    assert audit == 1
    listed = await tpd.list_domains(conn, tid)
    assert listed["available"] is True and listed["max_domains"] == 1
    assert [d["domain"] for d in listed["domains"]] == ["trip.larchfield.example.invalid"]
    assert listed["domains"][0]["dns_records"] == []


@pytest.mark.asyncio
async def test_limits_and_taken_hosts(conn):
    a, b = await _tenant(conn), await _tenant(conn)
    await tpd.add_domain(conn, a, domain="trip.ottervale.example.invalid", actor=ACTOR)
    with pytest.raises(tpd.DomainError) as e:
        await tpd.add_domain(conn, a, domain="go.ottervale.example.invalid", actor=ACTOR)
    assert e.value.code == "domain_limit_reached"
    with pytest.raises(tpd.DomainError) as e:
        await tpd.add_domain(conn, b, domain="trip.ottervale.example.invalid", actor=ACTOR)
    assert e.value.code == tpd.DOMAIN_TAKEN
    no_tp = await _tenant(conn, tripplanner=False)
    with pytest.raises(tpd.DomainError) as e:
        await tpd.add_domain(conn, no_tp, domain="trip.saltmarsh.example.invalid", actor=ACTOR)
    assert e.value.code == "no_tripplanner"
    with pytest.raises(tpd.DomainError):
        await tpd.add_domain(conn, b, domain="saltmarsh.example", actor=ACTOR)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_tripplanner_domain "
                               "WHERE tenant_id = $1::uuid", b) == 0


@pytest.mark.asyncio
async def test_the_job_walks_a_real_row_to_active_and_removes_it(conn):
    tid = await _tenant(conn)
    added = await tpd.add_domain(conn, tid, domain="trip.fernhollow.example.invalid", actor=ACTOR)
    did = added["domain_id"]
    acm, cf = FakeAcm(), FakeCf()
    ctx = _ctx(conn, did)
    with pytest.raises(Reschedule):
        await job.run_once(ctx, acm, cf)
    row = await conn.fetchrow("SELECT status, certificate_arn, validation_name, cname_target "
                              "FROM shared.tenant_tripplanner_domain WHERE domain_id = $1::uuid", did)
    assert row["status"] == "validating" and row["cname_target"] == TARGET and row["certificate_arn"]
    listed = (await tpd.list_domains(conn, tid))["domains"][0]
    assert {r["purpose"] for r in listed["dns_records"]} == {"route", "certificate"}
    with pytest.raises(Reschedule) as r:
        await job.run_once(ctx, acm, cf)                         # issued -> attaching -> update sent
    ctx.job.progress = {**ctx.job.progress, **r.value.progress}
    assert await _status(conn, did) == "attaching"
    cf.status = "Deployed"
    assert (await job.run_once(ctx, acm, cf))["status"] == "active"
    assert await conn.fetchval("SELECT active_at IS NOT NULL AS ok FROM shared.tenant_tripplanner_domain "
                               "WHERE domain_id = $1::uuid", did)

    removed = await tpd.remove_domain(conn, tid, did, actor=ACTOR)
    assert removed["status"] == "removing"
    out = await job.run_once(_ctx(conn, did), acm, cf)
    assert out["status"] == "removed" and await _status(conn, did) is None
    assert cf.config["Aliases"]["Items"] == ["tripplanner.planner-zone.example.test"]


@pytest.mark.asyncio
async def test_timeout_then_retry(conn):
    tid = await _tenant(conn)
    added = await tpd.add_domain(conn, tid, domain="trip.moorgate.example.invalid", actor=ACTOR)
    did = added["domain_id"]
    acm, cf = FakeAcm(), FakeCf()
    acm.next_status = "PENDING_VALIDATION"
    with pytest.raises(Reschedule):
        await job.run_once(_ctx(conn, did), acm, cf)
    await conn.execute("UPDATE shared.tenant_tripplanner_domain SET requested_at = now() - $2::interval "
                       "WHERE domain_id = $1::uuid", did, timedelta(hours=80))
    out = await job.run_once(_ctx(conn, did), acm, cf)
    assert out["failure_code"] == "validation_timeout"
    row = await conn.fetchrow("SELECT status, certificate_arn, failure_message FROM shared.tenant_tripplanner_domain "
                              "WHERE domain_id = $1::uuid", did)
    assert row["status"] == "failed" and row["certificate_arn"] is None and "retry" in row["failure_message"]
    again = await tpd.retry_domain(conn, tid, did, actor=ACTOR)
    assert again["status"] == "pending" and again["failure_code"] is None
