"""AA-493 — api/routers/admin_llm_ops.py surfaces stop_reason/truncated_count.

Mocks the asyncpg pool (same shape as tests/unit/test_aa300_admin_atoms.py) — this only proves
the Python passthrough is correct (the row dict from the SQL query reaches the JSON response
unmangled); the SQL's own GROUP BY/FILTER semantics were verified by reading the real query.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers import admin_llm_ops


def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


@pytest.mark.asyncio
async def test_llm_usage_tree_passes_through_truncated_count():
    row = {
        "tenant_id": None, "tenant_label": "aa_internal", "model": "sonnet-4-6",
        "stage": "t9_write", "role": "writer", "call_count": 10, "total_cost_usd": 1.23,
        "ok_count": 8, "ok_eligible_count": 10, "avg_atoms_extracted": None,
        "avg_output_len_chars": None, "truncated_count": 3, "last_call_at": None,
    }
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[row])
    result = await admin_llm_ops.get_llm_usage_tree(_make_request(_make_pool(conn)), days=30)
    assert result["branches"][0]["truncated_count"] == 3


@pytest.mark.asyncio
async def test_llm_usage_tree_truncated_count_zero_for_clean_branch():
    row = {
        "tenant_id": None, "tenant_label": "aa_internal", "model": "sonnet-4-6",
        "stage": "t9_write", "role": "writer", "call_count": 10, "total_cost_usd": 1.23,
        "ok_count": 10, "ok_eligible_count": 10, "avg_atoms_extracted": None,
        "avg_output_len_chars": None, "truncated_count": 0, "last_call_at": None,
    }
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[row])
    result = await admin_llm_ops.get_llm_usage_tree(_make_request(_make_pool(conn)), days=30)
    assert result["branches"][0]["truncated_count"] == 0


@pytest.mark.asyncio
async def test_llm_usage_calls_passes_through_stop_reason():
    row = {
        "id": "abc", "tenant_id": None, "stage": "t9_write", "role": "writer",
        "model": "sonnet-4-6", "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.01,
        "quality_signal": {}, "content_piece_id": None, "angle_gate_request_id": None,
        "stop_reason": "max_tokens", "created_at": None,
    }
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[row])
    result = await admin_llm_ops.get_llm_usage_calls(_make_request(_make_pool(conn)))
    assert result["calls"][0]["stop_reason"] == "max_tokens"
