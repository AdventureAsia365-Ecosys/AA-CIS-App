"""AA-1028 R1 slice 3 — CIS 361 (resolver fixes from the S266 review of slice 3) on real Postgres.

conftest replays 361 like every migration. Each test runs in one transaction that is rolled back.

  * the file applied, a re-run is a no-op;
  * an ended trial stays live until the tenant_account_tick moves it: its keys keep source trial and the past
    expires_at, the materialisation does not change at trial_ends_at, and 361's guards pass on that state;
  * a platform key is taken from the content plan only, whatever an add-on plan carries;
  * a canceled content subscription still drops every key, the platform ones included (356's rule stays).
"""
import os
import sys
import uuid

import pytest
import pytest_asyncio

import asyncpg

sys.path.insert(0, os.path.dirname(__file__))
from conftest import APP_USER_356_GRANTS, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    await c.execute(APP_USER_356_GRANTS)
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, *, tier="starter", rpm=60) -> uuid.UUID:
    """The INSERT of POST /admin/tenants (api/routers/admin.py); the 355 bridge adds the content subscription."""
    tid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
        "is_active) VALUES ($1, 'Larchmont Treks synthetic', $2, $3::plan_tier_enum, 3, $4, true)",
        tid, f"zz361-{tid.hex[:12]}", tier, rpm)
    return tid


async def _ents(conn, tid) -> dict:
    rows = await conn.fetch("SELECT feature_key, limit_value, source, plan_key, expires_at "
                            "FROM shared.v_tenant_entitlement_resolved WHERE tenant_id = $1", tid)
    return {r["feature_key"]: (None if r["limit_value"] is None else float(r["limit_value"]), r["source"],
                               r["plan_key"], r["expires_at"]) for r in rows}


async def _materialised(conn, tid) -> dict:
    rows = await conn.fetch("SELECT feature_key, limit_value, source, plan_key, expires_at "
                            "FROM shared.tenant_entitlement WHERE tenant_id = $1", tid)
    return {r["feature_key"]: (None if r["limit_value"] is None else float(r["limit_value"]), r["source"],
                               r["plan_key"], r["expires_at"]) for r in rows}


def _guards() -> str:
    body = migration_body(361)
    start = body.index("-- ── 3. Guards")
    return body[start:body.index("INSERT INTO shared.schema_versions", start)]


@pytest.mark.asyncio
async def test_file_applied_and_rerun_is_a_noop(conn):
    assert await conn.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = '361'"), \
        "conftest swallowed an error in 361 (see its log)"
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_account_event")
    await conn.execute(migration_body(361))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_account_event") == before


@pytest.mark.asyncio
async def test_an_ended_trial_stays_live_until_the_tick(conn):
    tid = await _tenant(conn)
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'trialing', "
                       "trial_ends_at = now() + interval '2 days' WHERE tenant_id = $1 AND product = 'content'", tid)
    running = await _ents(conn, tid)
    assert running["ai.budget_usd_monthly"][:2] == (15.0, "trial") and running["trade.book"][1] == "trial"
    events = await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event WHERE tenant_id = $1 "
                                 "AND type = 'entitlement.changed'", tid)
    # the trial ends; the tick has not run yet. The subscription trigger fires on the trial_ends_at update and
    # re-syncs: nothing changes but the end date.
    await conn.execute("UPDATE shared.tenant_subscription SET trial_ends_at = now() - interval '10 minutes' "
                       "WHERE tenant_id = $1 AND product = 'content'", tid)
    ended = await _ents(conn, tid)
    assert set(ended) == set(running)
    assert ended["ai.budget_usd_monthly"][:3] == (15.0, "trial", "content.starter")
    assert ended["trade.book"][3] is not None and await _materialised(conn, tid) == ended
    await conn.execute(_guards())
    # the tick's move (past_due: the grace state) re-syncs to the plan, expires_at cleared
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'past_due', updated_by = 'job:zz' "
                       "WHERE tenant_id = $1 AND product = 'content'", tid)
    moved = await _ents(conn, tid)
    assert moved["ai.budget_usd_monthly"] == (15.0, "plan", "content.starter", None)
    assert await _materialised(conn, tid) == moved
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event WHERE tenant_id = $1 "
                               "AND type = 'entitlement.changed'", tid) > events


@pytest.mark.asyncio
async def test_platform_keys_come_from_the_content_plan(conn):
    tid = await _tenant(conn)
    await conn.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, seats, "
                       "updated_by) VALUES ($1, 'crm', 'crm.core', 'active', 3, 'admin:test')", tid)
    await conn.execute("INSERT INTO shared.plan_feature (plan_key, feature_key, limit_value, updated_by) "
                       "VALUES ('crm.core', 'ai.budget_usd_monthly', 999, 'zz'), "
                       "('crm.core', 'policy.notice_days', 1, 'zz')")
    ents = await _ents(conn, tid)
    assert ents["ai.budget_usd_monthly"][:3] == (15.0, "plan", "content.starter")
    assert ents["policy.notice_days"][:3] == (7.0, "plan", "content.starter")
    assert ents["crm.seats"][:2] == (3.0, "plan")
    assert await _materialised(conn, tid) == ents
    await conn.execute(_guards())


@pytest.mark.asyncio
async def test_a_canceled_content_subscription_still_drops_every_key(conn):
    tid = await _tenant(conn)
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, updated_by) "
                       "VALUES ($1, 7, 'admin:test')", tid)
    assert (await _ents(conn, tid))["ai.budget_usd_monthly"][:2] == (7.0, "override")
    await conn.execute("UPDATE shared.tenants SET cancelled_at = now(), is_active = false WHERE tenant_id = $1", tid)
    assert await _ents(conn, tid) == {} and await _materialised(conn, tid) == {}
    await conn.execute(_guards())
