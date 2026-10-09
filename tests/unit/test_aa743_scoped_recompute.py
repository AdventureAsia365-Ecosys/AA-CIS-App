"""AA-743 — removing a tour from the active set is scoped (its ranking/route rows superseded right
away) and the platform Score/Route pass is one debounced job scoped to the touched Segments."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.export import handler

TOUR = "11111111-1111-1111-1111-111111111111"


def _conn():
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=["UPDATE 42", "UPDATE 3"])
    conn.fetch = AsyncMock(return_value=[{"segment_id": "s1"}, {"segment_id": "s2"}])

    @asynccontextmanager
    async def tx():
        yield
    conn.transaction = tx
    return conn


@pytest.mark.asyncio
async def test_remove_tour_supersedes_only_its_current_rows():
    conn = _conn()
    out = await handler.remove_tour_from_caches(conn, TOUR)
    assert out == {"ranking_rows": 42, "routes": 3}
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert "acp_contract.atom_ranking SET superseded_at = now()" in sqls[0]
    assert "acp_contract.route SET superseded_at = now()" in sqls[1]
    for c in conn.execute.await_args_list:
        assert "tour_id = $1::uuid AND superseded_at IS NULL" in c.args[0] and c.args[1] == TOUR
        assert "DELETE" not in c.args[0]          # versioned, never deleted (AA-532 / AA-734)


@pytest.mark.asyncio
async def test_tour_segment_ids():
    assert await handler.tour_segment_ids(_conn(), TOUR) == ["s1", "s2"]


@pytest.mark.asyncio
async def test_platform_recompute_relands_only_the_given_segments():
    with patch("services.acp_contract.atom_ranking.precompute_question_landings",
               AsyncMock(return_value={})) as pql, \
         patch("services.acp_contract.atom_ranking.run_atom_ranking", AsyncMock(return_value={})), \
         patch("services.acp_contract.route_detection.run_route_detection", AsyncMock(return_value={})), \
         patch("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP", {"US": 1}):
        await handler.recompute_rankings_and_routes(object(), segment_ids=["s1", "s2"])
        assert pql.await_args.args[1] == {"s1", "s2"}
        await handler.recompute_rankings_and_routes(object())
        assert pql.await_args.args[1] is None     # unscoped = from-scratch (backfill)


def _pool(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn
    pool.acquire = acquire
    return pool


@pytest.mark.asyncio
@pytest.mark.parametrize("status,removed", [("trashed", True), ("inactive", True), ("active", False)])
async def test_status_change_removes_now_and_enqueues_a_scoped_pass(status, removed):
    from api.routers import admin
    conn = _conn()
    with patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock(return_value=("j", True))) as enq:
        await admin._recompute_after_status_change(_pool(conn), TOUR, status)
    assert (conn.execute.await_count == 2) is removed
    kw = enq.await_args.kwargs
    assert kw["scope"] == "platform" and kw["segment_ids"] == ["s1", "s2"] and kw["debounce_s"] > 0


@pytest.mark.asyncio
async def test_status_change_never_raises():
    from api.routers import admin
    conn = _conn()
    conn.fetch = AsyncMock(side_effect=RuntimeError("db down"))
    await admin._recompute_after_status_change(_pool(conn), TOUR, "trashed")


@pytest.mark.asyncio
async def test_job_pool_is_a_real_pool_with_room_for_concurrent_decides():
    """S219: one shared connection made concurrent decide() calls collide ("another operation is
    in progress") and fail open — the jobs must open a real pool with more than one connection."""
    with patch("services.export.handler.asyncpg.create_pool", AsyncMock(return_value="pool")) as cp, \
         patch("services.export.handler.get_database_url", MagicMock(return_value="postgresql://x")):
        assert await handler.open_job_pool() == "pool"
    assert cp.await_args.kwargs["max_size"] >= 2


def test_job_paths_no_longer_use_a_single_shared_connection():
    import inspect
    from services.jobs import recompute_job
    assert "_SingleConnAsPool" not in inspect.getsource(recompute_job.run)
    assert "_SingleConnAsPool" not in inspect.getsource(handler._run_a3_atomize_background)
