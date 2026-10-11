"""AA-726 — publish_log claim/finish (services/acp_publish/idempotency.py) against real Postgres
with migration 215 applied by conftest: the partial unique index, the status check, and the
claim state machine (replay, in-flight, stale reclaim, failed-row reuse, concurrent claims).
"""
import asyncio
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_publish import idempotency as idem  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = str(uuid.uuid4())


async def _no_fk(conn):
    # publish_log has FKs to content_piece/tenants; these tests are about publish_log alone.
    # setup (per acquire), not init: the pool RESETs session settings on release.
    await conn.execute("SET session_replication_role = replica")


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=2, max_size=6, setup=_no_fk)
    await p.execute("DELETE FROM acp_shared.publish_log WHERE tenant_id = $1::uuid", TENANT)
    yield p
    await p.execute("DELETE FROM acp_shared.publish_log WHERE tenant_id = $1::uuid", TENANT)
    await p.close()


async def _claim(pool, piece, key, channel="blog"):
    async with pool.acquire() as conn:
        return await idem.claim(conn, piece_id=piece, tenant_id=TENANT, channel=channel, key=key)


async def _finish(pool, publish_id, success, ext="1"):
    async with pool.acquire() as conn:
        return await idem.finish(conn, publish_id=publish_id, tenant_id=TENANT, success=success,
                                 external_id=ext if success else None,
                                 external_url=f"https://b.com/{ext}" if success else None,
                                 error=None if success else "timeout")


@pytest.mark.asyncio
async def test_first_claim_writes_requested_then_published_is_replayed(pool):
    piece = str(uuid.uuid4())
    key = idem.publish_key(piece, "blog", "text")
    c = await _claim(pool, piece, key)
    assert c.state == "claimed" and not c.prior_attempt
    row = await pool.fetchrow("SELECT status, idempotency_key, requested_at FROM acp_shared.publish_log "
                              "WHERE publish_id = $1::uuid", c.publish_id)
    assert row["status"] == "requested" and row["idempotency_key"] == key and row["requested_at"]

    done = await _finish(pool, c.publish_id, True, "42")
    assert done["status"] == "published" and done["external_id"] == "42" and done["published_at"]

    again = await _claim(pool, piece, key)
    assert again.state == "published" and again.row["external_id"] == "42"
    assert await pool.fetchval("SELECT count(*) FROM acp_shared.publish_log WHERE piece_id = $1::uuid",
                               piece) == 1


@pytest.mark.asyncio
async def test_fresh_requested_is_in_progress_stale_is_reclaimed_as_prior_attempt(pool):
    piece = str(uuid.uuid4())
    key = idem.publish_key(piece, "blog", "text")
    c = await _claim(pool, piece, key)
    assert (await _claim(pool, piece, key)).state == "in_progress"

    await pool.execute("UPDATE acp_shared.publish_log SET requested_at = now() - interval '10 minutes' "
                       "WHERE publish_id = $1::uuid", c.publish_id)
    again = await _claim(pool, piece, key)
    assert again.state == "claimed" and again.prior_attempt and again.publish_id == c.publish_id


@pytest.mark.asyncio
async def test_failed_row_is_reused_and_counts_as_prior_attempt_for_same_revision(pool):
    piece = str(uuid.uuid4())
    key = idem.publish_key(piece, "blog", "text")
    c = await _claim(pool, piece, key)
    await _finish(pool, c.publish_id, False)

    retry = await _claim(pool, piece, key)
    assert retry.state == "claimed" and retry.prior_attempt and retry.publish_id == c.publish_id

    # edited text = new revision: reuses the failed row but is not a prior attempt of THIS key
    await _finish(pool, retry.publish_id, False)
    edited = await _claim(pool, piece, idem.publish_key(piece, "blog", "text edited"))
    assert edited.state == "claimed" and not edited.prior_attempt
    assert await pool.fetchval("SELECT count(*) FROM acp_shared.publish_log WHERE piece_id = $1::uuid",
                               piece) == 1


@pytest.mark.asyncio
async def test_pre_215_failed_row_without_key_is_a_prior_attempt(pool):
    piece = str(uuid.uuid4())
    await pool.execute("INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, last_error) "
                       "VALUES ($1::uuid, $2::uuid, 'blog', 'failed', 'timeout')", piece, TENANT)
    c = await _claim(pool, piece, idem.publish_key(piece, "blog", "text"))
    assert c.state == "claimed" and c.prior_attempt


@pytest.mark.asyncio
async def test_concurrent_claims_of_one_revision_let_exactly_one_through(pool):
    piece = str(uuid.uuid4())
    key = idem.publish_key(piece, "blog", "text")
    results = await asyncio.gather(*[_claim(pool, piece, key) for _ in range(5)])
    assert sorted(r.state for r in results).count("claimed") == 1
    assert await pool.fetchval("SELECT count(*) FROM acp_shared.publish_log WHERE piece_id = $1::uuid",
                               piece) == 1


@pytest.mark.asyncio
async def test_unique_index_and_status_check(pool):
    piece = str(uuid.uuid4())
    insert = ("INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, idempotency_key) "
              "VALUES ($1::uuid, $2::uuid, 'blog', $3, 'k-' || $1)")
    await pool.execute(insert, piece, TENANT, "published")
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(insert, piece, TENANT, "requested")
    await pool.execute(insert, piece, TENANT, "unpublished")    # history rows are not constrained
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(insert, str(uuid.uuid4()), TENANT, "bogus")
