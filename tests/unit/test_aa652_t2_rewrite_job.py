"""AA-652 — T2 tenant rewrite as a durable job: outcomes of services/jobs/t2_rewrite_job.py and
the portal retry endpoint. The full rewrite path (QA gate, save) is driven end to end in
test_aa469_viec1_t4_t5_split.py."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.jobs import t2_rewrite_job as t2
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext, NonRetryable

VERSION_ID = "66666666-6666-6666-6666-666666666666"
PAYLOAD = {"version_id": VERSION_ID, "tenant_id": "t-1", "published_tour_id": "p-1",
           "rewrite_language": "EN-US"}


def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _ctx(pool, payload=PAYLOAD):
    job = queue.Job(id="j1", kind=t2.KIND, payload=payload, status="running", attempt=1,
                    max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(pool, job, {"redis": None})
    ctx.progress = AsyncMock()
    return ctx


def test_kind_is_registered_with_a_terminal_hook():
    k = registry.get_kind(t2.KIND)
    assert k is not None and k.on_terminal is t2.mark_version_failed and k.max_attempts == 2


@pytest.mark.asyncio
async def test_incomplete_payload_is_not_retryable():
    with pytest.raises(NonRetryable):
        await t2.run(_ctx(_pool(AsyncMock()), {"version_id": VERSION_ID}))


@pytest.mark.asyncio
async def test_version_already_written_is_skipped():
    conn = AsyncMock()
    conn.fetchval.return_value = "ai_generated"
    out = await t2.run(_ctx(_pool(conn)))
    assert out == {"skipped": True, "version_status": "ai_generated"}
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsuccessful_rewrite_raises_so_the_job_retries():
    """Before AA-652 a non-success result wrote nothing and left the version 'pending' forever."""
    conn = AsyncMock()
    conn.fetchval.return_value = "pending"
    conn.fetchrow.side_effect = [{"id": "p-1", "tour_id": "tour-1", "aa_name": "Sapa", "country": "VN"},
                                 None, {"top_keywords": "[]"}]
    with patch.object(t2, "_prepare", AsyncMock(return_value=({"name": "Sapa"}, {}))), \
         patch("api.routers.v1_pipeline._rewrite_tour",
               AsyncMock(return_value={"status": "failed", "error": "Bedrock throttled"})):
        with pytest.raises(RuntimeError, match="did not succeed"):
            await t2.run(_ctx(_pool(conn)))


@pytest.mark.asyncio
async def test_terminal_hook_marks_a_pending_version_failed():
    conn = AsyncMock()
    await t2.mark_version_failed(_pool(conn), {"id": "j1", "status": "failed", "payload": PAYLOAD})
    sql, version = conn.execute.call_args.args
    assert "status = 'failed'" in sql and "status = 'pending'" in sql and version == VERSION_ID


# ── portal retry endpoint ─────────────────────────────────────────────────────────────────────

def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


@pytest.mark.asyncio
async def test_retry_requeues_the_job_and_resets_the_version():
    from api.routers import v1_tours
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": VERSION_ID, "status": "failed", "job_id": "job-9"}
    pool = _pool(conn)
    with patch("shared.jobs.queue.retry", AsyncMock(return_value=True)) as m_retry, \
         patch.object(v1_tours, "write_audit_log", AsyncMock()):
        out = await v1_tours.retry_rewrite(VERSION_ID, _request(pool), {"sub": "t-1"})
    assert out == {"version_id": VERSION_ID, "status": "pending", "job_id": "job-9"}
    m_retry.assert_awaited_once_with(pool, "job-9")
    assert "SET status = 'pending'" in conn.execute.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [{"id": VERSION_ID, "status": "ai_generated", "job_id": "job-9"},
                                 {"id": VERSION_ID, "status": "failed", "job_id": None}])
async def test_retry_refuses_versions_that_did_not_fail_in_a_job(row):
    from api.routers import v1_tours
    conn = AsyncMock()
    conn.fetchrow.return_value = row
    with pytest.raises(HTTPException) as e:
        await v1_tours.retry_rewrite(VERSION_ID, _request(_pool(conn)), {"sub": "t-1"})
    assert e.value.status_code == 409
