"""AA-764 (G5) — the new T7/T8 job SQL against real Postgres: the slate proposal enqueue guard
(advisory lock + in-flight + staleness on shared.job) and the T8 "generating" marker
(conditional UPDATE + enqueue in one transaction, on_terminal reset, fetch_request flags)."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

import services.jobs  # noqa: E402,F401  registers t7_slate / t8_angles
from services.acp_angle_gate import service as ag  # noqa: E402
from services.acp_shared import slate  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = uuid.uuid4()


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    await p.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                                    posts_per_week, api_key_hash)
        VALUES ($1, 'AA-764 Test', $2, 'starter', 60, true, 3, 'h') ON CONFLICT DO NOTHING""",
                    TENANT, f"t-{str(TENANT)[:8]}")
    yield p
    await p.execute("DELETE FROM shared.job WHERE payload->>'tenant_id' = $1", str(TENANT))
    await p.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = $1", TENANT)
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", TENANT)
    await p.close()


async def _slate_jobs(pool):
    return await pool.fetch("SELECT id, status FROM shared.job WHERE kind = 't7_slate' "
                            "AND payload->>'tenant_id' = $1 ORDER BY created_at", str(TENANT))


@pytest.mark.asyncio
async def test_slate_proposal_enqueue_guard(pool):
    first = await slate.request_slate_proposal(TENANT, pool, only_if_stale=True)  # never proposed
    assert first["status"] == "queued"
    again = await slate.request_slate_proposal(TENANT, pool, only_if_stale=False)  # in flight
    assert again["id"] == first["id"] and len(await _slate_jobs(pool)) == 1

    await pool.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now(), result = $2::jsonb "
                       "WHERE id = $1", first["id"], json.dumps({"unmatched_markets": ["XX"]}))
    async with pool.acquire() as conn:
        last = await slate.last_proposal(TENANT, conn)
    assert last["result"] == {"unmatched_markets": ["XX"]}
    await slate.request_slate_proposal(TENANT, pool, only_if_stale=True)  # fresh → nothing new
    assert len(await _slate_jobs(pool)) == 1

    await pool.execute("UPDATE shared.job SET finished_at = now() - interval '25 hours' WHERE id = $1",
                       first["id"])
    stale = await slate.request_slate_proposal(TENANT, pool, only_if_stale=True)
    assert stale["status"] == "queued" and len(await _slate_jobs(pool)) == 2


@pytest.mark.asyncio
async def test_goal_generation_marker_and_reset(pool):
    req_id = await pool.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel) "
        "VALUES ($1, 'atom-764', 'linkedin') RETURNING request_id", TENANT)

    out = await ag.start_goal_generation(TENANT, req_id, "promotion", pool)
    assert out["status"] == "pending_goal" and out["generating"] is True and out["goal"] == "promotion"
    job = await pool.fetchrow("SELECT status, payload FROM shared.job WHERE kind = 't8_angles' "
                              "AND payload->>'request_id' = $1", str(req_id))
    payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
    assert job["status"] == "queued" and payload["tenant_id"] == str(TENANT)

    with pytest.raises(ag.WrongStatusError):  # double click while generating
        await ag.start_goal_generation(TENANT, req_id, "promotion", pool)

    await pool.execute("UPDATE shared.job SET status = 'failed', finished_at = now() "
                       "WHERE kind = 't8_angles' AND payload->>'request_id' = $1", str(req_id))
    await ag.clear_failed_generation(str(req_id), pool)
    after = await ag.fetch_request(TENANT, req_id, pool)
    assert after["goal"] is None and after["generating"] is False and after["generation_failed"] is True

    retry = await ag.start_goal_generation(TENANT, req_id, "promotion", pool)  # tenant can retry
    assert retry["generating"] is True and retry["generation_failed"] is False
