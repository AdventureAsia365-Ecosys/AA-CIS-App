"""AA-1028 R1 slice 2 — the tenant_provision saga (services/tenant_account/provision.py) on real Postgres.

The saga runs on a test connection inside one outer transaction that is rolled back (a pool shim hands it out, so
the saga's own transaction is a savepoint). Covers:

  * create: the tenant row as POST /admin/tenants writes it (default brand rules row, no API key, no quota ledger
    row since slice 3), the content subscription from the 355 bridge (active; trials: test_aa1028_slice3), add-ons
    with seats, the feed events carrying the job's actor, the ONBOARD audit row with the request key;
  * idempotency: the same request again returns the stored outcome (resumed) and writes nothing; a slug that exists
    without this key's audit row is refused (slug_taken);
  * attach: add-ons on an existing tenant, a canceled add-on active again, unchanged when nothing differs, refused on
    an offboarded tenant and for an unknown plan; the refusals leave nothing behind.
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
from services.tenant_account import provision as pv  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class _Pool:
    """The saga's pool, handing out the test's connection (its transaction becomes a savepoint)."""

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
    # Dev's shared.tenants.plan_id (made by hand on Dev, AA-797) is not in the CI replay; as test_aa797 does.
    await c.execute("ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS plan_id uuid")
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


def _create(key=None, slug=None, **kw):
    body = {"request_key": key or f"zz-key-{uuid.uuid4().hex[:12]}", "mode": "create",
            "name": "Zephyr Trails synthetic", "slug": slug or f"zz1028p-{uuid.uuid4().hex[:10]}",
            "plan_tier": "starter", "posts_per_week": 3}
    body.update(kw)
    return pv.parse(body)


async def _run(conn, req, job_id=None):
    return await pv.provision(_Pool(conn), req, job_id=job_id or str(uuid.uuid4()))


async def _subs(conn, tid) -> dict:
    rows = await conn.fetch("SELECT product, plan_key, status, seats FROM shared.tenant_subscription "
                            "WHERE tenant_id = $1::uuid", tid)
    return {r["product"]: (r["plan_key"], r["status"], r["seats"]) for r in rows}


async def _ents(conn, tid) -> dict:
    rows = await conn.fetch("SELECT feature_key, limit_value, source FROM shared.v_tenant_entitlement_v1 "
                            "WHERE tenant_id = $1::uuid", tid)
    return {r["feature_key"]: (None if r["limit_value"] is None else float(r["limit_value"]), r["source"])
            for r in rows}


@pytest.mark.asyncio
async def test_create_writes_the_tenant_subscriptions_and_events(conn):
    job = str(uuid.uuid4())
    req = _create(kind="demo",
                  add_ons=[{"product": "crm", "plan_key": "crm.core", "seats": 4},
                           {"product": "tripplanner", "plan_key": "tripplanner.hosted"}])
    out = await _run(conn, req, job)
    tid = out["tenant_id"]
    assert out["resumed"] is False and out["mode"] == "create" and "api_key" not in out
    assert out["steps"] == ["tenant_created", "content_active", "crm_subscribed", "tripplanner_subscribed"]
    t = await conn.fetchrow("SELECT name, slug, plan_tier::text AS tier, rate_limit_rpm, is_active, "
                            "account_status, kind, is_canary, posts_per_week FROM shared.tenants "
                            "WHERE tenant_id = $1::uuid", tid)
    assert (t["name"], t["slug"], t["tier"], t["rate_limit_rpm"]) == ("Zephyr Trails synthetic", req.slug,
                                                                      "starter", 60)
    assert t["is_active"] is True and t["account_status"] == "active"
    assert t["kind"] == "demo" and t["is_canary"] is True and t["posts_per_week"] == 3
    # slice 3: the ACPv1 S2/S3/S4 10/10/50 ledger has no reader; the saga writes no row
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.acp_quota_ledger WHERE tenant_id = $1::uuid",
                               tid) == 0
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_brand_rules WHERE tenant_id = $1::uuid "
                               "AND brand_name = 'default'", tid) == 1
    assert await _subs(conn, tid) == {"content": ("content.starter", "active", None),
                                      "crm": ("crm.core", "active", 4),
                                      "tripplanner": ("tripplanner.hosted", "active", None)}
    ents = await _ents(conn, tid)
    assert ents["content.rewrite"] == (50.0, "plan") and ents["crm.seats"] == (4.0, "plan")
    assert ents["crm.ai_assist"] == (None, "plan") and ents["tp.hosted"] == (None, "plan")
    actor = pv.actor_for(job)
    n = await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event WHERE tenant_id = $1::uuid "
                            "AND actor = $2", tid, actor)
    assert n == out["events_written"] and n > 10
    types = {r["type"] for r in await conn.fetch("SELECT DISTINCT type FROM shared.tenant_account_event "
                                                 "WHERE tenant_id = $1::uuid", tid)}
    assert types == {"account.status_changed", "subscription.changed", "entitlement.changed"}
    audit = await conn.fetchrow("SELECT actor, action, details FROM acp_shared.audit_log WHERE tenant_id = $1 "
                                "AND action = 'agency.onboard'", tid)
    details = json.loads(audit["details"])
    assert audit["actor"] == actor and details["provision_request_key"] == req.request_key
    assert details["job_id"] == job and details["trial_days"] is None and details["pilot_weeks"] is None


@pytest.mark.asyncio
async def test_same_request_again_is_resumed_and_writes_nothing(conn):
    req = _create()
    first = await _run(conn, req)
    events = await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event")
    again = await _run(conn, req)
    assert again["resumed"] is True and again["tenant_id"] == first["tenant_id"]
    assert again["steps"] == first["steps"] == ["tenant_created", "content_active"]
    assert again["events_written"] == first["events_written"]
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event") == events
    assert await conn.fetchval("SELECT count(*) FROM shared.tenants WHERE slug = $1", req.slug) == 1


@pytest.mark.asyncio
async def test_slug_taken_by_another_request_is_refused(conn):
    req = _create()
    await _run(conn, req)
    other = _create(slug=req.slug)
    with pytest.raises(pv.ProvisionError) as info:
        await _run(conn, other)
    assert info.value.code == "slug_taken"


@pytest.mark.asyncio
async def test_unknown_add_on_plan_rolls_the_whole_request_back(conn):
    req = _create(add_ons=[{"product": "crm", "plan_key": "crm.platinum"}])
    with pytest.raises(pv.ProvisionError) as info:
        await _run(conn, req)
    assert info.value.code == "unknown_plan"
    assert await conn.fetchval("SELECT count(*) FROM shared.tenants WHERE slug = $1", req.slug) == 0


@pytest.mark.asyncio
async def test_attach_adds_revives_and_leaves_unchanged(conn):
    tid = (await _run(conn, _create()))["tenant_id"]
    add = pv.parse({"request_key": f"zz-att-{uuid.uuid4().hex[:10]}", "mode": "attach", "tenant_id": tid,
                    "add_ons": [{"product": "crm", "plan_key": "crm.pro", "seats": 6}]})
    out = await _run(conn, add)
    assert out["steps"] == ["crm_subscribed"] and "api_key" not in out
    assert (await _subs(conn, tid))["crm"] == ("crm.pro", "active", 6)
    assert (await _ents(conn, tid))["crm.campaigns"] == (None, "plan")
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1 "
                               "AND action = 'tenant.subscription_changed'", tid) == 1
    # the same add-on under a new key: nothing differs
    same = pv.parse({**add.to_payload(), "request_key": f"zz-att-{uuid.uuid4().hex[:10]}"})
    assert (await _run(conn, same))["steps"] == ["crm_unchanged"]
    # a canceled add-on is active again
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'canceled', canceled_at = now() "
                       "WHERE tenant_id = $1::uuid AND product = 'crm'", tid)
    assert "crm.core" not in await _ents(conn, tid)
    again = pv.parse({**add.to_payload(), "request_key": f"zz-att-{uuid.uuid4().hex[:10]}"})
    assert (await _run(conn, again))["steps"] == ["crm_subscribed"]
    assert (await _subs(conn, tid))["crm"] == ("crm.pro", "active", 6)
    assert "crm.core" in await _ents(conn, tid)


@pytest.mark.asyncio
async def test_attach_refuses_an_offboarded_or_unknown_tenant(conn):
    tid = (await _run(conn, _create()))["tenant_id"]
    await conn.execute("UPDATE shared.tenants SET cancelled_at = now(), is_active = false WHERE tenant_id = $1::uuid",
                       tid)
    for tenant, code in ((tid, "tenant_offboarded"), (str(uuid.uuid4()), "tenant_not_found")):
        req = pv.parse({"request_key": f"zz-att-{uuid.uuid4().hex[:10]}", "mode": "attach", "tenant_id": tenant,
                        "add_ons": [{"product": "tripplanner", "plan_key": "tripplanner.hosted"}]})
        with pytest.raises(pv.ProvisionError) as info:
            await _run(conn, req)
        assert info.value.code == code
    assert "tripplanner" not in await _subs(conn, tid)
