"""AA-756 — an llm_call_log write that fails on the shared pool is retried on a fresh connection."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.llm_client import call_log


class _BrokenPool:
    @asynccontextmanager
    async def acquire(self):
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=RuntimeError("another operation is in progress"))
        yield conn


@pytest.mark.asyncio
async def test_pool_failure_falls_back_to_a_fresh_connection():
    with patch.object(call_log, "record_call", new=AsyncMock()) as fresh:
        await call_log.record_call_with_pool(
            _BrokenPool(), stage="a3_demand", role="validate", model="jev-latest", tokens_in=400,
            tokens_out=0, cost_usd=0.0000168, quality_signal={"source": "decide"}, provider="typesafe")
    fresh.assert_awaited_once()
    kw = fresh.await_args.kwargs
    assert (kw["stage"], kw["tokens_in"], kw["cost_usd"], kw["provider"]) == ("a3_demand", 400, 0.0000168, "typesafe")


@pytest.mark.asyncio
async def test_pool_success_does_not_write_twice():
    conn = MagicMock()
    conn.execute = AsyncMock()

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    with patch.object(call_log, "record_call", new=AsyncMock()) as fresh:
        await call_log.record_call_with_pool(
            _Pool(), stage="s1_grounding", role="validate", model="jev-latest", tokens_in=1,
            tokens_out=0, cost_usd=0.0, quality_signal={}, provider="typesafe")
    conn.execute.assert_awaited_once()
    fresh.assert_not_awaited()
