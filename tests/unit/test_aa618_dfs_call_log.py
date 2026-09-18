"""AA-618 (epic AA-616) — DataForSEO usage/cost logging.

record_dfs_call*() persist shared.dfs_call_log (migration 154); extract_cost() reads the real
`cost` field off a DFS response. No live DB / no live DFS — asyncpg mocked.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.dfs_client.call_log import extract_cost


def test_extract_cost_top_level():
    assert extract_cost({"cost": 0.075, "tasks": []}) == 0.075


def test_extract_cost_sums_tasks_when_no_top_level():
    resp = {"tasks": [{"cost": 0.01}, {"cost": 0.02}]}
    assert extract_cost(resp) == pytest.approx(0.03)


def test_extract_cost_none_when_absent():
    assert extract_cost({"tasks": [{"result": []}]}) is None
    assert extract_cost({}) is None
    assert extract_cost(None) is None


@pytest.mark.asyncio
async def test_record_dfs_call_live_forwards_all_fields():
    from shared.dfs_client import call_log

    fake_conn = AsyncMock()
    with patch("shared.dfs_client.call_log.asyncpg.connect", AsyncMock(return_value=fake_conn)), \
         patch("shared.dfs_client.call_log.get_database_url", return_value="postgres://fake"):
        await call_log.record_dfs_call(
            endpoint="search_volume", fetched_live=True, cost_usd=0.05,
            tenant_id=None, tour_id="11111111-1111-1111-1111-111111111111",
            keyword="Bhutan festivals", location_code=2840, keyword_count=1,
        )
    args = fake_conn.execute.call_args.args
    # bind order: SQL, tenant_id, tour_id, endpoint, keyword, location_code, cost_usd,
    #             cache_hit, fetched_live, keyword_count, meta
    assert args[3] == "search_volume"      # endpoint
    assert args[6] == 0.05                  # cost_usd
    assert args[7] is False                 # cache_hit
    assert args[8] is True                  # fetched_live
    assert args[5] == 2840                  # location_code


@pytest.mark.asyncio
async def test_record_dfs_call_cache_hit_zero_cost():
    from shared.dfs_client import call_log

    fake_conn = AsyncMock()
    with patch("shared.dfs_client.call_log.asyncpg.connect", AsyncMock(return_value=fake_conn)), \
         patch("shared.dfs_client.call_log.get_database_url", return_value="postgres://fake"):
        await call_log.record_dfs_call(
            endpoint="fetch_all", cache_hit=True, cost_usd=0.0, keyword="Nepal trek",
        )
    args = fake_conn.execute.call_args.args
    assert args[7] is True     # cache_hit
    assert args[8] is False    # fetched_live
    assert args[6] == 0.0      # cost_usd


@pytest.mark.asyncio
async def test_record_dfs_call_swallows_errors():
    """Fire-and-forget: a DB failure must never propagate into the SEO fetch path."""
    from shared.dfs_client import call_log

    with patch("shared.dfs_client.call_log.asyncpg.connect",
               AsyncMock(side_effect=RuntimeError("db down"))), \
         patch("shared.dfs_client.call_log.get_database_url", return_value="postgres://fake"):
        # must not raise
        await call_log.record_dfs_call(endpoint="serp_advanced", fetched_live=True, cost_usd=0.02)
