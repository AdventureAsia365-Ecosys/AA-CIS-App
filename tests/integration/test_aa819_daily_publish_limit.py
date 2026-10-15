"""AA-819 (AUP 2026-11) — the per-tenant daily publish limit against real Postgres: COUNT_TODAY_SQL
(UTC day, what counts and what never counts), idempotency.claim() with a daily_limit (replays and
in-flight retries never refused, concurrent claims cannot both take the last slot), and the
shared.tenants.daily_publish_limit probe/override (works with or without migration 230 applied).
"""
import asyncio
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_publish import daily_limit  # noqa: E402
from services.acp_publish import idempotency as idem  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = str(uuid.uuid4())


async def _no_fk(conn):
    # publish_log has FKs to content_piece/tenants; these tests are about publish_log alone.
    await conn.execute("SET session_replication_role = replica")


@pytest_asyncio.fixture
async def pool():
    daily_limit._forget_column()
    p = await asyncpg.create_pool(DSN, min_size=2, max_size=8, setup=_no_fk)
    await p.execute("DELETE FROM acp_shared.publish_log WHERE tenant_id = $1::uuid", TENANT)
    yield p
    await p.execute("DELETE FROM acp_shared.publish_log WHERE tenant_id = $1::uuid", TENANT)
    await p.close()
    daily_limit._forget_column()


async def _row(pool, status, *, at="now()", published=False, tenant=TENANT):
    piece = str(uuid.uuid4())
    await pool.execute(
        f"""INSERT INTO acp_shared.publish_log
                (piece_id, tenant_id, channel, status, idempotency_key, requested_at, published_at, created_at)
            VALUES ($1::uuid, $2::uuid, 'blog', $3, 'k-' || $1, {at}, {at if published else 'NULL'}, {at})""",
        piece, tenant, status)
    return piece


async def _count(pool):
    async with pool.acquire() as conn:
        return await daily_limit.count_today(conn, TENANT)


async def _claim(pool, piece, key, limit):
    async with pool.acquire() as conn:
        return await idem.claim(conn, piece_id=piece, tenant_id=TENANT, channel="blog", key=key, daily_limit=limit)


@pytest.mark.asyncio
async def test_count_today_counts_requested_published_and_unpublished_after_publish(pool):
    await _row(pool, "requested")
    await _row(pool, "published", published=True)
    await _row(pool, "unpublished", published=True)   # unpublish does not hand the slot back
    await _row(pool, "failed")                         # a failed attempt never counts
    await _row(pool, "unpublished")                    # never published (published_at NULL)
    await _row(pool, "published", at="date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' "
                                     "- interval '1 second'", published=True)   # yesterday, UTC
    await _row(pool, "published", published=True, tenant=str(uuid.uuid4()))   # another tenant
    assert await _count(pool) == 3


@pytest.mark.asyncio
async def test_limit_refuses_a_new_publish_but_never_a_replay_or_in_flight_retry(pool):
    first = str(uuid.uuid4())
    key1 = idem.publish_key(first, "blog", "text 1")
    c1 = await _claim(pool, first, key1, limit=2)
    assert c1.state == "claimed"
    second = str(uuid.uuid4())
    key2 = idem.publish_key(second, "blog", "text 2")
    c2 = await _claim(pool, second, key2, limit=2)
    assert c2.state == "claimed"
    async with pool.acquire() as conn:
        await idem.finish(conn, publish_id=c1.publish_id, tenant_id=TENANT, success=True, external_id="1",
                          external_url="https://b.com/1", error=None)

    third = str(uuid.uuid4())
    c3 = await _claim(pool, third, idem.publish_key(third, "blog", "text 3"), limit=2)
    assert (c3.state, c3.used, c3.limit) == ("limited", 2, 2)
    assert await pool.fetchval("SELECT count(*) FROM acp_shared.publish_log WHERE piece_id = $1::uuid",
                               third) == 0     # refused before any row was written

    assert (await _claim(pool, first, key1, limit=2)).state == "published"     # replay: not refused
    assert (await _claim(pool, second, key2, limit=2)).state == "in_progress"  # 409 retry: not refused
    assert await _count(pool) == 2                                            # and neither counted


@pytest.mark.asyncio
async def test_failed_attempt_frees_its_slot_and_its_retry_is_checked(pool):
    piece = str(uuid.uuid4())
    key = idem.publish_key(piece, "blog", "text")
    c = await _claim(pool, piece, key, limit=1)
    async with pool.acquire() as conn:
        await idem.finish(conn, publish_id=c.publish_id, tenant_id=TENANT, success=False, external_id=None,
                          external_url=None, error="timeout")
    assert await _count(pool) == 0
    retry = await _claim(pool, piece, key, limit=1)
    assert retry.state == "claimed" and retry.prior_attempt
    other = str(uuid.uuid4())
    assert (await _claim(pool, other, idem.publish_key(other, "blog", "x"), limit=1)).state == "limited"


@pytest.mark.asyncio
async def test_concurrent_publishes_of_different_pieces_cannot_both_take_the_last_slot(pool):
    await _row(pool, "published", published=True)   # 1 used, limit 2: one slot left
    pieces = [str(uuid.uuid4()) for _ in range(5)]
    results = await asyncio.gather(*[_claim(pool, p, idem.publish_key(p, "blog", p), limit=2) for p in pieces])
    states = sorted(r.state for r in results)
    assert states.count("claimed") == 1 and states.count("limited") == 4
    assert await _count(pool) == 2


@pytest.mark.asyncio
async def test_override_column_probe_and_read_with_or_without_migration_230(pool):
    """Works before the 230 apply (default) and after it (override). The DDL runs in a transaction
    that is rolled back, so the shared test DB keeps whatever the migrations created."""
    tenant = str(uuid.uuid4())
    async with pool.acquire() as conn:
        applied = await daily_limit.override_column_exists(conn)
        has_column = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'shared' "
            "AND table_name = 'tenants' AND column_name = 'daily_publish_limit'")
        assert applied is bool(has_column)
        if not applied:
            assert await daily_limit.effective_limit(conn, tenant) == daily_limit.DEFAULT_DAILY_PUBLISH_LIMIT

        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS daily_publish_limit integer")
            await conn.execute(
                "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, posts_per_week, "
                "daily_publish_limit) VALUES ($1::uuid, 'AA-819 test', $2, 'starter', 60, 1, 2)",
                tenant, f"aa819-{tenant[:8]}")
            daily_limit._forget_column()
            assert await daily_limit.override_column_exists(conn) is True
            assert await daily_limit.effective_limit(conn, tenant) == 2
            await conn.execute("UPDATE shared.tenants SET daily_publish_limit = NULL WHERE tenant_id = $1::uuid",
                               tenant)
            assert await daily_limit.effective_limit(conn, tenant) == daily_limit.DEFAULT_DAILY_PUBLISH_LIMIT
        finally:
            await tr.rollback()
            daily_limit._forget_column()


@pytest.mark.asyncio
async def test_admin_patch_override_round_trip_and_list(monkeypatch):
    """PATCH /admin/tenants/{id} daily_publish_limit against real Postgres: 409 while the column is
    missing; with it, set (audited), list read, explicit null clears. Adds the column itself when 230
    is not applied in this DB, and drops it again afterwards."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from api.routers import admin
    monkeypatch.setattr(admin, "verify_admin_secret", lambda _s: None)
    hdr = {"x_admin_secret": "s", "x_admin_user_id": "admin-819", "x_admin_role": "admin"}
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=p, redis=None)))
    # plan_id: the AA-798 baseline column update_tenant() reads (no-op once it exists, as test_aa797)
    await p.execute("ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS plan_id uuid")
    tid = uuid.uuid4()
    await p.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, posts_per_week) "
                    "VALUES ($1, 'AA-819 admin', $2, 'starter', 60, 1)", tid, f"aa819a-{tid.hex[:8]}")
    daily_limit._forget_column()
    added = False
    try:
        async with p.acquire() as conn:
            applied = await daily_limit.override_column_exists(conn)
        if not applied:
            with pytest.raises(HTTPException) as e:
                await admin.update_tenant(tid, admin.UpdateTenantRequest(daily_publish_limit=2), request, **hdr)
            assert e.value.status_code == 409 and "migration 230" in e.value.detail
            await p.execute("ALTER TABLE shared.tenants ADD COLUMN daily_publish_limit integer")
            added = True
            daily_limit._forget_column()

        out = await admin.update_tenant(tid, admin.UpdateTenantRequest(daily_publish_limit=2), request, **hdr)
        assert out["changed"] == ["daily_publish_limit"] and out["daily_publish_limit"] == 2
        assert await p.fetchval("SELECT daily_publish_limit FROM shared.tenants WHERE tenant_id = $1", tid) == 2
        audit = await p.fetchrow("SELECT action, details FROM acp_shared.audit_log WHERE tenant_id = $1 "
                                 "ORDER BY created_at DESC LIMIT 1", str(tid))
        details = audit["details"] if isinstance(audit["details"], dict) else json.loads(audit["details"])
        assert audit["action"] == "tenant.daily_publish_limit_changed"
        assert details["before"] == {"daily_publish_limit": None} and details["after"] == {"daily_publish_limit": 2}
        async with p.acquire() as conn:
            assert (await admin._publish_limit_overrides(conn)).get(tid) == 2
            assert await daily_limit.effective_limit(conn, tid) == 2

        out = await admin.update_tenant(tid, admin.UpdateTenantRequest(daily_publish_limit=None), request, **hdr)
        assert out["daily_publish_limit"] is None and out["daily_publish_limit_effective"] == 20
        assert await p.fetchval("SELECT daily_publish_limit FROM shared.tenants WHERE tenant_id = $1", tid) is None
    finally:
        await p.execute("DELETE FROM acp_shared.audit_log WHERE tenant_id = $1", str(tid))
        await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
        if added:
            await p.execute("ALTER TABLE shared.tenants DROP COLUMN IF EXISTS daily_publish_limit")
        await p.close()
        daily_limit._forget_column()
