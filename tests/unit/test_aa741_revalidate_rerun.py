"""AA-741 (S229): a version can be revalidated more than once.

`revalidate:{content_id}` was a permanent idempotency key in shared.job, so the second Revalidate
of the same version returned the first, finished job and never ran again. Now only an ACTIVE
revalidate of the version is reused."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.routers import admin_pipeline


def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


async def _call(active_job):
    pool = AsyncMock()
    pool.fetchval.return_value = active_job
    with patch.object(admin_pipeline, "verify_admin_secret", lambda _s: None), \
         patch("shared.jobs.registry.enqueue", AsyncMock(return_value=("job-new", True))) as m_enq:
        resp = await admin_pipeline.revalidate_generated_content("tour-1", "content-1", _request(pool), "s")
    return json.loads(resp.body), m_enq


@pytest.mark.asyncio
async def test_finished_revalidate_does_not_block_a_new_one():
    body, m_enq = await _call(active_job=None)
    assert body["job_id"] == "job-new" and body["dedup"] is False
    assert "idempotency_key" not in m_enq.call_args.kwargs


@pytest.mark.asyncio
async def test_active_revalidate_is_reused():
    body, m_enq = await _call(active_job="job-running")
    assert body["job_id"] == "job-running" and body["dedup"] is True
    m_enq.assert_not_called()
