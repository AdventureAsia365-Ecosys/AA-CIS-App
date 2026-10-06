"""AA-723 — the three in-process API background tasks moved onto the durable worker runner:
`s1_rewrite`, `revalidate`, `s1_batch_ingest`. Mocks the executors; the full rewrite path is
covered by the S1 pipeline tests. Also checks the GET /admin/jobs/{id} shared.job->legacy shape map.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.jobs import revalidate_job as rj
from services.jobs import s1_batch_ingest_job as sbj
from services.jobs import s1_rewrite_job as sj
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext, NonRetryable

TOUR_ID = "11111111-1111-1111-1111-111111111111"
VERSION_ID = "22222222-2222-2222-2222-222222222222"


def _ctx(kind, payload, progress=None):
    job = queue.Job(id="job-1", kind=kind, payload=payload, status="running", attempt=1,
                    max_attempts=1, progress=progress or {}, cost_usd=0.0)
    ctx = JobContext(None, job, {"redis": None})
    ctx.progress = AsyncMock()
    return ctx


# ── registration ──────────────────────────────────────────────────────────────────────────────

def test_all_three_kinds_are_registered():
    for name, conc, attempts in [("s1_rewrite", 4, 1), ("revalidate", 2, 2), ("s1_batch_ingest", 1, 1)]:
        k = registry.get_kind(name)
        assert k is not None, f"{name} not registered"
        assert k.concurrency == conc and k.max_attempts == attempts


# ── s1_rewrite ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_s1_missing_tour_id_is_not_retryable():
    with pytest.raises(NonRetryable):
        await sj.run(_ctx("s1_rewrite", {}))


@pytest.mark.asyncio
async def test_s1_read_before_write_skips_a_job_that_already_wrote_its_version():
    """A reap/retry of a job whose progress already carries a version_id must NOT rewrite."""
    ctx = _ctx("s1_rewrite", {"tour_id": TOUR_ID, "batch_id": "b", "tenant_id": "x"},
               progress={"version_id": VERSION_ID})
    with patch("api.routers.admin_pipeline._run_tour_safe", AsyncMock()) as safe:
        out = await sj.run(ctx)
    assert out == {"version_id": VERSION_ID, "resumed": True}
    safe.assert_not_awaited()  # the executor never runs a second time


@pytest.mark.asyncio
async def test_s1_none_result_raises_so_the_run_is_not_marked_succeeded():
    ctx = _ctx("s1_rewrite", {"tour_id": TOUR_ID, "batch_id": "b", "tenant_id": "x"})
    with patch("api.routers.admin_pipeline._build_s1_progress_sink", return_value=None), \
         patch("api.routers.admin_pipeline._run_tour_safe", AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError, match="after retries"):
            await sj.run(ctx)


@pytest.mark.asyncio
async def test_s1_happy_path_returns_version_and_records_it_in_progress():
    ctx = _ctx("s1_rewrite", {"tour_id": TOUR_ID, "batch_id": "b", "tenant_id": "x"})
    result = {"version_id": VERSION_ID, "status": "success", "quality_score": 8.1,
              "model_used": "sonnet", "brand_audit_status": "pass"}
    with patch("api.routers.admin_pipeline._build_s1_progress_sink", return_value=None), \
         patch("api.routers.admin_pipeline._run_tour_safe", AsyncMock(return_value=result)):
        out = await sj.run(ctx)
    assert out["version_id"] == VERSION_ID and out["status"] == "success"
    # version id persisted to progress for idempotent resume
    ctx.progress.assert_any_await(phase="done", version_id=VERSION_ID)


# ── revalidate ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revalidate_missing_content_id_is_not_retryable():
    with pytest.raises(NonRetryable):
        await rj.run(_ctx("revalidate", {"tour_id": TOUR_ID}))


@pytest.mark.asyncio
async def test_revalidate_runs_the_graph_and_surfaces_the_outcome():
    ctx = _ctx("revalidate", {"content_id": VERSION_ID, "tour_id": TOUR_ID})
    rv = {"content_id": VERSION_ID, "revalidate_passed": True, "quality_score": 7.5}
    with patch("api.routers.admin_pipeline._revalidate_tour", AsyncMock(return_value=rv)):
        out = await rj.run(ctx)
    assert out["version_id"] == VERSION_ID and out["revalidate_passed"] is True


# ── s1_batch_ingest ───────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_ingest_missing_fields_is_not_retryable():
    with pytest.raises(NonRetryable):
        await sbj.run(_ctx("s1_batch_ingest", {"job_arn": "arn", "batch_id": "b"}))


@pytest.mark.asyncio
async def test_batch_ingest_polls_and_returns_summary():
    payload = {"job_arn": "arn", "output_uri": "s3://o", "account": "acc3",
               "tour_ids": [TOUR_ID], "batch_id": "b"}
    summary = {"ingested": 1, "gate_fail_retry": 0, "batch_write_failed": 0}
    with patch("services.content_generation.s1_batch.ingest_s1_batch",
               AsyncMock(return_value=summary)) as ing:
        out = await sbj.run(_ctx("s1_batch_ingest", payload))
    assert out == summary
    assert ing.await_args.kwargs["poll"] is True


# ── GET /admin/jobs/{id} shared.job -> legacy shape map ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_maps_shared_job_row_to_the_legacy_s1_page_shape():
    from datetime import datetime, timezone
    from api.routers import admin_pipeline as ap

    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    row = {"id": "job-1", "kind": "s1_rewrite", "status": "succeeded",
           "progress": {"current_stage": "export", "version_id": VERSION_ID},
           "result": {"version_id": VERSION_ID}, "error": None,
           "created_at": now, "started_at": now, "finished_at": now}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=None)))
    with patch("api.routers.admin_pipeline.verify_admin_secret"), \
         patch("shared.jobs.queue.get", AsyncMock(return_value=row)):
        out = await ap.get_run_tour_job("job-1", request, x_admin_secret="s")
    assert out["status"] == "succeeded"
    assert out["result_version_id"] == VERSION_ID
    assert out["current_stage"] == "export"
    assert out["job_type"] == "s1_rewrite"


@pytest.mark.asyncio
async def test_poll_maps_cancelled_to_interrupted_for_the_page():
    from api.routers import admin_pipeline as ap
    row = {"id": "job-1", "kind": "s1_rewrite", "status": "cancelled", "progress": {},
           "result": None, "error": "deploy", "created_at": None, "started_at": None,
           "finished_at": None}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=None)))
    with patch("api.routers.admin_pipeline.verify_admin_secret"), \
         patch("shared.jobs.queue.get", AsyncMock(return_value=row)):
        out = await ap.get_run_tour_job("job-1", request, x_admin_secret="s")
    assert out["status"] == "interrupted"
