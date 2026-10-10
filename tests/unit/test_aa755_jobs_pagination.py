"""AA-755 — server-side pagination / sort / filters for the Jobs page.

Covers the no-DB pieces: the sort whitelist (`queue.JOB_SORTS` / `queue._order_by`), the list +
count SQL built by `queue.list_jobs` / `queue.count_jobs`, and the router's page→offset math and
its 422s on an unknown sort key or direction. The live SQL against real Postgres is exercised in
tests/integration/test_aa650_job_queue.py.

The security property under test: the `sort` value is NEVER interpolated into SQL. The page can
only ask for a whitelisted key; the value stored in the whitelist is a trusted SQL expression and
an unknown key is rejected (422), so a column name can never be injected.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.jobs import queue


# ── sort whitelist (queue.JOB_SORTS / _order_by) ────────────────────────────────────────────────

def test_sort_whitelist_has_the_columns_the_page_shows():
    for key in ("created_at", "started_at", "finished_at", "kind", "status", "cost_usd", "duration"):
        assert key in queue.JOB_SORTS


def test_duration_sort_is_finished_minus_started():
    assert queue.JOB_SORTS["duration"] == "(j.finished_at - j.started_at)"


def test_cost_sort_reuses_the_read_side_llm_cost_expression():
    # Same expression the list already shows (handler cost + summed llm_call_log), not a new column.
    assert queue.LLM_COST_SQL in queue.JOB_SORTS["cost_usd"]


def test_order_by_appends_id_tiebreak_and_direction():
    sql = queue._order_by("kind", "asc")
    assert sql == "j.kind ASC NULLS FIRST, j.id ASC"
    sql = queue._order_by("created_at", "desc")
    assert sql == "j.created_at DESC NULLS LAST, j.id DESC"


def test_order_by_defaults_unknown_direction_to_desc():
    assert queue._order_by("kind", "sideways").startswith("j.kind DESC")


def test_order_by_rejects_an_unknown_sort_key():
    with pytest.raises(ValueError):
        queue._order_by("payload->>'secret'; DROP TABLE shared.job", "asc")


def test_no_sort_value_is_interpolated_into_sql():
    # Only whitelisted expressions ever appear; an attacker-controlled key can never reach SQL.
    for key, expr in queue.JOB_SORTS.items():
        assert queue._order_by(key, "asc").startswith(expr + " ")


# ── list_jobs / count_jobs SQL ──────────────────────────────────────────────────────────────────

def _pool(fetch_return=None, fetchval_return=0):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=fetch_return or [])
    pool.fetchval = AsyncMock(return_value=fetchval_return)
    return pool


@pytest.mark.asyncio
async def test_list_jobs_uses_limit_offset_and_order_by():
    pool = _pool()
    await queue.list_jobs(pool, kind="a3_atomize", status="running", limit=25, offset=50,
                          tour_id="t1", sort="cost_usd", sort_dir="asc")
    sql, *args = pool.fetch.call_args[0]
    assert "LIMIT $4 OFFSET $5" in sql
    # ORDER BY carries the whitelisted expression + the id tie-break.
    assert "ORDER BY" in sql and "j.id ASC" in sql
    # args: kind, status, tour_id, limit, offset
    assert args == ["a3_atomize", "running", "t1", 25, 50]


@pytest.mark.asyncio
async def test_list_jobs_default_order_is_created_at_desc():
    pool = _pool()
    await queue.list_jobs(pool)
    sql = pool.fetch.call_args[0][0]
    assert "j.created_at DESC NULLS LAST, j.id DESC" in sql


@pytest.mark.asyncio
async def test_list_jobs_rejects_unknown_sort():
    with pytest.raises(ValueError):
        await queue.list_jobs(_pool(), sort="nope")


@pytest.mark.asyncio
async def test_count_jobs_shares_the_list_where_clause():
    pool = _pool(fetchval_return=7)
    total = await queue.count_jobs(pool, kind="a3_atomize", status=None, tour_id=None)
    assert total == 7
    sql, *args = pool.fetchval.call_args[0]
    assert "count(*)" in sql
    assert queue._LIST_WHERE in sql
    assert args == ["a3_atomize", None, None]


# ── router: page→offset math + 422s ───────────────────────────────────────────────────────────

def _make_request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


@pytest.mark.asyncio
async def test_router_translates_page_to_offset_and_returns_pagination(monkeypatch):
    from api.routers import admin_job_runner

    seen = {}

    async def fake_list(pool, **kw):
        seen.update(kw)
        return [{"id": "j1"}]

    async def fake_count(pool, **kw):
        return 120

    monkeypatch.setattr(queue, "list_jobs", fake_list)
    monkeypatch.setattr(queue, "count_jobs", fake_count)

    out = await admin_job_runner.list_jobs(
        _make_request(MagicMock()), kind=None, status=None, limit=50,
        page=3, page_size=25, sort="cost_usd", sort_dir="asc", tour_id=None,
    )
    # page 3 (1-based) at size 25 → offset 50
    assert seen["offset"] == 50
    assert seen["limit"] == 25
    assert seen["sort"] == "cost_usd" and seen["sort_dir"] == "asc"
    assert out["total"] == 120
    assert out["pagination"] == {"page": 3, "page_size": 25, "total": 120, "pages": 5}


@pytest.mark.asyncio
async def test_router_page_size_defaults_to_limit_when_omitted(monkeypatch):
    from api.routers import admin_job_runner

    seen = {}

    async def fake_list(pool, **kw):
        seen.update(kw)
        return []

    async def fake_count(pool, **kw):
        return 0

    monkeypatch.setattr(queue, "list_jobs", fake_list)
    monkeypatch.setattr(queue, "count_jobs", fake_count)

    await admin_job_runner.list_jobs(
        _make_request(MagicMock()), kind=None, status=None, limit=100,
        page=1, page_size=None, sort="created_at", sort_dir="desc", tour_id=None,
    )
    assert seen["limit"] == 100 and seen["offset"] == 0


@pytest.mark.asyncio
async def test_router_rejects_unknown_sort():
    from fastapi import HTTPException
    from api.routers import admin_job_runner

    with pytest.raises(HTTPException) as exc:
        await admin_job_runner.list_jobs(
            _make_request(MagicMock()), kind=None, status=None, limit=50,
            page=1, page_size=None, sort="evil", sort_dir="desc", tour_id=None,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_router_rejects_bad_sort_dir():
    from fastapi import HTTPException
    from api.routers import admin_job_runner

    with pytest.raises(HTTPException) as exc:
        await admin_job_runner.list_jobs(
            _make_request(MagicMock()), kind=None, status=None, limit=50,
            page=1, page_size=None, sort="created_at", sort_dir="up", tour_id=None,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_router_rejects_bad_status():
    from fastapi import HTTPException
    from api.routers import admin_job_runner

    with pytest.raises(HTTPException) as exc:
        await admin_job_runner.list_jobs(
            _make_request(MagicMock()), kind=None, status="banana", limit=50,
            page=1, page_size=None, sort="created_at", sort_dir="desc", tour_id=None,
        )
    assert exc.value.status_code == 422
