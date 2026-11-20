"""AA-1168 follow-up (CIS 464) — publish_log.sent_message through services/acp_publish/idempotency.py against real
Postgres: claim() stores the social post text on a new row, keeps the text a prior attempt stored (and returns it for
the retry lookup) even when the wording changed since, replaces it for a failed row of another revision, and
record_sent_message() replaces it right before a retry posts again; a blog claim stores none.

Needs CIS 464 (the migration-only PR before this code); until it is on the branch the module is skipped. Invented text
only; the rows are deleted after each test."""
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
_HAS_COLUMN = ("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'acp_shared' "
               "AND table_name = 'publish_log' AND column_name = 'sent_message') AS ok")


async def _no_fk(conn):
    await conn.execute("SET session_replication_role = replica")   # publish_log alone: no piece / tenant parents


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3, setup=_no_fk)
    if not await p.fetchval(_HAS_COLUMN):
        await p.close()
        pytest.skip("CIS 464 is not on this branch yet (migration-only PR first)")
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


async def _claim_msg(pool, piece, key, message):
    async with pool.acquire() as conn:
        return await idem.claim(conn, piece_id=piece, tenant_id=TENANT, channel="facebook", key=key,
                                sent_message=message)


async def _stored(pool, publish_id):
    return await pool.fetchval("SELECT sent_message AS ok FROM acp_shared.publish_log WHERE publish_id = $1::uuid",
                               publish_id)


@pytest.mark.asyncio
async def test_the_sent_text_is_stored_kept_on_a_retry_and_replaced_only_by_a_record(pool):
    piece = str(uuid.uuid4())
    key = idem.publish_key(piece, "facebook", "A quiet pier.")
    first = await _claim_msg(pool, piece, key, "A quiet pier.\n\nOld wording. #OldTag")
    assert not first.prior_attempt and first.prior_message is None
    assert await _stored(pool, first.publish_id) == "A quiet pier.\n\nOld wording. #OldTag"

    # the attempt died mid-call; the wording changed before the retry
    await pool.execute("UPDATE acp_shared.publish_log SET requested_at = now() - interval '10 minutes' "
                       "WHERE publish_id = $1::uuid", first.publish_id)
    retry = await _claim_msg(pool, piece, key, "A quiet pier.\n\nNew wording. #NewTag")
    assert retry.prior_attempt and retry.publish_id == first.publish_id
    assert retry.prior_message == "A quiet pier.\n\nOld wording. #OldTag"
    assert await _stored(pool, first.publish_id) == "A quiet pier.\n\nOld wording. #OldTag"   # kept for the lookup

    async with pool.acquire() as conn:   # nothing found remotely: the route records the new text, then posts it
        await idem.record_sent_message(conn, publish_id=retry.publish_id, tenant_id=TENANT,
                                       sent_message="A quiet pier.\n\nNew wording. #NewTag")
    assert await _stored(pool, first.publish_id) == "A quiet pier.\n\nNew wording. #NewTag"


@pytest.mark.asyncio
async def test_a_failed_row_keeps_its_text_for_its_revision_and_takes_the_new_one_for_another(pool):
    piece = str(uuid.uuid4())
    key = idem.publish_key(piece, "facebook", "Lantern market.")
    c = await _claim_msg(pool, piece, key, "Lantern market.\n\nOld wording. #OldTag")
    await _finish(pool, c.publish_id, False)
    same = await _claim_msg(pool, piece, key, "Lantern market.\n\nNew wording. #NewTag")
    assert same.prior_attempt and same.prior_message == "Lantern market.\n\nOld wording. #OldTag"
    assert await _stored(pool, c.publish_id) == "Lantern market.\n\nOld wording. #OldTag"
    await _finish(pool, same.publish_id, False)

    edited = idem.publish_key(piece, "facebook", "Lantern market at dusk.")
    other = await _claim_msg(pool, piece, edited, "Lantern market at dusk.\n\nNew wording. #NewTag")
    assert not other.prior_attempt and other.prior_message is None
    assert await _stored(pool, c.publish_id) == "Lantern market at dusk.\n\nNew wording. #NewTag"


@pytest.mark.asyncio
async def test_a_blog_claim_stores_no_text(pool):
    piece = str(uuid.uuid4())
    c = await _claim(pool, piece, idem.publish_key(piece, "blog", "text"))
    assert await _stored(pool, c.publish_id) is None
