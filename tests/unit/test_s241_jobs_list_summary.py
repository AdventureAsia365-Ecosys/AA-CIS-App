"""S241 — `GET /admin/job-runner/jobs?fields=summary` drops payload/result from the list.

The Jobs page polls the list; payload/result are JSON blobs only the drawer shows (it reads one
job via `/jobs/{id}`, which still returns everything). Summary mode keeps the same keys (as null)
so older and newer clients parse the same shape. Default (no `fields`) is unchanged.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.jobs import queue


def _pool():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=0)
    return pool


def _request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


def test_summary_columns_null_out_payload_and_result_only():
    # Guards the str.replace that builds _SUMMARY_COLUMNS: if _LIST_COLUMNS is reworded the
    # replace would silently no-op and summary mode would carry the blobs again.
    assert queue._SUMMARY_COLUMNS != queue._LIST_COLUMNS
    assert "NULL::jsonb AS payload" in queue._SUMMARY_COLUMNS
    assert "NULL::jsonb AS result" in queue._SUMMARY_COLUMNS
    assert "progress" in queue._SUMMARY_COLUMNS  # the list's progress column still needs it


@pytest.mark.asyncio
async def test_list_jobs_default_selects_payload_and_result():
    pool = _pool()
    await queue.list_jobs(pool)
    sql = pool.fetch.call_args[0][0]
    assert queue._LIST_COLUMNS in sql


@pytest.mark.asyncio
async def test_list_jobs_without_blobs_selects_summary_columns():
    pool = _pool()
    await queue.list_jobs(pool, include_blobs=False)
    sql = pool.fetch.call_args[0][0]
    assert queue._SUMMARY_COLUMNS in sql
    assert queue._LIST_COLUMNS not in sql


def test_row_dict_handles_null_blobs():
    row = {"id": "j1", "payload": None, "progress": '{"done": 1, "total": 2}', "result": None}
    d = queue._row_dict(row)
    assert d["payload"] is None and d["result"] is None
    assert d["progress"] == {"done": 1, "total": 2}


async def _call(fields, monkeypatch):
    from api.routers import admin_job_runner

    seen = {}

    async def fake_list(pool, **kw):
        seen.update(kw)
        return []

    async def fake_count(pool, **kw):
        assert "include_blobs" not in kw  # count has no column list
        return 0

    monkeypatch.setattr(queue, "list_jobs", fake_list)
    monkeypatch.setattr(queue, "count_jobs", fake_count)
    kwargs = dict(kind=None, status=None, limit=50, page=1, page_size=25, sort="created_at",
                  sort_dir="desc", tour_id=None, since=None, until=None, created_by=None, q=None)
    if fields is not None:
        kwargs["fields"] = fields
    await admin_job_runner.list_jobs(_request(MagicMock()), **kwargs)
    return seen


@pytest.mark.asyncio
async def test_router_summary_drops_blobs(monkeypatch):
    seen = await _call("summary", monkeypatch)
    assert seen["include_blobs"] is False


@pytest.mark.asyncio
async def test_router_full_and_omitted_keep_blobs(monkeypatch):
    assert (await _call("full", monkeypatch))["include_blobs"] is True
    # Omitted: a direct call gets the Query default object, which must mean "full".
    assert (await _call(None, monkeypatch))["include_blobs"] is True


@pytest.mark.asyncio
async def test_router_rejects_unknown_fields(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _call("everything", monkeypatch)
    assert exc.value.status_code == 422
