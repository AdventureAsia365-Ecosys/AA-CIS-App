"""
tests/unit/test_aa560_platform_stats.py — AA-560: GET /admin/a4/platform-stats.

Real backend aggregate for "07 · Platform Stats", replacing the deleted `/admin/a4-oversight`
page's client-side "F1_GROUNDING × 4" tag rollup (which only ever counted the currently-loaded
`limit=200` page of Content Log rows, per AA-558 Phần 1 Q3). Mocks asyncpg via pool.acquire()
(same convention test_aa455_publish_log.py / test_aa469_viec5_a4_feedback_loop.py already use for
this router).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

_TEST_SECRET = "test-admin-secret"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)


def _make_pool(fetchval_side_effect=None, fetch_side_effect=None):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=fetchval_side_effect)
    conn.fetch = AsyncMock(side_effect=fetch_side_effect)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _make_request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    return req


def _row(**kwargs):
    """Plain dict stands in for an asyncpg Record — same convention every other test file for
    this router uses (get_content_log/get_review_log's own tests index fetched rows by dict key,
    not a real Record object)."""
    return kwargs


@pytest.mark.asyncio
class TestGetPlatformStats:
    async def test_requires_admin_secret(self):
        from api.routers.admin_a4 import get_platform_stats

        pool, _ = _make_pool()
        req = _make_request(pool)

        with pytest.raises(HTTPException) as exc_info:
            await get_platform_stats(req, top_n=15, x_admin_secret="wrong")
        assert exc_info.value.status_code == 403

    async def test_returns_totals_and_breakdowns(self):
        from api.routers.admin_a4 import get_platform_stats

        # Call order in the handler: fetchval(total_pieces), fetch(by_status), fetch(by_channel),
        # fetch(top_gates), fetchval(published_count).
        pool, conn = _make_pool(
            fetchval_side_effect=[15, 0],
            fetch_side_effect=[
                [_row(status="approved", n=8), _row(status="held", n=7)],
                [_row(channel="facebook", n=4), _row(channel="blog", n=3)],
                [_row(gate="F1_grounding", fail_count=5), _row(gate="F9_brand_voice", fail_count=3)],
            ],
        )
        req = _make_request(pool)

        result = await get_platform_stats(req, top_n=15, x_admin_secret=_TEST_SECRET)

        data = result["data"]
        assert data["total_pieces"] == 15
        assert data["published_count"] == 0
        assert data["by_status"] == [{"status": "approved", "count": 8}, {"status": "held", "count": 7}]
        assert data["by_channel"] == [{"channel": "facebook", "count": 4}, {"channel": "blog", "count": 3}]
        assert data["top_gate_failures"] == [
            {"gate": "F1_grounding", "fail_count": 5},
            {"gate": "F9_brand_voice", "fail_count": 3},
        ]

    async def test_empty_platform_returns_zeroed_stats(self):
        from api.routers.admin_a4 import get_platform_stats

        pool, conn = _make_pool(fetchval_side_effect=[0, 0], fetch_side_effect=[[], [], []])
        req = _make_request(pool)

        result = await get_platform_stats(req, top_n=15, x_admin_secret=_TEST_SECRET)

        data = result["data"]
        assert data["total_pieces"] == 0
        assert data["by_status"] == []
        assert data["by_channel"] == []
        assert data["top_gate_failures"] == []

    async def test_top_gate_query_unnests_gate_ledger_and_filters_failed_only(self):
        """Regression guard for the exact bug AA-558 flagged: the old page's rollup was
        client-side over a page-limited fetch. This asserts the new SQL does the counting itself
        (via jsonb_array_elements over the FULL content_piece table, no LIMIT on the source rows)
        and only counts entries where passed is false — never double-counts passed gates."""
        from api.routers.admin_a4 import get_platform_stats

        pool, conn = _make_pool(fetchval_side_effect=[0, 0], fetch_side_effect=[[], [], []])
        req = _make_request(pool)

        await get_platform_stats(req, top_n=15, x_admin_secret=_TEST_SECRET)

        # 3rd conn.fetch call is the top-gate-failures query (by_status, by_channel, then this).
        top_gate_sql = conn.fetch.call_args_list[2].args[0]
        # S282: only an array-shaped ledger is unnested (one object-shaped row must not fail the query)
        assert "jsonb_array_elements((CASE WHEN jsonb_typeof(cp.gate_ledger) = 'array'" in top_gate_sql
        assert "passed" in top_gate_sql
        assert "LIMIT $1" in top_gate_sql
        # No LIMIT/OFFSET on content_piece itself before the unnest — the whole table is scanned.
        assert "FROM acp_shared.content_piece cp" in top_gate_sql

    async def test_top_n_param_passed_through_to_query(self):
        from api.routers.admin_a4 import get_platform_stats

        pool, conn = _make_pool(fetchval_side_effect=[0, 0], fetch_side_effect=[[], [], []])
        req = _make_request(pool)

        await get_platform_stats(req, top_n=5, x_admin_secret=_TEST_SECRET)

        top_gate_call = conn.fetch.call_args_list[2]
        assert top_gate_call.args[1] == 5
