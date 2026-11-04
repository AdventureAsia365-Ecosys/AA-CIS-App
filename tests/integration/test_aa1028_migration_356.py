"""AA-1028 R1 slice 2 — CIS 356 (resolver fixes from the slice-1 review) on real Postgres.

conftest replays 356 like every migration. Each test runs in one transaction that is rolled back.

  * the file applied, a re-run is a no-op;
  * a platform key (ai.budget_usd_monthly from the legacy budget row or an override) needs a live content
    subscription: an offboarded tenant loses it, a restore gives it back;
  * shared.tenants.rate_limit_rpm is published as content.rate_limit_rpm: an override while it differs from the
    content plan, the plan's value again when equal; each change writes entitlement.changed;
  * a running trial serves the plan's keys as source trial with the end as expires_at (356 also stopped an ended
    trial at once; CIS 361 keeps it live until the tick, see test_aa1028_migration_361);
  * the writer guard checks one privilege per call: a writer that holds SELECT but not INSERT on
    tenant_subscription is refused (355's guard, one call for 'SELECT, INSERT, UPDATE', let it pass).
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import APP_USER_356_GRANTS, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    # Another module may have re-created the resolver (a 353 re-run): CI's legacy writer app_user needs its grants back
    # for the 356 guard (rolled back with the test).
    await c.execute(APP_USER_356_GRANTS)
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(conn, *, tier="starter", rpm=60) -> uuid.UUID:
    """The INSERT of POST /admin/tenants (api/routers/admin.py)."""
    tid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
        "is_active) VALUES ($1, 'Zephyr Trails synthetic', $2, $3::plan_tier_enum, 3, $4, true)",
        tid, f"zz356-{tid.hex[:12]}", tier, rpm)
    return tid


async def _ents(conn, tid) -> dict:
    rows = await conn.fetch("SELECT feature_key, limit_value, source FROM shared.v_tenant_entitlement_v1 "
                            "WHERE tenant_id = $1", tid)
    return {r["feature_key"]: (None if r["limit_value"] is None else float(r["limit_value"]), r["source"])
            for r in rows}


async def _materialised(conn, tid) -> dict:
    rows = await conn.fetch("SELECT feature_key, limit_value, source FROM shared.tenant_entitlement "
                            "WHERE tenant_id = $1", tid)
    return {r["feature_key"]: (None if r["limit_value"] is None else float(r["limit_value"]), r["source"])
            for r in rows}


async def _changes(conn, tid, key) -> list:
    rows = await conn.fetch("SELECT change FROM shared.tenant_account_event WHERE tenant_id = $1 "
                            "AND type = 'entitlement.changed' AND change->>'feature_key' = $2 ORDER BY txid, seq",
                            tid, key)
    return [json.loads(r["change"]) for r in rows]


@pytest.mark.asyncio
async def test_file_applied_and_rerun_is_a_noop(conn):
    assert await conn.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = '356'"), \
        "conftest swallowed an error in 356 (see its log)"
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_account_event")
    await conn.execute(migration_body(356))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.tenant_account_event") == before


@pytest.mark.asyncio
async def test_platform_keys_need_a_live_content_subscription(conn):
    tid = await _tenant(conn)
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, updated_by) "
                       "VALUES ($1, 7, 'admin:test')", tid)
    await conn.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, reason, "
                       "granted_by) VALUES ($1, 'policy.notice_days', 30, 'contract term', 'admin:test')", tid)
    ents = await _ents(conn, tid)
    assert ents["ai.budget_usd_monthly"] == (7.0, "override") and ents["policy.notice_days"] == (30.0, "override")
    # offboard (admin.py shape): the bridge cancels the content subscription, every key goes, platform ones too
    await conn.execute("UPDATE shared.tenants SET cancelled_at = now(), is_active = false WHERE tenant_id = $1", tid)
    assert await _ents(conn, tid) == {} and await _materialised(conn, tid) == {}
    assert (await _changes(conn, tid, "ai.budget_usd_monthly"))[-1]["op"] == "removed"
    # restore: content active again, the budget row and the override apply again
    await conn.execute("UPDATE shared.tenants SET cancelled_at = NULL, is_active = true WHERE tenant_id = $1", tid)
    ents = await _ents(conn, tid)
    assert ents["ai.budget_usd_monthly"] == (7.0, "override") and ents["policy.notice_days"] == (30.0, "override")
    assert await _materialised(conn, tid) == ents


@pytest.mark.asyncio
async def test_suspended_tenant_keeps_its_keys_and_in_service_tells(conn):
    """A suspension (is_active false) leaves the subscription alone: the consumer reads account_status."""
    tid = await _tenant(conn)
    await conn.execute("UPDATE shared.tenants SET is_active = false WHERE tenant_id = $1", tid)
    ents = await _ents(conn, tid)
    assert ents["ai.budget_usd_monthly"] == (15.0, "plan")
    status = await conn.fetchval("SELECT DISTINCT account_status FROM shared.v_tenant_entitlement_v1 "
                                 "WHERE tenant_id = $1", tid)
    assert status == "suspended"


@pytest.mark.asyncio
async def test_rate_limit_rpm_is_mirrored(conn):
    tid = await _tenant(conn)
    assert (await _ents(conn, tid))["content.rate_limit_rpm"] == (60.0, "plan")
    # AA-766 PATCH {rate_limit_rpm}: the limiter's value is the entitlement
    await conn.execute("UPDATE shared.tenants SET rate_limit_rpm = 120, updated_at = now() WHERE tenant_id = $1", tid)
    assert (await _ents(conn, tid))["content.rate_limit_rpm"] == (120.0, "override")
    assert (await _materialised(conn, tid))["content.rate_limit_rpm"] == (120.0, "override")
    assert (await _changes(conn, tid, "content.rate_limit_rpm"))[-1] == {
        "op": "updated", "feature_key": "content.rate_limit_rpm", "product": "content", "limit": "120.00",
        "source": "override", "expires_at": None}
    # it wins over an explicit override of the same key (that would publish a limit nobody enforces)
    await conn.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, reason, "
                       "granted_by) VALUES ($1, 'content.rate_limit_rpm', 999, 'test', 'admin:test')", tid)
    assert (await _ents(conn, tid))["content.rate_limit_rpm"] == (120.0, "override")
    await conn.execute("DELETE FROM shared.tenant_entitlement_override WHERE tenant_id = $1", tid)
    # PATCH plan_tier (admin.py: rate_limit_rpm from PLAN_LIMITS in the same UPDATE): the plan's value again
    await conn.execute("UPDATE shared.tenants SET plan_tier = 'growth'::plan_tier_enum, rate_limit_rpm = 300 "
                       "WHERE tenant_id = $1", tid)
    assert (await _ents(conn, tid))["content.rate_limit_rpm"] == (300.0, "plan")
    assert await _materialised(conn, tid) == await _ents(conn, tid)


@pytest.mark.asyncio
async def test_rate_limit_override_needs_a_live_content_subscription(conn):
    tid = await _tenant(conn, rpm=90)
    assert (await _ents(conn, tid))["content.rate_limit_rpm"] == (90.0, "override")
    await conn.execute("UPDATE shared.tenants SET cancelled_at = now(), is_active = false WHERE tenant_id = $1", tid)
    assert "content.rate_limit_rpm" not in await _ents(conn, tid)


@pytest.mark.asyncio
async def test_an_ended_trial_is_not_live(conn):
    tid = await _tenant(conn, rpm=90)                           # an RPM override too: it needs a live content row
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, updated_by) "
                       "VALUES ($1, 7, 'admin:test')", tid)
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'trialing', "
                       "trial_ends_at = now() + interval '3 days' WHERE tenant_id = $1 AND product = 'content'", tid)
    ents = await _ents(conn, tid)
    assert ents["trade.book"][1] == "trial" and ents["content.rate_limit_rpm"] == (90.0, "override")
    expires = await conn.fetchval("SELECT expires_at FROM shared.v_tenant_entitlement_v1 WHERE tenant_id = $1 "
                                  "AND feature_key = 'trade.book'", tid)
    assert expires is not None
    # the trial ended: 356 stopped serving it at once; CIS 361 (replayed after it) keeps it live until the
    # tenant_account_tick moves it (test_aa1028_migration_361), so the 356 rule is checked on a canceled row instead
    await conn.execute("UPDATE shared.tenant_subscription SET trial_ends_at = now() - interval '1 day' "
                       "WHERE tenant_id = $1 AND product = 'content'", tid)
    assert (await _ents(conn, tid))["trade.book"][1] == "trial"
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'canceled', canceled_at = now() "
                       "WHERE tenant_id = $1 AND product = 'content'", tid)
    assert await _ents(conn, tid) == {}
    assert await _materialised(conn, tid) == {}                 # the subscription trigger re-synced it
    assert (await _changes(conn, tid, "trade.book"))[-1]["op"] == "removed"
    # the guards of 356 agree with the resolver
    await conn.execute(_guards())


def _guards() -> str:
    body = migration_body(356)
    start = body.index("-- ── 4. Guards")
    return body[start:body.index("INSERT INTO shared.schema_versions", start)]


@pytest.mark.asyncio
async def test_writer_guard_checks_each_privilege(conn):
    await conn.execute(_guards())                                 # the replay's writers pass
    await conn.execute("CREATE ROLE zz356_writer NOLOGIN")
    await conn.execute("GRANT UPDATE (plan_tier) ON shared.tenants TO zz356_writer")
    await conn.execute("GRANT SELECT ON shared.plan, shared.plan_feature, shared.tenant_subscription, "
                       "shared.tenant_entitlement, shared.v_tenant_entitlement_resolved TO zz356_writer")
    await conn.execute("GRANT INSERT, UPDATE, DELETE ON shared.tenant_entitlement TO zz356_writer")
    await conn.execute("GRANT INSERT ON shared.tenant_account_event TO zz356_writer")
    await conn.execute("GRANT EXECUTE ON FUNCTION shared.tenant_entitlement_sync(uuid), "
                       "shared.tenant_account_closed(uuid), shared.tenant_account_emit(text, uuid, jsonb) "
                       "TO zz356_writer")
    # 355's one-call check passes for this role (SELECT is held) ...
    assert await conn.fetchval("SELECT has_table_privilege('zz356_writer', 'shared.tenant_subscription', "
                               "'SELECT, INSERT, UPDATE') AS ok")
    # ... 356's per-privilege check does not
    with pytest.raises(asyncpg.RaiseError) as info:
        async with conn.transaction():
            await conn.execute(_guards())
    msg = str(info.value)
    assert "zz356_writer:shared.tenant_subscription INSERT" in msg
    assert "zz356_writer:shared.tenant_subscription UPDATE" in msg
    await conn.execute("GRANT INSERT, UPDATE ON shared.tenant_subscription TO zz356_writer")
    await conn.execute(_guards())
