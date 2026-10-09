"""AA-223 — async run-tour (202 + job poll). ADR-2026-016.

Covers the pipeline_jobs read helpers (jobs_repo) and the admin_pipeline poll endpoints WITHOUT
touching the DB or Bedrock:
  * jobs_repo: asyncpg.connect is patched to a fake AsyncMock conn.
  * endpoints: verify_admin_secret patched no-op, coroutines called directly.

AA-735 (ADR 0003 nac 5): `shared.pipeline_jobs` is now read-only history. The writer helpers
(create_job / mark_running / mark_succeeded / update_stage / mark_failed / mark_interrupted /
sweep_interrupted) and the in-process _run_tour_job wrapper were retired — S1 and revalidate run
as shared.job kinds (test_aa723_bg_tasks_on_worker.py / test_aa650_job_runner.py). Only the read
path (get_job, find_active_duplicate) and the shared.job-backed poll endpoints remain here.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.routers import admin_pipeline, jobs_repo

FAKE_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _db_url(monkeypatch):
    # jobs_repo reads os.environ["DATABASE_URL"] before the (patched) connect.
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")


def _fake_conn():
    """AsyncMock conn — fetchval/execute/fetchrow/close are all awaitable."""
    return AsyncMock()


def _req(**over):
    base = dict(tour_id=FAKE_UUID, batch_id="b-1", tenant_id="aa_internal", model_tier="haiku")
    base.update(over)
    return admin_pipeline.TourRunRequest(**base)


# ── jobs_repo read path: find_active_duplicate / get_job ───────────────────────

@pytest.mark.asyncio
async def test_find_active_duplicate_match():
    conn = _fake_conn()
    conn.fetchval.return_value = uuid.UUID(FAKE_UUID)
    with patch("api.routers.jobs_repo.asyncpg.connect", AsyncMock(return_value=conn)):
        res = await jobs_repo.find_active_duplicate(
            {"tour_id": FAKE_UUID, "model_tier": "haiku", "batch_id": "b-1"}
        )
    assert res == FAKE_UUID
    # the 3 JSONB text keys are passed as params in order
    assert conn.fetchval.call_args.args[1:] == (FAKE_UUID, "haiku", "b-1")


@pytest.mark.asyncio
async def test_find_active_duplicate_none():
    conn = _fake_conn()
    conn.fetchval.return_value = None
    with patch("api.routers.jobs_repo.asyncpg.connect", AsyncMock(return_value=conn)):
        res = await jobs_repo.find_active_duplicate(
            {"tour_id": FAKE_UUID, "model_tier": "sonnet", "batch_id": "b-2"}
        )
    assert res is None


# ── endpoints: POST /run-tour-async + GET /jobs/{id} ───────────────────────────
# AA-723: run_tour_async enqueues an `s1_rewrite` job onto shared.job and the poll endpoint reads
# shared.job (mapped to the legacy shape), falling back to the read-only pipeline_jobs row.

def _request():
    from types import SimpleNamespace
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=object())))


@pytest.mark.asyncio
async def test_run_tour_async_endpoint_202_enqueues_s1_rewrite():
    req = _req()
    with patch("api.routers.admin_pipeline.verify_admin_secret"), \
         patch("shared.jobs.registry.enqueue", AsyncMock(return_value=("job-uuid", True))) as enq:
        resp = await admin_pipeline.run_tour_async(req, _request(), x_admin_secret="secret")
    assert resp.status_code == 202
    body = json.loads(resp.body)
    assert body["job_id"] == "job-uuid"
    assert body["status"] == "queued"
    assert body["poll_url"] == "/admin/jobs/job-uuid"
    assert enq.await_args.args[1] == "s1_rewrite"
    # idempotency key = tour + model_tier + batch triple
    assert enq.await_args.kwargs["idempotency_key"] == f"s1_rewrite:{FAKE_UUID}:haiku:b-1"


@pytest.mark.asyncio
async def test_run_tour_async_dedup_returns_existing_job():
    req = _req()
    with patch("api.routers.admin_pipeline.verify_admin_secret"), \
         patch("shared.jobs.registry.enqueue", AsyncMock(return_value=("existing-uuid", False))):
        resp = await admin_pipeline.run_tour_async(req, _request(), x_admin_secret="secret")
    assert resp.status_code == 202
    body = json.loads(resp.body)
    assert body["job_id"] == "existing-uuid"
    assert body["dedup"] is True
    assert body["status"] == "running"


@pytest.mark.asyncio
async def test_get_job_404_when_absent_in_both_tables():
    with patch("api.routers.admin_pipeline.verify_admin_secret"), \
         patch("shared.jobs.queue.get", AsyncMock(return_value=None)), \
         patch("api.routers.jobs_repo.get_job", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as ei:
            await admin_pipeline.get_run_tour_job("missing", _request(), x_admin_secret="secret")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_job_200_maps_shared_job_to_legacy_shape():
    row = {"id": FAKE_UUID, "kind": "s1_rewrite", "status": "succeeded",
           "progress": {"current_stage": "export", "version_id": FAKE_UUID},
           "result": {"version_id": FAKE_UUID}, "error": None,
           "created_at": None, "started_at": None, "finished_at": None}
    with patch("api.routers.admin_pipeline.verify_admin_secret"), \
         patch("shared.jobs.queue.get", AsyncMock(return_value=row)):
        res = await admin_pipeline.get_run_tour_job(FAKE_UUID, _request(), x_admin_secret="secret")
    assert res["status"] == "succeeded"
    assert res["result_version_id"] == FAKE_UUID
    assert res["current_stage"] == "export"


@pytest.mark.asyncio
async def test_get_job_falls_back_to_legacy_pipeline_jobs_row():
    legacy = {"id": FAKE_UUID, "status": "succeeded", "result_version_id": None}
    with patch("api.routers.admin_pipeline.verify_admin_secret"), \
         patch("shared.jobs.queue.get", AsyncMock(return_value=None)), \
         patch("api.routers.jobs_repo.get_job", AsyncMock(return_value=legacy)):
        res = await admin_pipeline.get_run_tour_job(FAKE_UUID, _request(), x_admin_secret="secret")
    assert res == legacy


# ── AA-250 B2: current_stage read-through (migration 076) ───────────────────────

@pytest.mark.asyncio
async def test_get_job_selects_and_returns_current_stage():
    conn = _fake_conn()
    conn.fetchrow.return_value = {
        "id": uuid.UUID(FAKE_UUID), "job_type": "run_tour", "status": "running",
        "result_version_id": None, "pipeline_run_id": None, "error": None,
        "current_stage": "llm_judge",
        "created_at": None, "started_at": None, "finished_at": None, "heartbeat_at": None,
    }
    with patch("api.routers.jobs_repo.asyncpg.connect", AsyncMock(return_value=conn)):
        job = await jobs_repo.get_job(FAKE_UUID)
    sql = conn.fetchrow.call_args.args[0]
    assert "current_stage" in sql
    assert job["current_stage"] == "llm_judge"


@pytest.mark.asyncio
async def test_get_job_current_stage_null_before_first_stage_report():
    """A job that hasn't streamed a node yet (queued, or created pre-migration-076)."""
    conn = _fake_conn()
    conn.fetchrow.return_value = {
        "id": uuid.UUID(FAKE_UUID), "job_type": "run_tour", "status": "queued",
        "result_version_id": None, "pipeline_run_id": None, "error": None,
        "current_stage": None,
        "created_at": None, "started_at": None, "finished_at": None, "heartbeat_at": None,
    }
    with patch("api.routers.jobs_repo.asyncpg.connect", AsyncMock(return_value=conn)):
        job = await jobs_repo.get_job(FAKE_UUID)
    assert job["current_stage"] is None
